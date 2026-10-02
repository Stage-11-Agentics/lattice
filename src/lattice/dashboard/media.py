"""Issue media over HTTP (LAT-366): the bytes of an issue's photos, videos and frames.

::

    GET /api/issues/<iss_ULID>/media/<med_ULID>
    GET /api/issues/<iss_ULID>/media/<med_ULID>/frames/<tNNNN.NNNs.jpg>

Read-only and local boards only. A file is served only when the issue's
snapshot lists it, not removed, with an accepted type; the path is built from
the validated IDs and the type's extension, never from the request, and a
symlink or anything that is not a regular file inside ``issues/media/`` is
refused. The type sent is the one recorded at attach time, with ``nosniff``
and a sandboxing CSP. One ``Range`` is honoured (206), capped at
:data:`RANGE_CAP` so a paused video stream never holds much; these responses
run outside the dashboard's board lock (see ``server.py``).
"""

from __future__ import annotations

import json
import os
import re
import stat
from pathlib import Path
from typing import Any

from lattice.core.config import issues_enabled
from lattice.core.errors import OpError
from lattice.core.ids import validate_id
from lattice.core.issue_media import MEDIA_TYPES
from lattice.core.issues import issues_disabled_message
from lattice.dashboard import api
from lattice.storage.issue_media import frames_dir, media_path, media_root
from lattice.storage.issues import issues_dir, read_issue_snapshot

#: The two media routes; the groups are checked below, so a malformed ID is a 400.
MEDIA_ROUTE = re.compile(r"^/api/issues/([^/]*)/media/([^/]*)(?:/frames/([^/]*))?$")
_FRAME_RE = re.compile(r"t[0-9]{4,}\.[0-9]{3}s\.jpg\Z")
_SHA256_RE = re.compile(r"[0-9a-f]{64}\Z")
_SAFE_FILENAME_RE = re.compile(r"[A-Za-z0-9._-]+\Z")

#: The most one ranged response sends; the browser asks for the next range itself.
RANGE_CAP = 1024 * 1024
CHUNK = 64 * 1024
#: A media connection that stops reading is dropped after this many seconds.
SOCKET_TIMEOUT = 15
_RANGE_RE = re.compile(r"^bytes=([0-9]*)-([0-9]*)$")

_DIR_OPEN_FLAGS = (
    os.O_RDONLY
    | getattr(os, "O_DIRECTORY", 0)
    | getattr(os, "O_NOFOLLOW", 0)
    | getattr(os, "O_CLOEXEC", 0)
)
_FILE_OPEN_FLAGS = (
    os.O_RDONLY
    | getattr(os, "O_NOFOLLOW", 0)
    | getattr(os, "O_CLOEXEC", 0)
    | getattr(os, "O_NONBLOCK", 0)
)

UNSATISFIABLE = "unsatisfiable"


def _decimal_less(left: str, right: str) -> bool:
    """Compare two ASCII decimal strings without converting unbounded input."""
    left = left.lstrip("0") or "0"
    right = right.lstrip("0") or "0"
    return len(left) < len(right) or (len(left) == len(right) and left < right)


def _bounded_decimal(value: str, maximum: int) -> int:
    """Convert a decimal string while saturating values above *maximum*."""
    value = value.lstrip("0") or "0"
    ceiling = str(maximum)
    if len(value) > len(ceiling) or (len(value) == len(ceiling) and value > ceiling):
        return maximum
    return int(value)


def parse_range(header: str | None, size: int, cap: int = RANGE_CAP) -> Any:
    """The byte range to send for a ``Range`` header: ``(start, end)`` inclusive,
    ``None`` to send the whole file (no header, or one this ignores: several
    ranges or a malformed one, as RFC 9110 allows), or :data:`UNSATISFIABLE`.
    A range longer than *cap* is shortened to *cap* bytes."""
    if not header:
        return None
    match = _RANGE_RE.fullmatch(header.strip())
    if match is None:
        return None
    first, last = match.groups()
    if first:
        if last and _decimal_less(last, first):
            return None
        start = _bounded_decimal(first, size)
        if start >= size:
            return UNSATISFIABLE
        end = _bounded_decimal(last, size - 1) if last else size - 1
    elif last:
        suffix = _bounded_decimal(last, size)
        if suffix == 0 or size == 0:
            return UNSATISFIABLE
        start, end = max(0, size - suffix), size - 1
    else:
        return None
    return start, min(end, start + cap - 1)


