"""A bound checkout's own directories, never reached through a symlink (SPEC §9.4).

A hosted root's ``.lattice/``, and the cache-control and runtime directories the
client writes under it (``cache/``, ``cache/rescued/``, ``cache/incoming/``,
``locks/``, ...), must be real directories. A clone can commit ``.lattice`` as a
symlink, and a broken one can leave a file there; a client that followed it
would create, chmod, write, or delete in whatever it names.

So every client-side writer reaches those directories through :func:`open_dir`:
each component is opened relative to its parent's descriptor with
``O_DIRECTORY | O_NOFOLLOW`` (made first where missing) and set to 0700 through
that descriptor, and files are created, replaced, read, and removed through the
directory's descriptor, each with ``O_NOFOLLOW``. A symlink or a non-directory
is refused with :class:`UnsafeCachePath` before anything is changed.

Commands refuse such a checkout (or one whose ``locks/``, ``review_state/``,
``tmp-prompts/``, or ``.daemon/`` is one) up front with :func:`layout_error`
(``BINDING_CONFLICT``, ``details.reason`` ``UNSAFE_CACHE_PATH``): root
classification checks :func:`unsafe_component` for every bound or marked root.
Best-effort side effects (the offline window, the server info, the follower's
record, the acknowledged-write ledger) catch the ``OSError`` and skip, as they
do for any other failure. Each operation validates its base directories once,
at its start: ``catch_up`` and a sync's apply (reset and rescue included),
``cache clear``, the read lock, and the runtime writers shared with local
boards (review state, review prompts, spawn scratch, auto-review logs), which
call :func:`require_safe_board`, a no-op on a local board.

Threat model (orchestrator ruling, LAT-337): this closes the **static** case,
a ``.lattice``, ``cache/``, or runtime directory that is already a symlink or a
non-directory when an operation starts (committed into a malicious or broken
clone). A swap in the middle of an operation needs a concurrent local attacker
with write access to the checkout, who already controls the user's files; that
window is an accepted residual. Operations do not keep directory descriptors
across their board-file writes (the syncer applies board files by path, under
the directories it validated).

No ``fcntl`` here: local Lattice imports this through root classification (G-6).
"""

from __future__ import annotations

import contextlib
import errno
import os
import secrets
import stat
from collections.abc import Iterator
from pathlib import Path

from lattice.core.errors import OpError
from lattice.storage.fs import BINDING_FILE, LATTICE_DIR

PRIVATE_DIR_MODE = 0o700
FILE_MODE = 0o600
NOFOLLOW = getattr(os, "O_NOFOLLOW", 0)
#: Runtime directories every sync creates (SPEC §9.4).
RUNTIME_DIRS = ("locks", "review_state", "tmp-prompts", ".daemon")
#: What root classification checks under ``.lattice/``: the client's own
#: directories, never board data.
GUARDED = ("cache", *RUNTIME_DIRS)
UNSAFE_REASON = "UNSAFE_CACHE_PATH"


class UnsafeCachePath(NotADirectoryError):
    """A cache directory component is a symlink or not a directory."""

    def __init__(self, path: Path) -> None:
        super().__init__(
            errno.ENOTDIR,
            "not a real directory (a symlink or a file); left untouched",
            str(path),
        )


def unsafe_component(root: Path) -> Path | None:
    """The first of ``.lattice`` and the client's own directories under it
    (:data:`GUARDED`) that exists but is not a real directory (never followed),
    or ``None``."""
    lattice_dir = Path(root) / LATTICE_DIR
    for path in (lattice_dir, *(lattice_dir / name for name in GUARDED)):
        try:
            info = os.lstat(path)
        except FileNotFoundError:
            if path == lattice_dir:
                return None
            continue
        if not stat.S_ISDIR(info.st_mode):
            return path
    return None


def layout_error(root: Path, path: Path) -> OpError:
    """``BINDING_CONFLICT`` for a hosted root whose *path* is not a real directory."""
    what = "a symlink" if os.path.islink(path) else "not a directory"
    return OpError(
        "BINDING_CONFLICT",
        f"{path} is {what}; a hosted checkout's cache must be a real directory, and "
        f"Lattice never writes through one. Remove it with `rm {path}` (a symlink's "
        "target is kept; the server holds the board) and run any lattice command to "
        "rebuild the cache.",
        {"root": str(root), "path": str(path), "reason": UNSAFE_REASON},
    )


