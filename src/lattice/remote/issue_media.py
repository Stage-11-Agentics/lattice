"""Private, on-demand copies of issue media in a hosted checkout.

This cache is separate from ordinary board sync. Every path is opened beneath
``.lattice/cache`` through :mod:`remote.cache_paths`; the server response and
the bytes are verified before a path is returned to a command.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import stat
import urllib.parse
from collections.abc import Collection
from pathlib import Path

from lattice.core.errors import OpError
from lattice.core.ids import validate_id
from lattice.core.issue_media import frame_name, media_ext, parse_frame_name
from lattice.remote import cache_paths, http
from lattice.remote.client import get_json, server_unreachable

MAX_CACHE_BYTES = 1024 * 1024 * 1024


def _endpoint(project: str, suffix: str) -> str:
    return f"/v1/projects/{urllib.parse.quote(project, safe='')}/issues/media/{suffix}"


def availability(remote: http.Remote, project: str, issue_ids: list[str]) -> dict[str, list[dict]]:
    if not issue_ids:
        return {}
    for issue_id in issue_ids:
        if not validate_id(issue_id, "iss"):
            raise OpError(
                "INTEGRITY_ERROR", "issue-media availability received an invalid issue ID."
            )
    result: dict[str, list[dict]] = {}
    for offset in range(0, len(issue_ids), 100):
        batch = issue_ids[offset : offset + 100]
        query = "&".join(f"issue={urllib.parse.quote(issue_id, safe='')}" for issue_id in batch)
        payload = get_json(remote, _endpoint(project, "availability") + "?" + query)
        issues = payload.get("issues") if isinstance(payload, dict) else None
        expected = set(batch)
        if (
            not isinstance(issues, dict)
            or set(issues) != expected
            or any(
                not validate_id(issue_id, "iss") or not isinstance(rows, list)
                for issue_id, rows in issues.items()
            )
        ):
            raise OpError(
                "INTEGRITY_ERROR", "server returned invalid issue-media availability metadata."
            )
        result.update(issues)
    return result


def _safe_component(value: str, kind: str) -> None:
    if not validate_id(value, kind):
        raise OpError("INTEGRITY_ERROR", f"issue-media {kind} is invalid")


def _directory(
    root: Path,
    project: str,
    issue_id: str,
    media_id: str,
    *tail: str,
    create: bool = True,
):
    _safe_component(issue_id, "iss")
    _safe_component(media_id, "med")
    if not project or project in {".", ".."} or "/" in project or "\\" in project:
        raise OpError("INTEGRITY_ERROR", "project slug is invalid for the issue-media cache")
    return cache_paths.opened_dir(
        root,
        ".lattice",
        "cache",
        "issue-media",
        project,
        issue_id,
        media_id,
        *tail,
        create=create,
    )


def _safe_read(
    root: Path, project: str, issue_id: str, media_id: str, name: str, *, frame: bool = False
) -> bytes | None:
    parent = ("frames",) if frame else ()
    try:
        with _directory(root, project, issue_id, media_id, *parent, create=False) as fd:
            return cache_paths.read_file(fd, name)
    except FileNotFoundError:
        return None


def _safe_write(
    root: Path,
    project: str,
    issue_id: str,
    media_id: str,
    name: str,
    data: bytes,
    *,
    frame: bool = False,
) -> Path:
    parent = ("frames",) if frame else ()
    with _directory(root, project, issue_id, media_id, *parent) as fd:
        cache_paths.write_file(fd, name, data, mode=0o600)
    return (
        root
        / ".lattice"
        / "cache"
        / "issue-media"
        / project
        / issue_id
        / media_id
        / Path(*parent)
        / name
    )


def _verified_cached(
    root: Path,
    project: str,
    issue_id: str,
    media_id: str,
    name: str,
    sha256: str,
    size_bytes: int,
    *,
    frame: bool = False,
) -> Path | None:
    try:
        data = _safe_read(root, project, issue_id, media_id, name, frame=frame)
    except OSError:
        # A symlink or a non-directory where a cache object belongs is not a
        # verified copy; it is never read, and never a reason to fail a read.
        return None
    if data is None or len(data) != size_bytes or hashlib.sha256(data).hexdigest() != sha256:
        return None
    try:
        with _directory(
            root, project, issue_id, media_id, *(("frames",) if frame else ()), create=False
        ) as fd:
            os.utime(name, None, dir_fd=fd, follow_symlinks=False)
    except OSError:
        pass
    path = root / ".lattice" / "cache" / "issue-media" / project / issue_id / media_id
    return path / ("frames" if frame else "") / name


def _remove_entry_at(parent_fd: int, name: str) -> None:
    """Remove an entry using its parent descriptor; never follow symlinks."""
    try:
        info = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
    except FileNotFoundError:
        return
    if stat.S_ISDIR(info.st_mode):
        flags = os.O_RDONLY | os.O_DIRECTORY | getattr(os, "O_NOFOLLOW", 0)
        child_fd = os.open(name, flags, dir_fd=parent_fd)
        try:
            for child in os.listdir(child_fd):
                _remove_entry_at(child_fd, child)
        finally:
            os.close(child_fd)
        os.rmdir(name, dir_fd=parent_fd)
    else:
        os.unlink(name, dir_fd=parent_fd)


def _evict(root: Path, project: str, issue_id: str, media_id: str) -> None:
    _safe_component(issue_id, "iss")
    _safe_component(media_id, "med")
    if not project or project in {".", ".."} or "/" in project or "\\" in project:
        raise OpError("INTEGRITY_ERROR", "project slug is invalid for the issue-media cache")
    try:
        with cache_paths.opened_dir(
            root, ".lattice", "cache", "issue-media", project, issue_id, create=False
        ) as parent_fd:
            _remove_entry_at(parent_fd, media_id)
    except FileNotFoundError:
        return


def _enforce_lru(root: Path, keep: Collection[tuple[str, ...]] = ()) -> None:
    """Evict least-recently-used files until the cache fits :data:`MAX_CACHE_BYTES`.

    *keep* holds ``(project, issue, media)`` entries: what the command just
    fetched or verified for its caller is never evicted, so a path it returns
    always exists.
    """
    try:
        root_fd = cache_paths.open_dir(root, ".lattice", "cache", "issue-media", create=False)
    except FileNotFoundError:
        return
    files: list[tuple[int, int, tuple[str, ...]]] = []
    total = 0
    directory_flags = os.O_RDONLY | os.O_DIRECTORY | getattr(os, "O_NOFOLLOW", 0)

    def walk(dir_fd: int, prefix: tuple[str, ...]) -> None:
        nonlocal total
        for name in os.listdir(dir_fd):
            try:
                info = os.stat(name, dir_fd=dir_fd, follow_symlinks=False)
            except OSError:
                continue
            if stat.S_ISREG(info.st_mode):
                total += info.st_size
                files.append((info.st_atime_ns, info.st_size, (*prefix, name)))
            elif stat.S_ISDIR(info.st_mode):
                try:
                    child_fd = os.open(name, directory_flags, dir_fd=dir_fd)
                except OSError:
                    continue
                try:
                    walk(child_fd, (*prefix, name))
                finally:
                    os.close(child_fd)

    try:
        walk(root_fd, ())
    finally:
        os.close(root_fd)
    for _atime, size, parts in sorted(files):
        if total <= MAX_CACHE_BYTES:
            break
        if parts[:3] in keep:
            continue
        try:
            with cache_paths.opened_dir(
                root, ".lattice", "cache", "issue-media", *parts[:-1], create=False
            ) as parent_fd:
                cache_paths.remove_file(parent_fd, parts[-1])
        except OSError:
            continue
        total -= size


def _cached_frames(root: Path, project: str, issue_id: str, media_id: str) -> list[dict]:
    """Find previously fetched frames from their private hash metadata."""
    frames = []
    try:
        with _directory(root, project, issue_id, media_id, "frames", create=False) as fd:
            names = os.listdir(fd)
            for metadata_name in names:
                if not metadata_name.endswith(".meta"):
                    continue
                filename = metadata_name[:-5]
                t_ms = parse_frame_name(filename)
                if t_ms is None:
                    continue
                try:
                    raw = cache_paths.read_file(fd, metadata_name)
                except OSError:
                    continue  # a symlink where metadata belongs: not ours, not read
                if raw is None:
                    continue
                try:
                    metadata = json.loads(raw)
                except (UnicodeDecodeError, ValueError):
                    continue
                if not isinstance(metadata, dict):
                    continue
                sha256 = metadata.get("sha256")
                size_bytes = metadata.get("size_bytes")
                if (
                    metadata.get("t_ms") != t_ms
                    or not isinstance(sha256, str)
                    or not re.fullmatch(r"[0-9a-f]{64}", sha256)
                    or isinstance(size_bytes, bool)
                    or not isinstance(size_bytes, int)
                    or size_bytes < 0
                ):
                    continue
                path = _verified_cached(
                    root,
                    project,
                    issue_id,
                    media_id,
                    filename,
                    sha256,
                    size_bytes,
                    frame=True,
                )
                if path is not None:
                    frames.append(
                        {
                            "t_ms": t_ms,
                            "sha256": sha256,
                            "size_bytes": size_bytes,
                            "path": str(path),
                            "available": "local",
                            "missing": False,
                        }
                    )
    except FileNotFoundError:
        return []
    return sorted(frames, key=lambda row: row["t_ms"])


def _write_frame_metadata(
    root: Path,
    project: str,
    issue_id: str,
    media_id: str,
    name: str,
    t_ms: int,
    sha256: str,
    size_bytes: int,
) -> None:
    body = json.dumps(
        {"t_ms": t_ms, "sha256": sha256, "size_bytes": size_bytes}, sort_keys=True
    ).encode("utf-8")
    with _directory(root, project, issue_id, media_id, "frames") as fd:
        cache_paths.write_file(fd, f"{name}.meta", body, mode=0o600)


def annotate_views(root: Path, remote: http.Remote, project: str, views: list[dict]) -> list[dict]:
    """Attach verified cache/remote availability and safe paths to issue views."""
    issue_ids = [row["id"] for row in views if validate_id(row.get("id"), "iss")]
    server_available = True
    try:
        remote_rows = availability(remote, project, issue_ids)
    except OpError as exc:
        if exc.code != "SERVER_UNREACHABLE":
            raise
        remote_rows = {}
        server_available = False
    for view in views:
        issue_id = view.get("id")
        if not validate_id(issue_id, "iss"):
            continue
        by_media = {
            item.get("media_id"): item
            for item in remote_rows.get(issue_id, [])
            if isinstance(item, dict)
        }
        for entry in view.get("media", []):
            media_id = entry.get("id")
            if entry.get("removed"):
                if validate_id(media_id, "med"):
                    _evict(root, project, issue_id, media_id)
                entry.update(path=None, missing=True, available="missing", frames=[])
                continue
            if not validate_id(media_id, "med"):
                entry.update(path=None, missing=True, available="missing", frames=[])
                continue
            metadata = by_media.get(media_id)
            sha = entry.get("sha256")
            size = entry.get("size_bytes")
            ext = media_ext(str(entry.get("content_type")))
            name = f"{media_id}{ext}" if ext else ""
            local_path = None
            valid_local_metadata = (
                isinstance(sha, str)
                and re.fullmatch(r"[0-9a-f]{64}", sha) is not None
                and isinstance(size, int)
                and not isinstance(size, bool)
                and size >= 0
                and bool(ext)
            )
            if valid_local_metadata:
                local_path = _verified_cached(root, project, issue_id, media_id, name, sha, size)
            remote_present = (
                isinstance(metadata, dict)
                and metadata.get("sha256") == sha
                and metadata.get("size_bytes") == size
                and metadata.get("content_type") == entry.get("content_type")
            )
            if local_path is not None:
                available_value = "local"
            elif remote_present:
                available_value = "remote"
            else:
                available_value = "missing"
                if server_available:
                    _evict(root, project, issue_id, media_id)
            entry.update(
                path=str(local_path) if local_path else None,
                missing=available_value == "missing",
                available=available_value,
            )

            frame_rows = (
                metadata.get("frames", [])
                if isinstance(metadata, dict) and isinstance(metadata.get("frames"), list)
                else _cached_frames(root, project, issue_id, media_id)
            )
            frames = []
            for frame in frame_rows:
                if not isinstance(frame, dict):
                    continue
                t_ms = frame.get("t_ms")
                frame_sha = frame.get("sha256")
                frame_size = frame.get("size_bytes")
                if (
                    not isinstance(t_ms, int)
                    or isinstance(t_ms, bool)
                    or t_ms < 0
                    or t_ms > 86_400_000
                    or not isinstance(frame_sha, str)
                    or re.fullmatch(r"[0-9a-f]{64}", frame_sha) is None
                    or isinstance(frame_size, bool)
                    or not isinstance(frame_size, int)
                    or frame_size < 0
                ):
                    continue
                filename = frame_name(t_ms)
                path = _verified_cached(
                    root,
                    project,
                    issue_id,
                    media_id,
                    filename,
                    frame_sha,
                    frame_size,
                    frame=True,
                )
                frame_available = (
                    "local"
                    if path is not None
                    else "remote"
                    if server_available and isinstance(metadata, dict)
                    else "missing"
                )
                frames.append(
                    {
                        "t_ms": t_ms,
                        "sha256": frame_sha,
                        "size_bytes": frame_size,
                        "path": str(path) if path else None,
                        "available": frame_available,
                        "missing": frame_available == "missing",
                    }
                )
            entry["frames"] = frames
    return views


def fetch_view_media(root: Path, remote: http.Remote, project: str, view: dict) -> dict:
    """Fetch originals and known frames, then return an updated verified view."""
    annotated = annotate_views(root, remote, project, [view])[0]
    for entry in annotated.get("media", []):
        if entry.get("removed") or entry.get("available") == "missing":
            continue
        media_id = entry["id"]
        ext = media_ext(str(entry.get("content_type")))
        name = f"{media_id}{ext}" if ext else None
        if name and entry.get("available") == "remote":
            path = _fetch_to_cache(
                root,
                remote,
                project,
                view["id"],
                media_id,
                name,
                entry["sha256"],
                entry["size_bytes"],
            )
            entry.update(path=str(path), available="local", missing=False)
        for frame in entry.get("frames", []):
            if frame.get("available") != "remote":
                continue
            filename = frame_name(frame["t_ms"])
            path = _fetch_to_cache(
                root,
                remote,
                project,
                view["id"],
                media_id,
                filename,
                frame["sha256"],
                frame["size_bytes"],
                frame=True,
            )
            frame.update(path=str(path), available="local", missing=False)
    _enforce_lru(
        root,
        {
            (project, view["id"], entry["id"])
            for entry in annotated.get("media", [])
            if entry.get("available") == "local"
        },
    )
    return annotated


def _fetch_to_cache(
    root: Path,
    remote: http.Remote,
    project: str,
    issue_id: str,
    media_id: str,
    name: str,
    sha256: str,
    size_bytes: int,
    *,
    frame: bool = False,
) -> Path:
    _safe_component(issue_id, "iss")
    _safe_component(media_id, "med")
    if (
        not isinstance(sha256, str)
        or re.fullmatch(r"[0-9a-f]{64}", sha256) is None
        or isinstance(size_bytes, bool)
        or not isinstance(size_bytes, int)
        or size_bytes < 0
    ):
        raise OpError("INTEGRITY_ERROR", "issue-media fetch metadata is invalid.")
    endpoint = _endpoint(project, f"{issue_id}/{media_id}")
    if frame:
        t_ms = parse_frame_name(name)
        if t_ms is None:
            raise OpError("INTEGRITY_ERROR", "issue-media frame name is invalid.")
        endpoint += f"/frames/{urllib.parse.quote(name, safe='')}"
    try:
        response = http.request(
            remote, "GET", endpoint, expect="bytes", policy=http.BULK, what="issue media"
        )
    except http.Unreachable as exc:
        raise server_unreachable(remote, exc.reason) from None
    except http.ServerError as exc:
        raise OpError(exc.code, exc.message, exc.details) from None
    data = response.body
    if len(data) != size_bytes or hashlib.sha256(data).hexdigest() != sha256:
        raise OpError(
            "INTEGRITY_ERROR", "downloaded issue media failed hash or size verification."
        )
    path = _safe_write(root, project, issue_id, media_id, name, data, frame=frame)
    if frame:
        _write_frame_metadata(root, project, issue_id, media_id, name, t_ms, sha256, size_bytes)
    return path


__all__ = ["MAX_CACHE_BYTES", "annotate_views", "availability", "fetch_view_media"]
