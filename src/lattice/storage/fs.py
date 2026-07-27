"""Atomic file writes, directory management, and root discovery."""

from __future__ import annotations

import os
import tempfile
from collections.abc import Callable
from pathlib import Path

LATTICE_DIR = ".lattice"
LATTICE_ROOT_ENV = "LATTICE_ROOT"


def _fsync_directory(path: Path) -> None:
    """Fsync a directory to ensure metadata (e.g. renames) is durable.

    Some platforms (notably macOS HFS+) may not support fsync on directory
    file descriptors, so ``OSError`` is silently ignored.
    """
    try:
        fd = os.open(str(path), os.O_RDONLY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
    except OSError:
        pass


def atomic_write(path: Path, content: str | bytes) -> None:
    """Write content to path atomically via temp file + fsync + rename.

    The temp file is created in the same directory as the target to ensure
    os.rename() is an atomic operation (same filesystem).

    Raises:
        FileNotFoundError: If the parent directory does not exist.
    """
    parent = path.parent
    if not parent.is_dir():
        raise FileNotFoundError(f"Parent directory does not exist: {parent}")

    data = content.encode("utf-8") if isinstance(content, str) else content

    fd, tmp_path = tempfile.mkstemp(dir=parent, prefix=".tmp.")
    closed = False
    try:
        # os.write() can short-write; loop until all bytes are flushed.
        mv = memoryview(data)
        while mv:
            written = os.write(fd, mv)
            mv = mv[written:]
        os.fsync(fd)
        os.close(fd)
        closed = True
        os.replace(tmp_path, path)
        _fsync_directory(parent)
    except BaseException:
        if not closed:
            os.close(fd)
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise


def ensure_artifact_dirs(lattice_dir: Path) -> None:
    """Create artifacts/meta and artifacts/payload under an existing .lattice/.

    Both are scaffolded at init, but git doesn't track empty directories, so
    cloned installs of projects with no stored artifacts lack them (LAT-239).
    Call before any artifact payload/metadata write.
    """
    for subdir in ("artifacts/meta", "artifacts/payload"):
        (lattice_dir / subdir).mkdir(parents=True, exist_ok=True)


def ensure_lattice_dirs(root: Path) -> None:
    """Create the full .lattice/ directory structure under root.

    root is the project directory (the directory that will contain .lattice/).
    """
    lattice = root / LATTICE_DIR
    subdirs = [
        "tasks",
        "events",
        "artifacts/meta",
        "artifacts/payload",
        "notes",
        "plans",
        "resources",
        "sessions",
        "sessions/archive",
        "archive/tasks",
        "archive/events",
        "archive/notes",
        "archive/plans",
        "locks",
        "templates",
    ]
    for subdir in subdirs:
        (lattice / subdir).mkdir(parents=True, exist_ok=True)

    # Create empty _lifecycle.jsonl ready for appends
    lifecycle_log = lattice / "events" / "_lifecycle.jsonl"
    if not lifecycle_log.exists():
        lifecycle_log.touch()

    # Scaffold a self-contained .lattice/.gitignore. The board (tasks, events,
    # plans, artifacts, ids.json, config.json) is deliberately tracked — it is
    # the audit log and the cross-machine coordination state. These subdirs are
    # pure ephemeral runtime state with no audit value; tracking them only
    # causes per-worktree divergence and merge collisions. Idempotent so
    # existing projects pick it up on the next lattice invocation.
    gitignore = lattice / ".gitignore"
    if not gitignore.exists():
        atomic_write(
            gitignore,
            "# Ephemeral Lattice runtime state — not part of the durable board.\n"
            "# The board (tasks/ events/ plans/ artifacts/ ids.json config.json)\n"
            "# stays tracked: it is the audit log and cross-machine coordination\n"
            "# state. The paths below are mutated constantly and must never\n"
            "# diverge per-worktree or collide on merge.\n"
            "review_state/\n"
            "tmp-prompts/\n"
            ".daemon/\n"
            "locks/\n",
        )


def find_root(start: Path | None = None) -> Path | None:
    """Find the project root containing .lattice/.

    Checks LATTICE_ROOT env var first. If set, validates it and returns
    the path or raises an error (no fallback to walk-up).

    Otherwise, walks up from start (defaults to cwd) looking for .lattice/.
    Mirrors ``git rev-parse --show-toplevel``: when start is inside a git
    linked worktree, the search jumps to the primary worktree first so the
    canonical .lattice/ is found rather than any stale snapshot copied into
    the worktree at creation time. This makes ``lattice`` worktree-transparent.

    Returns:
        Path to the directory containing .lattice/, or None if not found.

    Raises:
        LatticeRootError: If LATTICE_ROOT is set but invalid.
    """
    env_root = os.environ.get(LATTICE_ROOT_ENV)
    if env_root is not None:
        if not env_root:
            raise LatticeRootError("LATTICE_ROOT is set but empty")
        env_path = Path(env_root)
        if not env_path.is_dir():
            raise LatticeRootError(
                f"LATTICE_ROOT points to a path that does not exist: {env_root}"
            )
        if not (env_path / LATTICE_DIR).is_dir():
            raise LatticeRootError(
                f"LATTICE_ROOT points to a directory with no {LATTICE_DIR}/ inside: {env_root}"
            )
        return env_path

    current = (start or Path.cwd()).resolve()

    primary = _git_primary_worktree(current)
    if primary is not None:
        current = primary

    while True:
        if (current / LATTICE_DIR).is_dir():
            return current
        parent = current.parent
        if parent == current:
            # Reached filesystem root
            return None
        current = parent


def _git_primary_worktree(start: Path) -> Path | None:
    """Return the primary worktree root if start is inside a git linked worktree.

    A linked worktree is marked by a ``.git`` *file* (not directory) whose
    contents are ``gitdir: <abspath>/.git/worktrees/<name>``. The primary
    worktree's root is the parent of that primary ``.git`` directory.

    Returns None when start is not inside any git tree, when the nearest git
    marker is a real ``.git`` directory (i.e., already the primary worktree),
    or when the worktree pointer can't be parsed. In those cases the caller
    keeps its existing walk-up search from start.
    """
    current = start
    while True:
        git_path = current / ".git"
        if git_path.is_dir():
            return None
        if git_path.is_file():
            try:
                content = git_path.read_text(encoding="utf-8").strip()
            except OSError:
                return None
            prefix = "gitdir:"
            if not content.startswith(prefix):
                return None
            gitdir = Path(content[len(prefix) :].strip())
            # gitdir points at <primary>/.git/worktrees/<name>; primary root
            # is two levels up from there.
            primary_git = gitdir.parent.parent
            if primary_git.name == ".git" and primary_git.is_dir():
                return primary_git.parent
            return None
        parent = current.parent
        if parent == current:
            return None
        current = parent


class LatticeRootError(Exception):
    """Raised when LATTICE_ROOT env var is set but invalid."""


def jsonl_append(
    path: Path,
    line: str,
    *,
    after_write: Callable[[], None] | None = None,
    after_fsync: Callable[[], None] | None = None,
) -> None:
    """Append a single line to a JSONL file.

    The caller must already hold the appropriate lock; this function does
    no locking of its own.

    The line **must** already end with ``\\n``.  The function opens the file
    in append mode, writes the line, then flushes and fsyncs to ensure
    durability.

    As a defensive measure, if the file exists and does not end with a
    newline, one is prepended before writing to prevent concatenation
    with the previous record.

    Args:
        path: Path to the JSONL file (created if it does not exist).
        line: A single JSONL record ending with a newline character.
    """
    # Defensive: ensure file ends with newline before appending
    needs_separator = False
    if path.exists() and path.stat().st_size > 0:
        with open(path, "rb") as fh:
            fh.seek(-1, 2)
            needs_separator = fh.read(1) != b"\n"

    with open(path, "a", encoding="utf-8") as fh:
        if needs_separator:
            fh.write("\n")
        fh.write(line)
        if after_write is not None:
            after_write()
        fh.flush()
        os.fsync(fh.fileno())
        if after_fsync is not None:
            after_fsync()
    _fsync_directory(path.parent)
