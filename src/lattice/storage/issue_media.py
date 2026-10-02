"""Issue media files (LAT-366).

::

    issues/media/<iss_ULID>/<med_ULID>.<ext>                  the file (extension from its type)
    issues/media/<iss_ULID>/<med_ULID>.frames/t0012.500s.jpg   a video's frames (derived)

Every path is built from a validated ``iss_`` ID, a validated ``med_`` ID and
an extension from ``core.issue_media.MEDIA_TYPES``; a frame's name from an
integer time. The original filename is metadata and never part of a path.
The readers call no writer, so ``issue media``, ``issue show`` and the
dashboard can use them.
"""

from __future__ import annotations

import os
import stat
from collections.abc import Mapping
from pathlib import Path

from lattice.core.ids import validate_id
from lattice.core.issue_media import frame_name, media_ext, parse_frame_name
from lattice.storage.fs import atomic_write, ensure_dir, remove_dir, unlink_entry
from lattice.storage.issues import issues_dir

MEDIA_DIR = "media"

# ---------------------------------------------------------------------------
# Paths and readers
# ---------------------------------------------------------------------------


def media_root(lattice_dir: Path) -> Path:
    return issues_dir(lattice_dir) / MEDIA_DIR


def _issue_media_dir(lattice_dir: Path, issue_id: str) -> Path | None:
    if not validate_id(issue_id, "iss"):
        return None
    return media_root(lattice_dir) / issue_id


def media_path(lattice_dir: Path, issue_id: str, entry: Mapping) -> Path | None:
    """Where *entry*'s file lives; ``None`` when an ID or its type is not valid."""
    directory = _issue_media_dir(lattice_dir, issue_id)
    media_id = entry.get("id")
    ext = media_ext(str(entry.get("content_type")))
    if directory is None or not validate_id(media_id, "med") or ext is None:
        return None
    return directory / f"{media_id}{ext}"


def frames_dir(lattice_dir: Path, issue_id: str, entry: Mapping) -> Path | None:
    """The directory of *entry*'s frames; ``None`` when an ID is not valid."""
    directory = _issue_media_dir(lattice_dir, issue_id)
    media_id = entry.get("id")
    if directory is None or not validate_id(media_id, "med"):
        return None
    return directory / f"{media_id}.frames"


def _is_regular(path: Path) -> bool:
    """A regular file, not through a symlink."""
    try:
        return stat.S_ISREG(os.lstat(path).st_mode)
    except OSError:
        return False


def list_frames(lattice_dir: Path, issue_id: str, entry: Mapping) -> list[tuple[int, Path]]:
    """*entry*'s frames as ``(t_ms, path)``, by time. Names that are not frame
    names, and anything that is not a regular file, are ignored."""
    directory = frames_dir(lattice_dir, issue_id, entry)
    if directory is None or os.path.islink(directory) or not directory.is_dir():
        return []
    frames = []
    for child in directory.iterdir():
        t_ms = parse_frame_name(child.name)
        if t_ms is not None and _is_regular(child):
            frames.append((t_ms, child))
    return sorted(frames)


def media_views(lattice_dir: Path, snapshot: Mapping) -> list[dict]:
    """The snapshot's media entries with ``path``, ``missing`` and ``frames``.

    A removed entry has ``path: null`` and no frames. A present entry whose
    file is absent (media git-ignored on another machine) is ``missing``.
    """
    issue_id = str(snapshot.get("id"))
    views = []
    for entry in snapshot.get("media", []):
        view = dict(entry)
        if entry.get("removed"):
            view.update(path=None, missing=False, frames=[])
        else:
            path = media_path(lattice_dir, issue_id, entry)
            view["path"] = str(path) if path is not None else None
            view["missing"] = path is None or not _is_regular(path)
            view["frames"] = [
                {"t_ms": t_ms, "path": str(p)}
                for t_ms, p in list_frames(lattice_dir, issue_id, entry)
            ]
        views.append(view)
    return views


# ---------------------------------------------------------------------------
# Writers (operations only)
# ---------------------------------------------------------------------------


def store_media(
    lattice_dir: Path,
    issue_id: str,
    entry: Mapping,
    content: bytes,
    frames: list[tuple[int, bytes]],
) -> None:
    """Write one media file and its frames. *entry* carries ``id`` and ``content_type``."""
    path = media_path(lattice_dir, issue_id, entry)
    if path is None:
        raise ValueError(f"invalid media entry for {issue_id}: {entry.get('id')}")
    ensure_dir(path.parent)
    atomic_write(path, content)
    if frames:
        directory = frames_dir(lattice_dir, issue_id, entry)
        assert directory is not None
        ensure_dir(directory)
        for t_ms, data in frames:
            atomic_write(directory / frame_name(t_ms), data)


def _remove_entry(path: Path) -> bool:
    if os.path.lexists(path):
        unlink_entry(path)
        return True
    return False


def delete_media_files(lattice_dir: Path, issue_id: str, entry: Mapping) -> int:
    """Delete *entry*'s frames, their directory and its file; never follows a
    link. Removes the issue's media directory when it is left empty. Returns
    the number of frames deleted."""
    deleted = 0
    directory = frames_dir(lattice_dir, issue_id, entry)
    if directory is not None and os.path.lexists(directory):
        if os.path.islink(directory) or not directory.is_dir():
            unlink_entry(directory)
        else:
            for child in sorted(directory.iterdir()):
                if child.is_dir() and not child.is_symlink():
                    continue
                unlink_entry(child)
                deleted += parse_frame_name(child.name) is not None
            if not any(directory.iterdir()):
                remove_dir(directory)
    path = media_path(lattice_dir, issue_id, entry)
    if path is not None:
        _remove_entry(path)
    parent = _issue_media_dir(lattice_dir, issue_id)
    issues = issues_dir(lattice_dir)
    candidates = [
        parent,
        media_root(lattice_dir),
        issues / "events",
        issues,
    ]
    for directory in candidates:
        if (
            directory is not None
            and directory.is_dir()
            and not os.path.islink(directory)
            and not any(directory.iterdir())
        ):
            remove_dir(directory)
    return deleted


__all__ = [
    "MEDIA_DIR",
    "delete_media_files",
    "frames_dir",
    "list_frames",
    "media_path",
    "media_root",
    "media_views",
    "store_media",
]
