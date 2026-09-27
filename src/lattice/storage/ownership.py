"""Board ownership: path classes, markers, flags, and offline maintenance (SPEC §6).

One writer per board is enforced where bytes reach the disk. Every storage
write primitive in :mod:`lattice.storage.fs` calls :func:`locate` and
:func:`check_write` before it touches anything, so no command can write a
board it does not own:

- **Path classes (§6.1).** Every path under a ``.lattice/`` belongs to one
  :class:`PathClass`. Only durable and workspace paths are marker-checked and
  recorded; runtime, temporary, cache-control, and unmanaged paths stay
  writable everywhere, so reads (which take lock files) work on a cache.
- **Markers (§6.2).** ``cache/state.json`` or ``cache/applying`` makes a board a
  client cache (``BOARD_IS_CACHE`` unless the syncer flag is set for it);
  ``hosted/owner.json`` makes it server-owned (``BOARD_IS_HOSTED`` unless the
  owner flag or offline maintenance is set for it). The presence of the marker
  file decides; a stale ``owner.json`` still refuses until the server takes it
  over or an admin unlocks it.
- **Confinement.** A write whose resolved path is not under the resolved
  ``.lattice/`` of its board raises ``BoardPathError`` and writes nothing. The
  board is the one set by :func:`board_scope` (``lattice.ops.execute`` sets it
  for the whole operation); outside a scope it is the prefix of the path up to
  its *first* ``.lattice`` component, so ``a/.lattice/../../b/.lattice/x``
  cannot reach a sibling board. Markers and classes are checked against every
  ``.lattice`` directory enclosing the *resolved* target (and the board above),
  so neither a symlink alias into a board nor a board kept under a directory
  that is itself named ``.lattice`` escapes its markers. A path whose written
  and resolved forms both lack a ``.lattice`` component, outside any scope, is
  not a board path and is written unchecked.
- **Flags** are ``contextvars`` values holding the resolved ``.lattice/`` they
  apply to, never process globals: :func:`owning_board` (the server that holds
  the owner lease), :func:`syncing_board` (the cache syncer), and the
  maintenance flag set only by :func:`offline_maintenance` while it holds the
  owner flock. A new thread starts without them.

``fcntl`` is imported only inside the functions that take a hosted lock, so
local Lattice still imports on platforms without it (G-6).
"""

from __future__ import annotations

import contextlib
import contextvars
import json
import os
from collections.abc import Iterator
from dataclasses import dataclass
from enum import Enum
from pathlib import Path, PurePath

from lattice.core.errors import BoardIsCache, BoardIsHosted, BoardPathError, OpError

_LATTICE_DIR = ".lattice"


class PathClass(str, Enum):
    """The class of a path relative to its board's ``.lattice/`` (SPEC §6.1)."""

    DURABLE = "durable"
    WORKSPACE = "workspace"
    RUNTIME = "runtime"
    TEMPORARY = "temporary"
    SERVER_CONTROL = "server_control"
    CACHE_CONTROL = "cache_control"
    UNMANAGED = "unmanaged"


DURABLE_DIRS = frozenset(
    {
        "tasks",
        "events",
        "archive",
        "plans",
        "notes",
        "artifacts",
        "resources",
        "sessions",
        "templates",
    }
)
DURABLE_FILES = frozenset({"config.json", "ids.json", "context.md", ".gitignore"})
RUNTIME_DIRS = frozenset({"locks", "review_state", "tmp-prompts", ".daemon"})
TEMP_PREFIX = ".tmp."

#: Classes whose writes are marker-checked against cache and hosted markers
#: and recorded by the write recorder.
RECORDED_CLASSES = frozenset({PathClass.DURABLE, PathClass.WORKSPACE})


def classify_path(relative: PurePath | str) -> PathClass:
    """Classify a path given relative to its board's ``.lattice/``.

    The board directory itself (``.``) is durable.
    """
    parts = tuple(p for p in PurePath(relative).parts if p != ".")
    if not parts:
        return PathClass.DURABLE
    if parts[-1].startswith(TEMP_PREFIX):
        return PathClass.TEMPORARY
    head = parts[0]
    if head in DURABLE_DIRS or (len(parts) == 1 and head in DURABLE_FILES):
        return PathClass.DURABLE
    if head == "orchestration":
        return PathClass.WORKSPACE
    if head in RUNTIME_DIRS:
        return PathClass.RUNTIME
    if head == "hosted":
        return PathClass.SERVER_CONTROL
    if head == "cache":
        return PathClass.CACHE_CONTROL
    return PathClass.UNMANAGED


# ---------------------------------------------------------------------------
# Flags (contextvars; each holds the resolved .lattice/ it applies to)
# ---------------------------------------------------------------------------