def _open_regular(path: Path, root: Path) -> tuple[int, int] | None:
    """``(fd, size)`` of a regular file reached through real directories.

    Keep each directory descriptor while opening its child relative to that
    descriptor when the platform supports it. Elsewhere, check and resolve
    each path component before and after opening, and verify the opened file's
    identity. The fallback preserves local media serving on platforms without
    ``dir_fd`` while refusing detected links and replacements.
    """
    try:
        relative = path.relative_to(root)
        if len(relative.parts) not in (2, 3) or root.resolve(strict=True) != root:
            return None
    except (OSError, ValueError, RuntimeError):
        return None

    if (
        getattr(os, "open", None) not in getattr(os, "supports_dir_fd", set())
        or not hasattr(os, "O_DIRECTORY")
        or not hasattr(os, "O_NOFOLLOW")
    ):
        return _open_regular_by_path(path, root, relative)

    directory_fd: int | None = None
    file_fd: int | None = None
    try:
        if not root.is_absolute():
            return None
        directory_fd = os.open(os.sep, _DIR_OPEN_FLAGS)
        for component in (*root.parts[1:], *relative.parts[:-1]):
            child_fd = os.open(component, _DIR_OPEN_FLAGS, dir_fd=directory_fd)
            previous_fd = directory_fd
            directory_fd = child_fd
            os.close(previous_fd)
        file_fd = os.open(relative.parts[-1], _FILE_OPEN_FLAGS, dir_fd=directory_fd)
        info = os.fstat(file_fd)
        if not stat.S_ISREG(info.st_mode):
            return None
        result = (file_fd, info.st_size)
        file_fd = None
        return result
    except OSError:
        return None
    finally:
        if file_fd is not None:
            os.close(file_fd)
        if directory_fd is not None:
            os.close(directory_fd)


def _directory_chain_identity(root: Path, relative: Path) -> tuple[tuple[int, int], ...] | None:
    """Identity of real parent directories under root, or None if unsafe."""
    try:
        if root.resolve(strict=True) != root:
            return None
        parent = root
        info = os.lstat(parent)
        if not stat.S_ISDIR(info.st_mode):
            return None
        identities = [(info.st_dev, info.st_ino)]
        for component in relative.parts[:-1]:
            parent /= component
            info = os.lstat(parent)
            if not stat.S_ISDIR(info.st_mode):
                return None
            identities.append((info.st_dev, info.st_ino))
        if not parent.resolve(strict=True).is_relative_to(root):
            return None
        return tuple(identities)
    except (OSError, ValueError, RuntimeError):
        return None


def _open_regular_by_path(path: Path, root: Path, relative: Path) -> tuple[int, int] | None:
    """Portable fallback for platforms without descriptor-relative ``os.open``.

    Rechecking the chain and comparing the opened inode catches static links
    and ordinary path replacement. Unlike the dirfd path, the standard library
    cannot make intermediate directory traversal atomic on those platforms.
    """
    before_chain = _directory_chain_identity(root, relative)
    if before_chain is None:
        return None
    try:
        before = os.lstat(path)
        if not stat.S_ISREG(before.st_mode):
            return None
        fd = os.open(
            path,
            os.O_RDONLY
            | getattr(os, "O_BINARY", 0)
            | getattr(os, "O_NOFOLLOW", 0)
            | getattr(os, "O_NONBLOCK", 0),
        )
    except OSError:
        return None
    try:
        opened = os.fstat(fd)
        current = os.lstat(path)
        if (
            not stat.S_ISREG(opened.st_mode)
            or not stat.S_ISREG(current.st_mode)
            or (opened.st_dev, opened.st_ino) != (before.st_dev, before.st_ino)
            or (current.st_dev, current.st_ino) != (opened.st_dev, opened.st_ino)
            or _directory_chain_identity(root, relative) != before_chain
        ):
            os.close(fd)
            return None
        return fd, opened.st_size
    except OSError:
        os.close(fd)
        return None


def _refuse(handler: Any, status: int, code: str, message: str) -> None:
    handler._send(api.error(status, code, message))


