"""Which checkouts are hosted, and where their board lives (SPEC §9.2, §9.3).

``find_root`` (``storage/fs.py``) finds a root: a directory holding ``.lattice/``
or the committed binding ``.lattice-remote.json``, after the linked-worktree
jump, so every worktree of a clone shares one root. :func:`classify` then says
whether that root is **hosted**:

1. The machine-local marker (``.lattice/cache/state.json`` or
   ``cache/applying``) routes by itself, whatever branch is checked out.
2. The committed binding, beside no ``.lattice/`` or a ``.lattice/`` holding no
   durable file (runtime leftovers after git removed a tracked board), is
   adopted as an empty cache; the first sync fills it.

A binding beside a local board, or a marker naming another remote or project
than the binding, is ``BINDING_CONFLICT``, and so is a bound or marked root whose
``.lattice`` or ``.lattice/cache`` is a symlink or not a directory (a clone can
commit one): the client never writes through it (``lattice.remote.cache_paths``).
Everything else is a local board.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path

from lattice.core.errors import OpError
from lattice.storage.fs import BINDING_FILE, LATTICE_DIR

MOVE_GUIDE = "the hosted guide's 'Moving a board' steps (docs/hosted)"


@dataclass(frozen=True)
class Hosted:
    """A hosted root: the checkout whose ``.lattice/`` is the cache of
    ``remote``/``project``."""

    root: Path
    remote: str
    project: str

    @property
    def label(self) -> str:
        return f"{self.remote}/{self.project}"

    @property
    def lattice_dir(self) -> Path:
        return self.root / LATTICE_DIR


def _identity(path: Path) -> tuple[str, str] | None:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    remote, project = data.get("remote"), data.get("project")
    if isinstance(remote, str) and remote and isinstance(project, str) and project:
        return remote, project
    return None


def read_binding(root: Path) -> tuple[str, str] | None:
    """``(remote, project)`` from ``<root>/.lattice-remote.json``, or ``None``."""
    return _identity(Path(root) / BINDING_FILE)


def marker_identity(root: Path) -> tuple[str, str] | None:
    """``(remote, project)`` from the cache marker, or ``None`` when there is none."""
    cache_dir = Path(root) / LATTICE_DIR / "cache"
    for name in ("state.json", "applying"):
        identity = _identity(cache_dir / name)
        if identity is not None:
            return identity
    return None


def has_marker(root: Path) -> bool:
    cache_dir = Path(root) / LATTICE_DIR / "cache"
    return (cache_dir / "state.json").exists() or (cache_dir / "applying").exists()


def holds_local_board(root: Path) -> bool:
    """Whether ``<root>/.lattice/`` holds a durable file and no cache marker."""
    lattice_dir = Path(root) / LATTICE_DIR
    if not lattice_dir.is_dir() or has_marker(root):
        return False
    from lattice.remote.cache import synced_files

    return bool(synced_files(lattice_dir))


def classify(root: Path) -> Hosted | None:
    """The hosted identity of *root* (a ``find_root`` result), or ``None`` for a
    local board. Raises ``BINDING_CONFLICT`` for the two cases §9.3 names, and
    for a bound or marked root whose cache is not a real directory."""
    from lattice.remote.cache_paths import require_safe_layout

    root = Path(root)
    binding_path = root / BINDING_FILE
    marker = marker_identity(root)
    if marker is not None or binding_path.is_file():
        require_safe_layout(root)
    if marker is not None:
        binding = read_binding(root) if binding_path.is_file() else None
        if binding is not None and binding != marker:
            raise OpError(
                "BINDING_CONFLICT",
                f"{root} holds a cache of {marker[0]}/{marker[1]}, but its "
                f"{BINDING_FILE} names {binding[0]}/{binding[1]}. To rebind this checkout, "
                "run 'lattice cache clear --forget' and then any lattice command.",
                {"root": str(root), "cache": "/".join(marker), "binding": "/".join(binding)},
            )
        return Hosted(root, *marker)
    if not binding_path.is_file():
        return None
    binding = read_binding(root)
    if binding is None:
        raise OpError(
            "VALIDATION_ERROR",
            f"{binding_path} must be a JSON object with string keys 'remote' and 'project'.",
            {"root": str(root)},
        )
    if holds_local_board(root):
        raise OpError(
            "BINDING_CONFLICT",
            f"{root} is bound to {binding[0]}/{binding[1]} ({BINDING_FILE}) but also holds "
            f"a local board in {LATTICE_DIR}/. Move the board to the server first: "
            f"follow {MOVE_GUIDE}.",
            {"root": str(root), "binding": "/".join(binding)},
        )
    return Hosted(root, *binding)


def hosted_supported() -> bool:
    """Hosted mode needs a POSIX platform with ``fcntl`` (SPEC §6.2)."""
    if os.name != "posix":
        return False
    try:
        import fcntl  # noqa: F401
    except ImportError:
        return False
    return True


def require_supported(what: str = "hosted mode") -> None:
    """Raise ``HOSTED_UNSUPPORTED_PLATFORM`` off POSIX (SPEC §6.2)."""
    if not hosted_supported():
        raise OpError(
            "HOSTED_UNSUPPORTED_PLATFORM",
            f"{what} needs macOS or Linux (a POSIX platform with fcntl); local Lattice "
            "boards keep working here.",
        )


def hosted_root(start: Path) -> Hosted | None:
    """The hosted root a command started in *start* belongs to, or ``None``.

    Raises ``BINDING_CONFLICT``, ``HOSTED_UNSUPPORTED_PLATFORM``, and
    ``NOT_INITIALIZED`` (an invalid ``LATTICE_ROOT``).
    """
    from lattice.storage.fs import LatticeRootError, find_root

    try:
        root = find_root(Path(start))
    except LatticeRootError as exc:
        raise OpError("NOT_INITIALIZED", str(exc)) from exc
    if root is None:
        return None
    hosted = classify(root)
    if hosted is not None:
        require_supported()
    return hosted