_SCOPE: contextvars.ContextVar[Path | None] = contextvars.ContextVar(
    "lattice_board_scope", default=None
)
_OWNER: contextvars.ContextVar[Path | None] = contextvars.ContextVar(
    "lattice_board_owner", default=None
)
_SYNCER: contextvars.ContextVar[Path | None] = contextvars.ContextVar(
    "lattice_board_syncer", default=None
)
_MAINTENANCE: contextvars.ContextVar[Path | None] = contextvars.ContextVar(
    "lattice_offline_maintenance", default=None
)


@contextlib.contextmanager
def _flag(var: contextvars.ContextVar[Path | None], lattice_dir: Path) -> Iterator[None]:
    token = var.set(Path(lattice_dir).resolve())
    try:
        yield
    finally:
        var.reset(token)


def board_scope(lattice_dir: Path) -> contextlib.AbstractContextManager[None]:
    """Confine every primitive write in this context to *lattice_dir*."""
    return _flag(_SCOPE, lattice_dir)


def owning_board(lattice_dir: Path) -> contextlib.AbstractContextManager[None]:
    """Mark this context as the server that owns *lattice_dir* (holds its lease)."""
    return _flag(_OWNER, lattice_dir)


def syncing_board(lattice_dir: Path) -> contextlib.AbstractContextManager[None]:
    """Mark this context as the cache syncer of *lattice_dir*."""
    return _flag(_SYNCER, lattice_dir)


def _set_for(var: contextvars.ContextVar[Path | None], board: Path) -> bool:
    value = var.get()
    return value is not None and value == board


# ---------------------------------------------------------------------------
# Markers
# ---------------------------------------------------------------------------


def _read_json(path: Path) -> dict:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def board_state(lattice_dir: Path) -> str:
    """``"cache"``, ``"hosted"``, or ``"local"``, from the board's markers."""
    board = Path(lattice_dir)
    if (board / "cache" / "state.json").exists() or (board / "cache" / "applying").exists():
        return "cache"
    if (board / "hosted" / "owner.json").exists():
        return "hosted"
    return "local"


def is_hosted_scaffold(lattice_dir: Path) -> bool:
    """A server project's ``.lattice/`` not initialized yet: ``hosted/``, no ``config.json``."""
    board = Path(lattice_dir)
    return (board / "hosted").is_dir() and not (board / "config.json").exists()


def _cache_error(board: Path, path: Path | None) -> BoardIsCache:
    marker = _read_json(board / "cache" / "state.json") or _read_json(board / "cache" / "applying")
    remote, project = marker.get("remote"), marker.get("project")
    name = f"{remote}/{project}" if remote and project else "a hosted board"
    details = {"board": str(board)}
    if path is not None:
        details["path"] = str(path)
    return BoardIsCache(
        f"this is a read-only mirror of {name}; writes go through the server", details
    )


def _owner_description(board: Path) -> str:
    owner = _read_json(board / "hosted" / "owner.json")
    parts = [str(owner["server_id"])] if owner.get("server_id") else []
    if owner.get("host"):
        parts.append(f"on {owner['host']}")
    if owner.get("pid") is not None:
        parts.append(f"pid {owner['pid']}")
    return f" ({' '.join(parts)})" if parts else ""


def _hosted_error(board: Path, path: Path | None) -> BoardIsHosted:
    details = {"board": str(board)}
    if path is not None:
        details["path"] = str(path)
    return BoardIsHosted(
        f"this board is owned by a Lattice server{_owner_description(board)}; "
        "writes go through the server. Maintenance commands take --offline-maintenance "
        "once the server has stopped or unloaded the project.",
        details,
    )


def _check_markers(board: Path, path: Path | None) -> None:
    state = board_state(board)
    if state == "cache" and not _set_for(_SYNCER, board):
        raise _cache_error(board, path)
    if state == "hosted" and not (_set_for(_OWNER, board) or _set_for(_MAINTENANCE, board)):
        raise _hosted_error(board, path)


def check_board_writable(lattice_dir: Path) -> None:
    """Refuse a board this context may not write (SPEC §3.2 step 1).

    Raises ``BoardIsCache`` or ``BoardIsHosted``; returns for a local board, or
    for a marked board whose syncer, owner, or offline maintenance this is.
    """
    _check_markers(Path(lattice_dir).resolve(), None)


# ---------------------------------------------------------------------------
# The check every primitive runs
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class BoardTarget:
    """A primitive's target, resolved against the board it is confined to.

    ``classes`` holds the target's class relative to every ``.lattice``
    directory that encloses its resolved path (plus the board it is confined
    to), outermost first: normally one entry, more only when a board lives
    under an ancestor that is itself named ``.lattice``. Every one of them is
    checked, so a write can never be classified against the wrong board and
    slip past its markers.
    """

    board: Path  # the resolved .lattice/ the write is confined to
    path: Path  # the target, resolved
    classes: tuple[tuple[Path, PathClass], ...]

    @property
    def path_class(self) -> PathClass:
        """The class relative to the innermost enclosing ``.lattice`` (its own board)."""
        return self.classes[-1][1]

    @property
    def recorded(self) -> bool:
        return any(cls in RECORDED_CLASSES for _board, cls in self.classes)