def require_safe_layout(root: Path) -> None:
    """Raise :func:`layout_error` when *root*'s ``.lattice`` or ``cache`` is unsafe."""
    bad = unsafe_component(root)
    if bad is not None:
        raise layout_error(Path(root), bad)


def routes_to_server(root: Path) -> bool:
    """Whether *root* is bound (``.lattice-remote.json``) or carries a cache marker,
    so its ``.lattice/`` is (or will be) a hosted board's cache."""
    cache_dir = Path(root) / LATTICE_DIR / "cache"
    return (
        (Path(root) / BINDING_FILE).is_file()
        or (cache_dir / "state.json").exists()
        or (cache_dir / "applying").exists()
    )


def require_safe_board(lattice_dir: Path) -> None:
    """Before a runtime writer shared with local boards writes under *lattice_dir*:
    when it is a hosted checkout's ``.lattice/``, require :func:`require_safe_layout`.
    A local board (nothing routes it to a server) is left as it is."""
    lattice_dir = Path(lattice_dir)
    if lattice_dir.name == LATTICE_DIR and routes_to_server(lattice_dir.parent):
        require_safe_layout(lattice_dir.parent)


def open_child(parent_fd: int, name: str, path: Path, *, create: bool = True) -> int:
    """Open directory *name* under *parent_fd* without following it, making it
    first when *create*, and set it to 0700; *path* names it in errors."""
    if create:
        with contextlib.suppress(FileExistsError):
            os.mkdir(name, PRIVATE_DIR_MODE, dir_fd=parent_fd)
    try:
        fd = os.open(name, os.O_RDONLY | os.O_DIRECTORY | NOFOLLOW, dir_fd=parent_fd)
    except OSError as exc:
        if exc.errno in (errno.ELOOP, errno.ENOTDIR):
            raise UnsafeCachePath(path) from exc
        raise
    try:
        os.fchmod(fd, PRIVATE_DIR_MODE)
    except BaseException:
        os.close(fd)
        raise
    return fd


def open_dir(base: Path, *parts: str, create: bool = True) -> int:
    """A descriptor of ``base/parts...``, each part opened by :func:`open_child`.
    *base* itself (the checkout) is opened normally. The caller closes it."""
    base = Path(base)
    fd = os.open(base, os.O_RDONLY | os.O_DIRECTORY)
    for depth, name in enumerate(parts, start=1):
        try:
            child = open_child(fd, name, base.joinpath(*parts[:depth]), create=create)
        finally:
            os.close(fd)
        fd = child
    return fd


@contextlib.contextmanager
def opened_dir(base: Path, *parts: str, create: bool = True) -> Iterator[int]:
    fd = open_dir(base, *parts, create=create)
    try:
        yield fd
    finally:
        os.close(fd)


def write_file(dir_fd: int, name: str, data: bytes, *, mode: int = FILE_MODE) -> None:
    """Replace *name* in *dir_fd* with *data*: a fresh temporary file (``O_EXCL``,
    never followed), written and fsynced, then renamed over *name* (which
    replaces a symlink there rather than writing through it)."""
    tmp = f".{name}.{os.getpid()}.{secrets.token_hex(4)}.tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | NOFOLLOW, mode, dir_fd=dir_fd)
    try:
        try:
            view = memoryview(data)
            while view:
                view = view[os.write(fd, view) :]
            os.fsync(fd)
        finally:
            os.close(fd)
        os.replace(tmp, name, src_dir_fd=dir_fd, dst_dir_fd=dir_fd)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp, dir_fd=dir_fd)
        raise


def read_file(dir_fd: int, name: str) -> bytes | None:
    """*name*'s bytes, or ``None`` when it is missing (a symlink is refused)."""
    try:
        fd = os.open(name, os.O_RDONLY | NOFOLLOW, dir_fd=dir_fd)
    except FileNotFoundError:
        return None
    try:
        chunks = []
        while chunk := os.read(fd, 1 << 16):
            chunks.append(chunk)
        return b"".join(chunks)
    finally:
        os.close(fd)


def remove_file(dir_fd: int, name: str) -> None:
    """Unlink *name* itself (a symlink goes, its target stays); missing is fine."""
    with contextlib.suppress(FileNotFoundError):
        os.unlink(name, dir_fd=dir_fd)