def serve_issue_media(handler: Any, target: Any, path: str) -> None:
    """Answer one media GET on *handler* for the dashboard board *target*."""
    match = MEDIA_ROUTE.fullmatch(path)
    if match is None:
        _refuse(handler, 404, "NOT_FOUND", f"Not found: {path}")
        return
    issue_id, media_id, frame = match.groups()
    if not validate_id(issue_id, "iss") or not validate_id(media_id, "med"):
        _refuse(handler, 400, "INVALID_ID", "Invalid issue or media ID")
        return
    if frame is not None and not _FRAME_RE.fullmatch(frame):
        _refuse(handler, 404, "NOT_FOUND", f"Not found: {path}")
        return
    if target.hosted:
        _refuse(handler, 400, "LOCAL_ONLY", "The issue log works only on local boards for now.")
        return
    # Resolve once so both the constructed path and its confinement root use
    # the same spelling when LATTICE_ROOT or .lattice is a symlink.
    lattice_dir = Path(target.lattice_dir).resolve()
    try:
        config = json.loads((lattice_dir / "config.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        _refuse(handler, 500, "INTEGRITY_ERROR", "Cannot read .lattice/config.json")
        return
    if not issues_enabled(config):
        exists = issues_dir(lattice_dir).is_dir()
        _refuse(handler, 409, "ISSUES_DISABLED", issues_disabled_message(exists))
        return
    try:
        snapshot = read_issue_snapshot(lattice_dir, issue_id)
    except OpError as exc:
        _refuse(handler, 500, exc.code, exc.message)
        return
    if snapshot is not None and not isinstance(snapshot, dict):
        _refuse(handler, 500, "INTEGRITY_ERROR", "Issue snapshot must be an object")
        return
    entries = (snapshot or {}).get("media", [])
    if not isinstance(entries, list) or any(not isinstance(m, dict) for m in entries):
        _refuse(handler, 500, "INTEGRITY_ERROR", "Issue media entries must be objects")
        return
    for media_entry in entries:
        content_type = media_entry.get("content_type")
        if content_type is not None and not isinstance(content_type, str):
            _refuse(handler, 500, "INTEGRITY_ERROR", "Issue media content_type must be a string")
            return
    entry = next((m for m in entries if m.get("id") == media_id), None)
    if entry is None or entry.get("removed") or entry.get("content_type") not in MEDIA_TYPES:
        _refuse(handler, 404, "NOT_FOUND", "No such media")
        return
    if frame is None:
        file_path = media_path(lattice_dir, issue_id, entry)
        digest = entry.get("sha256")
        if not isinstance(digest, str) or not _SHA256_RE.fullmatch(digest):
            _refuse(handler, 500, "INTEGRITY_ERROR", "Issue media has an invalid SHA-256 digest.")
            return
        content_type, etag = entry["content_type"], digest
        filename = file_path.name if file_path else media_id
    else:
        directory = frames_dir(lattice_dir, issue_id, entry)
        file_path = directory / frame if directory else None
        content_type, etag, filename = "image/jpeg", None, f"{media_id}-{frame}"
    root = media_root(lattice_dir)
    opened = _open_regular(file_path, root) if file_path is not None else None
    if opened is None:
        _refuse(handler, 404, "NOT_FOUND", "No such media")
        return
    fd, size = opened
    try:
        _send_file(handler, fd, size, content_type, etag, filename)
    finally:
        os.close(fd)


def _send_file(
    handler: Any, fd: int, size: int, content_type: str, etag: str | None, filename: str
) -> None:
    # Recheck at the header boundary: ``send_header`` does not reject CR/LF,
    # and persisted board fields are untrusted even after snapshot replay.
    quoted = f'"{etag}"' if isinstance(etag, str) and _SHA256_RE.fullmatch(etag) else None
    if not _SAFE_FILENAME_RE.fullmatch(filename):
        filename = "media"
    if quoted and handler.headers.get("If-None-Match") == quoted:
        handler.send_response(304)
        handler.send_header("ETag", quoted)
        handler.end_headers()
        return
    wanted = parse_range(handler.headers.get("Range"), size)
    if wanted == UNSATISFIABLE:
        handler.send_response(416)
        handler.send_header("Content-Range", f"bytes */{size}")
        handler.send_header("Content-Length", "0")
        handler.end_headers()
        return
    start, end = wanted if wanted else (0, size - 1)
    length = max(0, end - start + 1)
    handler.send_response(206 if wanted else 200)
    handler.send_header("Content-Type", content_type)
    handler.send_header("Content-Length", str(length))
    if wanted:
        handler.send_header("Content-Range", f"bytes {start}-{end}/{size}")
    handler.send_header("Accept-Ranges", "bytes")
    if quoted:
        handler.send_header("ETag", quoted)
    handler.send_header("Cache-Control", "no-cache")
    handler.send_header("X-Content-Type-Options", "nosniff")
    handler.send_header("Content-Security-Policy", "default-src 'none'; sandbox")
    handler.send_header("Content-Disposition", f'inline; filename="{filename}"')
    handler.end_headers()
    offset, remaining = start, length
    try:
        while remaining > 0:
            chunk = _read_at(fd, min(CHUNK, remaining), offset)
            if not chunk:
                break
            handler.wfile.write(chunk)
            offset += len(chunk)
            remaining -= len(chunk)
    except (BrokenPipeError, ConnectionResetError, TimeoutError):
        # Browsers cancel range requests constantly while seeking.
        handler.close_connection = True


def _read_at(fd: int, size: int, offset: int) -> bytes:
    """Read at *offset* using pread where available, with a portable fallback.

    Every response owns its descriptor, so its seek position cannot race with
    another request when the fallback uses ``lseek`` followed by ``read``.
    """
    pread = getattr(os, "pread", None)
    if pread is not None:
        return pread(fd, size, offset)
    os.lseek(fd, offset, os.SEEK_SET)
    return os.read(fd, size)


__all__ = ["MEDIA_ROUTE", "RANGE_CAP", "SOCKET_TIMEOUT", "parse_range", "serve_issue_media"]
