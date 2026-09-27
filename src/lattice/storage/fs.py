"""Atomic file writes, directory management, and root discovery.

Every write to a board goes through the primitives here: :func:`atomic_write`,
:func:`jsonl_append`, :func:`unlink_path`, and :func:`ensure_dir`. Before it
touches the disk, each one confines its path to its board (``BoardPathError``)
and checks the board's ownership markers (``BoardIsCache`` / ``BoardIsHosted``),
see :mod:`lattice.storage.ownership`; then it tells the active write recorder.

The write recorder (SPEC §8.5)::

    def before(path: Path, kind: str) -> None:  # kind: append | create | replace | unlink
        ...  # e.g. write an undo entry; raising here aborts the write

    result = execute(board_dir, op_name, params, caller, run_hooks=False, on_mutation=before)
    result.paths  # sorted, relative to .lattice/, directories included

    with recording(before) as recorder:  # the same, around any other code
        ...
    recorder.relative_paths(board_dir)

``lattice.ops.execute`` owns one per call: it takes the callback as
``on_mutation`` and returns the paths as ``OpResult.paths``. The recorder sees
durable and workspace paths only (SPEC §6.1), as resolved absolute paths, and
calls the callback before every mutation of one: a file written (``create`` /
``replace``), appended (``append``), or unlinked (``unlink``), and each
directory ``ensure_dir`` creates (``create``). A recorder is a ``contextvars``
value: create it in the thread that runs the operation; it never follows work
into another thread.
"""

from __future__ import annotations

import contextlib
import contextvars
import os
import tempfile
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

from lattice.core.errors import BoardIsCache, BoardIsHosted, BoardPathError, BoardWriteError
from lattice.storage.ownership import check_write, locate

__all__ = [
    "LATTICE_DIR",
    "LATTICE_ROOT_ENV",
    "BoardIsCache",
    "BoardIsHosted",
    "BoardPathError",
    "BoardWriteError",
    "LatticeRootError",
    "MutationKind",
    "WriteRecorder",
    "atomic_write",
    "ensure_artifact_dirs",
    "ensure_dir",
    "ensure_lattice_dirs",
    "find_root",
    "jsonl_append",
    "recording",
    "remove_dir",
    "strict_durability",
    "truncate_file",
    "unlink_path",
]

LATTICE_DIR = ".lattice"
LATTICE_ROOT_ENV = "LATTICE_ROOT"

MutationKind = Literal["append", "create", "replace", "unlink"]


# ---------------------------------------------------------------------------
# Write recorder
# ---------------------------------------------------------------------------


@dataclass
class WriteRecorder:
    """The durable paths one operation changed, and a hook before each change.

    ``callback(path, kind)`` runs before every durable mutation, with the
    resolved path and one of ``append``, ``create``, ``replace``, ``unlink``.
    If it raises, the primitive writes nothing and the exception propagates.
    """

    callback: Callable[[Path, MutationKind], None] | None = None
    _paths: dict[Path, None] = field(default_factory=dict, repr=False)

    @property
    def paths(self) -> list[Path]:
        """Resolved durable paths written, appended, unlinked, or created as
        directories, in first-touch order."""
        return list(self._paths)

    def relative_paths(self, lattice_dir: Path) -> list[str]:
        """The recorded paths under *lattice_dir*, relative to it, sorted (POSIX form)."""
        board = Path(lattice_dir).resolve()
        return sorted(
            p.relative_to(board).as_posix() for p in self._paths if p.is_relative_to(board)
        )

    def _before(self, path: Path, kind: MutationKind) -> None:
        if self.callback is not None:
            self.callback(path, kind)
        self._paths.setdefault(path, None)


_RECORDER: contextvars.ContextVar[WriteRecorder | None] = contextvars.ContextVar(
    "lattice_write_recorder", default=None
)


@contextlib.contextmanager
def recording(
    callback: Callable[[Path, MutationKind], None] | None = None,
) -> Iterator[WriteRecorder]:
    """Record every durable write made in this context until the block exits."""
    recorder = WriteRecorder(callback)
    token = _RECORDER.set(recorder)
    try:
        yield recorder
    finally:
        _RECORDER.reset(token)