def locate(path: Path, *, follow: bool = True) -> BoardTarget | None:
    """Resolve *path* against its board, or ``None`` when it is not a board path.

    ``follow=False`` resolves only the parent directory and keeps the final
    component as named, for a mutation of the directory entry itself (removing
    a symlink, never its target).

    Raises ``BoardPathError`` when the resolved path escapes the board.
    """
    absolute = Path(path).absolute()
    resolved = absolute.resolve() if follow else absolute.parent.resolve() / absolute.name
    board = _SCOPE.get()
    if board is None:
        parts = absolute.parts
        if _LATTICE_DIR in parts:
            board = Path(*parts[: parts.index(_LATTICE_DIR) + 1]).resolve()
    if board is not None and resolved != board and not resolved.is_relative_to(board):
        raise BoardPathError(
            f"Refusing to write {path}: it resolves to {resolved}, outside the board {board}.",
            {"reason": "PATH_OUTSIDE_BOARD", "board": str(board)},
        )
    # Boards are found from the resolved target as well as from the path as
    # written, so a symlink alias into a board is still checked as that board.
    enclosing = {c for c in (*resolved.parents, resolved) if c.name == _LATTICE_DIR}
    if board is not None:
        enclosing.add(board)
    if not enclosing:
        return None
    ordered = sorted(enclosing, key=lambda b: len(b.parts))
    classes = tuple((b, classify_path(resolved.relative_to(b))) for b in ordered)
    return BoardTarget(board if board is not None else ordered[-1], resolved, classes)


def check_write(target: BoardTarget) -> None:
    """Refuse a write this context may not make to *target* (SPEC §6.2)."""
    for board, path_class in target.classes:
        if path_class in RECORDED_CLASSES:
            _check_markers(board, target.path)
        elif path_class is PathClass.SERVER_CONTROL and not (
            _set_for(_OWNER, board) or _set_for(_MAINTENANCE, board)
        ):
            raise BoardIsHosted(
                f"Refusing to write {target.path}: hosted/ is written only by the "
                "owning server and by offline maintenance.",
                {"board": str(board), "path": str(target.path)},
            )


# ---------------------------------------------------------------------------
# Owner flock and offline maintenance (hosted-only; fcntl imported here only)
# ---------------------------------------------------------------------------


def _fcntl():  # noqa: ANN202
    try:
        import fcntl
    except ImportError as exc:
        raise OpError(
            "HOSTED_UNSUPPORTED_PLATFORM",
            "Hosted mode needs a POSIX platform (macOS or Linux).",
        ) from exc
    return fcntl


def try_owner_flock(lattice_dir: Path) -> int | None:
    """Take the exclusive owner flock on ``hosted/owner.lock`` without waiting.

    Returns the open descriptor holding it, or ``None`` when another open file
    (any process) holds it. Release with :func:`release_owner_flock`; the
    kernel releases it when the process dies.
    """
    fcntl = _fcntl()
    fd = os.open(Path(lattice_dir) / "hosted" / "owner.lock", os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        os.close(fd)
        return None
    except BaseException:
        os.close(fd)
        raise
    return fd


def release_owner_flock(fd: int) -> None:
    """Release a flock taken by :func:`try_owner_flock`."""
    os.close(fd)


@contextlib.contextmanager
def offline_maintenance(lattice_dir: Path, command: str) -> Iterator[None]:
    """Run a local-only maintenance command on a server project's board (SPEC §3.5).

    Refused with ``BOARD_IS_HOSTED`` while any process holds the owner flock.
    Otherwise takes the flock for the duration, writes ``hosted/maintenance.json``
    (``{at, command}``, so the next project load rotates the epoch), and allows
    durable writes to this board in this context.
    """
    from lattice.core.events import utc_now
    from lattice.storage.fs import atomic_write

    board = Path(lattice_dir).resolve()
    if not (board / "hosted").is_dir():
        raise OpError(
            "VALIDATION_ERROR",
            f"--offline-maintenance is only for a server project's board; "
            f"{board} has no hosted/ directory.",
        )
    fd = try_owner_flock(board)
    if fd is None:
        raise BoardIsHosted(
            f"a running Lattice server holds this board{_owner_description(board)}; "
            "stop the server or unload the project "
            "(lattice server project unload <slug>) before offline maintenance.",
            {"board": str(board)},
        )
    try:
        token = _MAINTENANCE.set(board)
        try:
            atomic_write(
                board / "hosted" / "maintenance.json",
                json.dumps({"at": utc_now(), "command": command}, sort_keys=True, indent=2) + "\n",
            )
            yield
        finally:
            _MAINTENANCE.reset(token)
    finally:
        release_owner_flock(fd)
