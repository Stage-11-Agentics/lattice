"""Atomic file writes, directory management, and root discovery."""

from __future__ import annotations

import os
import tempfile
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


def find_root(start: Path | None = None, *, prefer_worktree: bool = False) -> Path | None:
    """Find the project root containing .lattice/.

    Checks LATTICE_ROOT env var first. If set, validates it and returns
    the path or raises an error (no fallback to walk-up).

    Otherwise, walks up from start (defaults to cwd) looking for .lattice/.
    Mirrors ``git rev-parse --show-toplevel``: when start is inside a git
    linked worktree, the search jumps to the primary worktree first so the
    canonical .lattice/ is found rather than any stale snapshot copied into
    the worktree at creation time. This makes ``lattice`` worktree-transparent.

    When *prefer_worktree* is True (used by commands whose output rides the
    feature branch — ``branch-link``, ``branch-unlink``, ``code-review``),
    the search prefers a worktree-local ``.lattice/``. If the worktree has
    none, it falls back to the auto-route to the primary. This keeps
    tracked-``.lattice/`` projects (e.g. c11) writing artifacts into the
    feature branch while gitignored-``.lattice/`` projects (e.g. Lattice
    itself) still update canonical coordination state.

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

    start_resolved = (start or Path.cwd()).resolve()
    primary, worktree = _detect_worktree(start_resolved)

    if prefer_worktree and worktree is not None:
        # Try the worktree-local .lattice/ first; only consider the worktree
        # subtree, since a hit higher up the filesystem would route to whatever
        # ancestor happens to have a .lattice/ — not the worktree's own.
        found = _walk_up_for_lattice(start_resolved, ceiling=worktree)
        if found is not None:
            return found
        # Fall back to the auto-route below.

    walk_start = primary if primary is not None else start_resolved
    return _walk_up_for_lattice(walk_start)


def _walk_up_for_lattice(start: Path, *, ceiling: Path | None = None) -> Path | None:
    """Walk up from start looking for a .lattice/ directory.

    When *ceiling* is provided, the search stops after checking *ceiling*
    itself (used by ``prefer_worktree=True`` to restrict the worktree-local
    search to the worktree subtree).
    """
    current = start
    while True:
        if (current / LATTICE_DIR).is_dir():
            return current
        if ceiling is not None and current == ceiling:
            return None
        parent = current.parent
        if parent == current:
            return None
        current = parent


def _detect_worktree(start: Path) -> tuple[Path | None, Path | None]:
    """Detect whether *start* is inside a git linked worktree.

    Returns ``(primary_root, worktree_root)`` when *start* is inside a linked
    worktree, both resolved to absolute paths. Returns ``(None, None)`` when
    *start* is not inside any git tree, when the nearest git marker is a real
    ``.git`` directory (i.e. already the primary worktree), or when the
    worktree pointer can't be parsed.

    The worktree marker is a ``.git`` *file* (not directory) whose contents
    are ``gitdir: <abspath>/.git/worktrees/<name>``. The primary worktree's
    root is the parent of that primary ``.git`` directory.
    """
    current = start
    while True:
        git_path = current / ".git"
        if git_path.is_dir():
            return None, None
        if git_path.is_file():
            try:
                content = git_path.read_text(encoding="utf-8").strip()
            except OSError:
                return None, None
            prefix = "gitdir:"
            if not content.startswith(prefix):
                return None, None
            gitdir = Path(content[len(prefix) :].strip())
            # gitdir points at <primary>/.git/worktrees/<name>; primary root
            # is two levels up from there.
            primary_git = gitdir.parent.parent
            if primary_git.name == ".git" and primary_git.is_dir():
                return primary_git.parent, current
            return None, None
        parent = current.parent
        if parent == current:
            return None, None
        current = parent


class LatticeRootError(Exception):
    """Raised when LATTICE_ROOT env var is set but invalid."""


def jsonl_append(path: Path, line: str) -> None:
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
        fh.flush()
        os.fsync(fh.fileno())
    _fsync_directory(path.parent)
