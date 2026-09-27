"""The client cache (SPEC §9.4): a read-only mirror of one hosted project board.

Published interface (H-10c and H-11 build on exactly these):

- :func:`catch_up` ``(hosted_root) -> SyncOutcome``: bring the cache up to the
  server's head.
- :func:`read_lock` ``(hosted_root)``: the cache's shared read lock, held by
  every hosted read from its first directory enumeration through its last
  file read.

Everything else here is private to the client.
"""

from __future__ import annotations

import hashlib
import json
import os
import stat
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Literal

from lattice.core.errors import OpError
from lattice.storage.ownership import PathClass, classify_path

LATTICE_DIR = ".lattice"
BINDING_FILE = ".lattice-remote.json"

#: Classes a sync may write or remove (SPEC §6.1: synced paths).
SYNCED_CLASSES = frozenset({PathClass.DURABLE, PathClass.WORKSPACE})
RUNTIME_DIRS = ("locks", "review_state", "tmp-prompts", ".daemon")
#: The durable directories of a local board (``ensure_lattice_dirs``), so a cache
#: has the same layout even where the server holds no file (AC-9).
STANDARD_DIRS = (
    "tasks",
    "events",
    "archive",
    "archive/tasks",
    "archive/events",
    "archive/notes",
    "archive/plans",
    "artifacts",
    "artifacts/meta",
    "artifacts/payload",
    "notes",
    "plans",
    "resources",
    "sessions",
    "sessions/archive",
    "templates",
)

FILE_MODE = 0o400
DURABLE_DIR_MODE = 0o500
PRIVATE_DIR_MODE = 0o700

OutcomeKind = Literal["applied", "unchanged", "unreachable", "busy", "incomplete"]


@dataclass(frozen=True)
class SyncOutcome:
    """What one :func:`catch_up` did.

    ``head_seq`` and ``synced_at`` describe the cache after the call (``None``
    when it has never completed a sync). ``detail`` is a one-line reason for
    ``unreachable``, ``busy``, and ``incomplete``.
    """

    kind: OutcomeKind
    head_seq: int | None
    synced_at: str | None
    detail: str | None = None


# ---------------------------------------------------------------------------
# Identity: which remote and project a hosted root mirrors
# ---------------------------------------------------------------------------


def _read_json(path: Path) -> dict:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def cache_identity(hosted_root: Path) -> tuple[str, str] | None:
    """``(remote, project)`` from the cache marker, an interrupted apply, or the
    committed binding, in that order; ``None`` when none names both."""
    lattice_dir = Path(hosted_root) / LATTICE_DIR
    for source in (
        lattice_dir / "cache" / "state.json",
        lattice_dir / "cache" / "applying",
        Path(hosted_root) / BINDING_FILE,
    ):
        data = _read_json(source)
        remote, project = data.get("remote"), data.get("project")
        if isinstance(remote, str) and remote and isinstance(project, str) and project:
            return remote, project
    return None


def not_hosted(root: Path) -> OpError:
    return OpError(
        "NOT_HOSTED",
        f"{root} is not a hosted checkout (no cache marker and no {BINDING_FILE}); "
        "nothing was changed.",
        {"root": str(root)},
    )


# ---------------------------------------------------------------------------
# The tamper fingerprint (SPEC §9.4): a stat walk, no reads
# ---------------------------------------------------------------------------


def _walk_durable(lattice_dir: Path) -> list[tuple[str, os.stat_result]]:
    """``(relative path, lstat)`` of every synced-class entry that is not a
    directory, found without following symlinks."""
    found: list[tuple[str, os.stat_result]] = []
    stack = [("", lattice_dir)]
    while stack:
        prefix, directory = stack.pop()
        try:
            entries = list(os.scandir(directory))
        except FileNotFoundError:
            continue
        for entry in entries:
            rel = f"{prefix}{entry.name}"
            if classify_path(rel) not in SYNCED_CLASSES:
                continue
            info = entry.stat(follow_symlinks=False)
            if stat.S_ISDIR(info.st_mode):
                stack.append((rel + "/", Path(entry.path)))
            else:
                found.append((rel, info))
    found.sort()
    return found


def fingerprint(lattice_dir: Path) -> str:
    """SHA-256 over each durable file's path, size, and mtime (lstat; no reads)."""
    digest = hashlib.sha256()
    for rel, info in _walk_durable(lattice_dir):
        digest.update(f"{rel}\0{info.st_size}\0{info.st_mtime_ns}\n".encode())
    return digest.hexdigest()


def durable_files(lattice_dir: Path) -> list[str]:
    """Relative paths of every durable (or workspace) non-directory entry."""
    return [rel for rel, _info in _walk_durable(lattice_dir)]


# ---------------------------------------------------------------------------
# Delta verification (SPEC §9.4)
# ---------------------------------------------------------------------------


def unsafe_path_reason(rel: object, lattice_dir: Path) -> str | None:
    """Why a server-supplied path may not be written, or ``None`` if it may.

    It must be a relative POSIX path of synced class (durable or workspace)
    with no empty, ``.``, or ``..`` component, no backslash or NUL, and no
    existing symlink along it under *lattice_dir*.
    """
    if not isinstance(rel, str) or not rel:
        return "not a path"
    if "\\" in rel or "\0" in rel:
        return "contains a backslash or NUL"
    if rel.startswith("/"):
        return "is absolute"
    parts = rel.split("/")
    if any(part in ("", ".", "..") for part in parts):
        return "has an empty, '.', or '..' component"
    if classify_path(PurePosixPath(rel)) not in SYNCED_CLASSES:
        return f"is not a board path ({classify_path(PurePosixPath(rel)).value})"
    current = lattice_dir
    for part in parts:
        current = current / part
        try:
            info = os.lstat(current)
        except FileNotFoundError:
            break
        if stat.S_ISLNK(info.st_mode):
            return "passes through a symlink"
    return None