def _guard(path: Path, kind: MutationKind | None) -> None:
    """Confine, check markers, and record one mutation of *path* before it happens.

    ``kind`` ``None`` means a whole-file write: ``replace`` if the path exists,
    else ``create``.
    """
    target = locate(path)
    if target is None:
        return
    check_write(target)
    recorder = _RECORDER.get()
    if recorder is not None and target.recorded:
        if kind is None:
            kind = "replace" if os.path.lexists(target.path) else "create"
        recorder._before(target.path, kind)


_STRICT_DURABILITY: contextvars.ContextVar[bool] = contextvars.ContextVar(
    "lattice_strict_durability", default=False
)


@contextlib.contextmanager
def strict_durability() -> Iterator[None]:
    """Make a failed directory fsync raise for the duration of the block.

    A server transaction runs inside one, so recovery can react to a write
    whose durability is unknown (SPEC §8.6). Local mode never enters it.
    """
    token = _STRICT_DURABILITY.set(True)
    try:
        yield
    finally:
        _STRICT_DURABILITY.reset(token)


def _fsync_directory(path: Path) -> None:
    """Fsync a directory to ensure metadata (e.g. renames) is durable.

    Some platforms (notably macOS HFS+) may not support fsync on directory
    file descriptors, so ``OSError`` is silently ignored, except inside
    :func:`strict_durability`, where it propagates.
    """
    try:
        fd = os.open(str(path), os.O_RDONLY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
    except OSError:
        if _STRICT_DURABILITY.get():
            raise


def atomic_write(path: Path, content: str | bytes) -> None:
    """Write content to path atomically via temp file + fsync + rename.

    The temp file is created in the same directory as the target to ensure
    os.rename() is an atomic operation (same filesystem).

    Raises:
        BoardWriteError: If the path escapes its board, or the board is a
            cache or server-owned and this context is not its writer.
        FileNotFoundError: If the parent directory does not exist.
    """
    _guard(path, None)
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


def ensure_dir(path: Path) -> None:
    """``mkdir -p`` for a directory, confined to its board and marker-checked.

    An existing directory is left alone without a marker check (so reads on a
    cache that ensure a directory keep working). Each directory it creates,
    missing parents first, is checked like any other write to its path class
    and reported to the recorder as a ``create`` before its ``mkdir``.
    """
    locate(path)  # confinement, even when nothing needs creating
    missing: list[Path] = []
    current = Path(path)
    while not current.is_dir() and current.parent != current:
        missing.append(current)
        current = current.parent
    for directory in reversed(missing):
        _guard(directory, "create")
        directory.mkdir(exist_ok=True)


def unlink_path(path: Path, *, missing_ok: bool = False) -> None:
    """Remove a file, confined to its board, marker-checked, and recorded."""
    if missing_ok and not os.path.lexists(path):
        return
    _guard(path, "unlink")
    path.unlink(missing_ok=missing_ok)


def truncate_file(path: Path, length: int) -> None:
    """Cut a file back to *length* bytes and fsync it, confined, marker-checked, and
    recorded as a ``replace``. Used to roll back appends (SPEC §8.6)."""
    _guard(path, "replace")
    with open(path, "r+b") as fh:
        fh.truncate(length)
        fh.flush()
        os.fsync(fh.fileno())


def remove_dir(path: Path) -> None:
    """Remove an empty directory, confined, marker-checked, and recorded as an
    ``unlink``. Used to roll back a directory an operation created (SPEC §8.6)."""
    _guard(path, "unlink")
    path.rmdir()
    _fsync_directory(path.parent)


def ensure_artifact_dirs(lattice_dir: Path) -> None:
    """Create artifacts/meta and artifacts/payload under an existing .lattice/.

    Both are scaffolded at init, but git doesn't track empty directories, so
    cloned installs of projects with no stored artifacts lack them (LAT-239).
    Call before any artifact payload/metadata write.
    """
    for subdir in ("artifacts/meta", "artifacts/payload"):
        ensure_dir(lattice_dir / subdir)


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
        ensure_dir(lattice / subdir)

    # Create empty _lifecycle.jsonl ready for appends
    lifecycle_log = lattice / "events" / "_lifecycle.jsonl"
    if not lifecycle_log.exists():
        atomic_write(lifecycle_log, b"")

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
    _guard(path, "append")
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
