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
        "archive/tasks",
        "archive/events",
        "archive/notes",
        "locks",
    ]
    for subdir in subdirs:
        (lattice / subdir).mkdir(parents=True, exist_ok=True)

    # Create empty _lifecycle.jsonl ready for appends
    lifecycle_log = lattice / "events" / "_lifecycle.jsonl"
    if not lifecycle_log.exists():
        lifecycle_log.touch()


def find_root(start: Path | None = None) -> Path | None:
    """Find the project root containing .lattice/.

    Checks LATTICE_ROOT env var first. If set, validates it and returns
    the path or raises an error (no fallback to walk-up).

    Otherwise, walks up from start (defaults to cwd) looking for .lattice/.

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
    while True:
        if (current / LATTICE_DIR).is_dir():
            return current
        parent = current.parent
        if parent == current:
            # Reached filesystem root
            return None
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

    Args:
        path: Path to the JSONL file (created if it does not exist).
        line: A single JSONL record ending with a newline character.
    """
    with open(path, "a", encoding="utf-8") as fh:
        fh.write(line)
        fh.flush()
        os.fsync(fh.fileno())
    _fsync_directory(path.parent)
