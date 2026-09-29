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
_FRAME_RE = re.compile(r"^t\d{4,}\.\d{3}s\.jpg$")

#: The most one ranged response sends; the browser asks for the next range itself.
RANGE_CAP = 1024 * 1024
CHUNK = 64 * 1024
#: A media connection that stops reading is dropped after this many seconds.
SOCKET_TIMEOUT = 15
_RANGE_RE = re.compile(r"^bytes=(\d*)-(\d*)$")

UNSATISFIABLE = "unsatisfiable"


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
        start = int(first)
        if last and int(last) < start:
            return None
        if start >= size:
            return UNSATISFIABLE
        end = min(int(last), size - 1) if last else size - 1
    elif last:
        suffix = int(last)
        if suffix == 0 or size == 0:
            return UNSATISFIABLE
        start, end = max(0, size - suffix), size - 1
    else:
        return None
    return start, min(end, start + cap - 1)


def _open_regular(path: Path, root: Path) -> tuple[int, int] | None:
    """``(fd, size)`` of a regular file directly inside *root*'s tree, never
    through a symlink: ``lstat`` first, then the descriptor must be that same
    file. ``None`` otherwise."""
    try:
        before = os.lstat(path)
        if not stat.S_ISREG(before.st_mode):
            return None
        if not path.parent.resolve(strict=True).is_relative_to(root):
            return None
        fd = os.open(path, os.O_RDONLY)
    except (OSError, ValueError):
        return None
    try:
        after = os.fstat(fd)
    except OSError:
        os.close(fd)
        return None
    if (after.st_dev, after.st_ino) != (before.st_dev, before.st_ino) or not stat.S_ISREG(
        after.st_mode
    ):
        os.close(fd)
        return None
    return fd, after.st_size


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
    lattice_dir = Path(target.lattice_dir)
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
    entry = next((m for m in (snapshot or {}).get("media", []) if m.get("id") == media_id), None)
    if entry is None or entry.get("removed") or entry.get("content_type") not in MEDIA_TYPES:
        _refuse(handler, 404, "NOT_FOUND", "No such media")
        return
    if frame is None:
        file_path = media_path(lattice_dir, issue_id, entry)
        content_type, etag = entry["content_type"], entry.get("sha256")
        filename = file_path.name if file_path else media_id
    else:
        directory = frames_dir(lattice_dir, issue_id, entry)
        file_path = directory / frame if directory else None
        content_type, etag, filename = "image/jpeg", None, f"{media_id}-{frame}"
    root = media_root(lattice_dir.resolve())
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
    quoted = f'"{etag}"' if etag else None
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
            chunk = os.pread(fd, min(CHUNK, remaining), offset)
            if not chunk:
                break
            handler.wfile.write(chunk)
            offset += len(chunk)
            remaining -= len(chunk)
    except (BrokenPipeError, ConnectionResetError, TimeoutError):
        # Browsers cancel range requests constantly while seeking.
        handler.close_connection = True


__all__ = ["MEDIA_ROUTE", "RANGE_CAP", "SOCKET_TIMEOUT", "parse_range", "serve_issue_media"]
