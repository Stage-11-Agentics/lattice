"""``lattice server project import``: move a local board onto a server (SPEC §11).

The import never modifies its source and never follows a symbolic link inside
it. In order:

1. Check the arguments, and refuse an existing slug, before anything is read.
2. Scan the source's ``.lattice/`` through directory descriptors opened with
   ``O_NOFOLLOW``, descending every real directory and classifying every path
   on its own (SPEC §6.1). A durable or workspace path must be a real
   directory or a regular file, else ``VALIDATION_ERROR`` naming it; a
   directory that cannot be read refuses the same way, because the import
   must name every path it does not move. The scan records the identity of
   every path, copied or not, and of the board directory itself.
3. Copy each durable regular file byte for byte into a staging board under
   ``projects/.importing-<slug>-<id>/``, reading it through the same
   descriptor walk and checking its identity before and after the read.
4. Open the source's ``.lattice/`` again and rescan it: any change to the
   board directory or to any path under it means a writer is still running,
   so the import refuses (``CONFLICT``).
5. Run doctor's board checks on the staged copy, which holds exactly the bytes
   imported; any error refuses (``INTEGRITY_ERROR``) with every finding.
6. Rebuild the task-derived files with the short-ID log floor (SPEC §5).
7. Seal the board (journal at a new epoch, head 0), make the staging directory
   its audit repository as ``project create`` does (SPEC §8.10; the first
   commit names the new epoch), and, under ``admin.lock``, rename it into place
   if the slug is still free.

Any refusal or failure removes the staging directory, so nothing is created.
"""

from __future__ import annotations

import errno
import hashlib
import json
import os
import re
import shutil
import stat
import tempfile
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath

from lattice.core.errors import OpError
from lattice.core.events import create_issue_event
from lattice.core.ids import generate_instance_id, generate_media_id, generate_op_id, validate_id
from lattice.core.issue_media import (
    PhotoMetadataError,
    format_size,
    media_ext,
    next_media_n,
    parse_frame_name,
    sniff_media,
    strip_photo_metadata,
)
from lattice.core.issues import apply_issue_event, replay_issue, validate_issue_media_hashes
from lattice.server.admin import (
    _create_audit_repo,
    admin_lock,
    check_slug,
    project_dir,
    require_root,
    seal_new_board,
)
from lattice.server.config import PROJECTS_DIR, SERVER_JSON, ServerConfigError, load_config
from lattice.server.journal import HOSTED_DIR
from lattice.storage.fs import LATTICE_DIR, atomic_write, ensure_dir
from lattice.storage.issue_media import frames_dir, media_path, store_media
from lattice.storage.integrity import DoctorReport, check_board, repair_task_derived_files
from lattice.storage.issues import read_issue_events, rebuild_issue_snapshots, write_issue_events
from lattice.storage.operations import AuthoritativeLogError
from lattice.storage.ownership import (
    PathClass,
    classify_path,
    owning_board,
    release_owner_flock,
    try_owner_flock,
)

_COPIED_CLASSES = frozenset({PathClass.DURABLE, PathClass.WORKSPACE})
_PROSE_DIRS = (("plans",), ("notes",), ("archive", "plans"), ("archive", "notes"))
_DIR_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0)
_FILE_FLAGS = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | getattr(os, "O_CLOEXEC", 0)
_ROOT = "."
_SHA256_RE = re.compile(r"[0-9a-f]{64}\Z")


@dataclass(frozen=True)
class _Identity:
    """What the scan saw at a path (``lstat``): kind, device, inode, and for
    anything but a directory its size and mtime."""

    kind: str
    dev: int
    ino: int
    size: int = 0
    mtime_ns: int = 0

    @classmethod
    def of(cls, st: os.stat_result) -> _Identity:
        if stat.S_ISDIR(st.st_mode):
            return cls("dir", st.st_dev, st.st_ino)
        kind = (
            "file" if stat.S_ISREG(st.st_mode) else "link" if stat.S_ISLNK(st.st_mode) else "other"
        )
        return cls(kind, st.st_dev, st.st_ino, st.st_size, st.st_mtime_ns)


@dataclass
class _Scan:
    """Every path under the board (relative, POSIX), with its class and identity.

    ``"."`` is the board directory itself.
    """

    entries: dict[str, tuple[PathClass, _Identity]] = field(default_factory=dict)
    unreadable_media_dirs: set[str] = field(default_factory=set)

    def identity(self, path: str) -> _Identity | None:
        entry = self.entries.get(path)
        return entry[1] if entry else None

    @property
    def copied_files(self) -> list[str]:
        return [
            path
            for path, (path_class, identity) in self.entries.items()
            if path_class in _COPIED_CLASSES and identity.kind == "file"
        ]

    @property
    def copied_dirs(self) -> list[str]:
        """Durable directories, and any directory that holds a copied file, parents first."""
        wanted = {
            path
            for path, (path_class, identity) in self.entries.items()
            if path != _ROOT and path_class in _COPIED_CLASSES and identity.kind == "dir"
        }
        for path in self.copied_files:
            wanted.update(p.as_posix() for p in PurePosixPath(path).parents if p.parts)
        return sorted(wanted, key=lambda p: (p.count("/"), p))

    @property
    def not_copied(self) -> list[tuple[str, str]]:
        """``(path, class)`` for every path the import does not move; directories end in ``/``."""
        created = set(self.copied_dirs)
        rows = []
        for path, (path_class, identity) in self.entries.items():
            if path == _ROOT or path_class in _COPIED_CLASSES or path in created:
                continue
            rows.append((f"{path}/" if identity.kind == "dir" else path, path_class.value))
        return sorted(rows)


@dataclass(frozen=True)
class _MediaFrame:
    t_ms: int
    path: str
    source_sha256: str
    source_size: int
    stored_sha256: str
    stored_size: int
    spool_path: Path | None


@dataclass(frozen=True)
class _MediaSource:
    issue_id: str
    media_id: str
    content_type: str
    original_path: str
    original_sha256: str
    original_size: int
    stored_sha256: str
    stored_size: int
    original_name: str | None
    n: int
    width: int | None
    height: int | None
    converted_from: dict | None
    spool_path: Path | None
    frames: tuple[_MediaFrame, ...]


@dataclass(frozen=True)
class _MediaInventory:
    sources: tuple[_MediaSource, ...]
    media_count: int
    media_object_count: int
    media_known_bytes: int
    media_unknown_size_count: int
    media_inventory_complete: bool
    photos_sanitized: int = 0
    frames_sanitized: int = 0
    photos_unchecked: int = 0

    @property
    def media_bytes(self) -> int | None:
        if self.media_unknown_size_count or not self.media_inventory_complete:
            return None
        return self.media_known_bytes


def _media_refusal(path: str, detail: str, *, code: str = "INTEGRITY_ERROR") -> OpError:
    return OpError(code, f"Import refused: issue media .lattice/{path} {detail}.", {"path": path})


def _issue_snapshots_for_import(lattice_fd: int, scan: _Scan, source_board: Path) -> list[dict]:
    """Replay scanned issue logs without trusting snapshots or following paths."""
    snapshots = []
    for path in sorted(scan.copied_files):
        rel = PurePosixPath(path)
        if rel.parent.as_posix() != "issues/events" or not rel.name.endswith(".jsonl"):
            continue
        issue_id = rel.stem
        if not validate_id(issue_id, "iss"):
            raise OpError(
                "INTEGRITY_ERROR",
                f"Import refused: invalid issue log name {source_board / path}.",
                {"path": path},
            )
        try:
            raw = _read_file(lattice_fd, path, scan)
            events = [
                json.loads(line) for line in raw.decode("utf-8").splitlines() if line.strip()
            ]
            snapshot = replay_issue(events)
            if snapshot is not None:
                if snapshot.get("id") != issue_id:
                    raise ValueError("log issue ID does not match its filename")
                validate_issue_media_hashes(snapshot)
                snapshots.append(snapshot)
        except (OSError, UnicodeDecodeError, ValueError, KeyError, TypeError) as exc:
            raise OpError(
                "INTEGRITY_ERROR",
                f"Import refused: issue-log replay failed for {source_board / path}: {exc}.",
                {"path": path},
            ) from exc
    return snapshots


def _media_relative_paths(source_board: Path, issue_id: str, entry: dict) -> tuple[str, str]:
    original = media_path(source_board, issue_id, entry)
    frames = frames_dir(source_board, issue_id, entry)
    if original is None or frames is None:
        raise _media_refusal(f"{issue_id}/{entry.get('id')}", "has an invalid ID or content type")
    return original.relative_to(source_board).as_posix(), frames.relative_to(
        source_board
    ).as_posix()


def _file_bytes_for_preflight(
    lattice_fd: int,
    scan: _Scan,
    relative: str,
    *,
    expected_sha256: str | None,
    expected_size: int | None,
    expected_type: str,
) -> tuple[str, int, bytes]:
    identity = scan.entries.get(relative)
    if identity is None:
        raise _media_refusal(relative, "is missing")
    if identity[1].kind != "file":
        raise _media_refusal(relative, "is a symbolic link or not a regular file")
    try:
        content = _read_file(lattice_fd, relative, scan)
    except OpError as exc:
        raise _media_refusal(relative, f"could not be read safely ({exc.message})") from exc
    digest = hashlib.sha256(content).hexdigest()
    if expected_size is not None and len(content) != expected_size:
        raise _media_refusal(relative, "does not match the size recorded in issue metadata")
    if expected_sha256 is not None and digest != expected_sha256:
        raise _media_refusal(relative, "does not match the SHA-256 recorded in issue metadata")
    if sniff_media(content[:64]) != expected_type:
        raise _media_refusal(relative, f"does not have the recorded {expected_type} content type")
    return digest, len(content), content


def _spool_bytes(spool: Path, content: bytes) -> Path:
    """Write transformed media once into the import's private temporary spool."""
    digest = hashlib.sha256(content).hexdigest()
    path = spool / f"{digest}.blob"
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    except FileExistsError:
        if path.read_bytes() != content:
            raise OpError("INTEGRITY_ERROR", "private import media spool hash collision")
        return path
    try:
        view = memoryview(content)
        while view:
            view = view[os.write(fd, view) :]
        os.fsync(fd)
    except BaseException:
        os.close(fd)
        path.unlink(missing_ok=True)
        raise
    os.close(fd)
    return path


def _direct_children(scan: _Scan, directory: str) -> list[tuple[str, _Identity]]:
    prefix = directory + "/"
    result = []
    for path, (_path_class, identity) in scan.entries.items():
        if not path.startswith(prefix):
            continue
        remainder = path[len(prefix) :]
        if remainder and "/" not in remainder:
            result.append((path, identity))
    return sorted(result)


def _preflight_media_copy(
    lattice_fd: int,
    scan: _Scan,
    source_board: Path,
    snapshots: list[dict],
    limits,
    spool: Path,
    *,
    keep_photo_metadata: bool,
) -> _MediaInventory:
    """Verify, sanitize, spool, and quota-check every referenced original/frame."""
    sources: list[_MediaSource] = []
    issue_totals: dict[str, int] = {}
    project_hash_sizes: dict[str, int] = {}
    object_sizes: list[tuple[str, str, int]] = []
    photos_sanitized = 0
    frames_sanitized = 0

    for snapshot in snapshots:
        issue_id = snapshot["id"]
        for entry in snapshot.get("media", []):
            if entry.get("removed"):
                continue
            media_id = entry.get("id")
            content_type = entry.get("content_type")
            sha256 = entry.get("sha256")
            size = entry.get("size_bytes")
            if (
                not validate_id(media_id, "med")
                or media_ext(content_type) is None
                or not isinstance(sha256, str)
                or _SHA256_RE.fullmatch(sha256) is None
                or isinstance(size, bool)
                or not isinstance(size, int)
                or size < 0
            ):
                raise _media_refusal(f"{issue_id}/{media_id}", "has invalid issue metadata")
            original, frame_directory = _media_relative_paths(source_board, issue_id, entry)
            original_sha, original_size, original_content = _file_bytes_for_preflight(
                lattice_fd,
                scan,
                original,
                expected_sha256=sha256,
                expected_size=size,
                expected_type=content_type,
            )
            stored_content = original_content
            original_spool: Path | None = None
            if content_type in {"image/jpeg", "image/png"}:
                try:
                    stored_content = strip_photo_metadata(original_content, content_type)
                except PhotoMetadataError as exc:
                    if not keep_photo_metadata:
                        raise _media_refusal(
                            original,
                            "could not be sanitized; repair it or rerun with "
                            "--keep-photo-metadata to retain the original",
                            code="VALIDATION_ERROR",
                        ) from exc
                if stored_content != original_content:
                    original_spool = _spool_bytes(spool, stored_content)
                    photos_sanitized += 1
            elif content_type == "image/heic" and not keep_photo_metadata:
                raise _media_refusal(
                    original,
                    "is HEIC and cannot be converted by the importer; rerun with "
                    "--keep-photo-metadata to retain it",
                    code="VALIDATION_ERROR",
                )
            stored_sha = hashlib.sha256(stored_content).hexdigest()
            stored_size = len(stored_content)
            frame_rows: list[_MediaFrame] = []
            frame_identity = scan.entries.get(frame_directory)
            if frame_identity is not None:
                if frame_identity[1].kind != "dir":
                    raise _media_refusal(frame_directory, "is not a real frame-sidecar directory")
                for frame_path, identity in _direct_children(scan, frame_directory):
                    t_ms = parse_frame_name(PurePosixPath(frame_path).name)
                    if t_ms is None:
                        raise _media_refusal(frame_path, "has an invalid frame-sidecar name")
                    if identity.kind != "file":
                        raise _media_refusal(
                            frame_path, "is a symbolic link or not a regular file"
                        )
                    frame_hash, frame_size, frame_content = _file_bytes_for_preflight(
                        lattice_fd,
                        scan,
                        frame_path,
                        expected_sha256=None,
                        expected_size=None,
                        expected_type="image/jpeg",
                    )
                    clean_frame = frame_content
                    frame_spool: Path | None = None
                    try:
                        clean_frame = strip_photo_metadata(frame_content, "image/jpeg")
                    except PhotoMetadataError as exc:
                        if not keep_photo_metadata:
                            raise _media_refusal(
                                frame_path,
                                "could not be sanitized; repair it or rerun with "
                                "--keep-photo-metadata to retain it",
                                code="VALIDATION_ERROR",
                            ) from exc
                    if clean_frame != frame_content:
                        frame_spool = _spool_bytes(spool, clean_frame)
                        frames_sanitized += 1
                    frame_rows.append(
                        _MediaFrame(
                            t_ms,
                            frame_path,
                            frame_hash,
                            frame_size,
                            hashlib.sha256(clean_frame).hexdigest(),
                            len(clean_frame),
                            frame_spool,
                        )
                    )

            sources.append(
                _MediaSource(
                    issue_id,
                    media_id,
                    content_type,
                    original,
                    original_sha,
                    original_size,
                    stored_sha,
                    stored_size,
                    entry.get("original_name"),
                    int(entry.get("n") or 0),
                    entry.get("width"),
                    entry.get("height"),
                    entry.get("converted_from"),
                    original_spool,
                    tuple(frame_rows),
                )
            )
            items = [(original, stored_sha, stored_size)] + [
                (frame.path, frame.stored_sha256, frame.stored_size) for frame in frame_rows
            ]
            issue_totals[issue_id] = issue_totals.get(issue_id, 0) + sum(
                item_size for _path, _digest, item_size in items
            )
            for path, digest, item_size in items:
                object_sizes.append((path, digest, item_size))
                project_hash_sizes.setdefault(digest, item_size)
                if project_hash_sizes[digest] != item_size:
                    raise _media_refusal(path, "has an inconsistent size for its SHA-256")

    for path, _digest, size in object_sizes:
        if size > limits.max_issue_media_file_bytes:
            raise _media_refusal(
                path,
                f"exceeds max_issue_media_file_bytes ({format_size(limits.max_issue_media_file_bytes)}); "
                "rerun with --omit-media to import metadata only if acceptable",
                code="PAYLOAD_TOO_LARGE",
            )
    for issue_id, size in issue_totals.items():
        if size > limits.max_issue_media_issue_bytes:
            raise _media_refusal(
                issue_id,
                f"exceeds max_issue_media_issue_bytes ({format_size(limits.max_issue_media_issue_bytes)}); "
                "rerun with --omit-media to import metadata only if acceptable",
                code="PAYLOAD_TOO_LARGE",
            )
    # Storage is not deduplicated, so the project total is every stored object.
    project_bytes = sum(size for _path, _digest, size in object_sizes)
    if project_bytes > limits.max_issue_media_project_bytes:
        raise _media_refusal(
            "issues/media",
            f"exceeds max_issue_media_project_bytes ({format_size(limits.max_issue_media_project_bytes)}); "
            "rerun with --omit-media to import metadata only if acceptable",
            code="MEDIA_QUOTA_EXCEEDED",
        )

    return _MediaInventory(
        tuple(sources),
        len(sources),
        len(object_sizes),
        sum(size for _path, _digest, size in object_sizes),
        0,
        True,
        photos_sanitized,
        frames_sanitized,
    )


def _safe_missing_parent(scan: _Scan, relative: str) -> bool:
    """Whether a missing path's scanned parents are real dirs or absent, never links."""
    parts = PurePosixPath(relative).parts[:-1]
    for depth in range(1, len(parts) + 1):
        parent = PurePosixPath(*parts[:depth]).as_posix()
        entry = scan.entries.get(parent)
        if entry is None:
            return True
        if entry[1].kind != "dir":
            return False
    return True


def _media_dir_unreadable(scan: _Scan, relative: str) -> bool:
    return any(
        relative == directory or relative.startswith(directory + "/")
        for directory in scan.unreadable_media_dirs
    )


def _inventory_media_omit(
    scan: _Scan, source_board: Path, snapshots: list[dict]
) -> _MediaInventory:
    """Report media paths using lstat identities only; never opens or hashes bytes."""
    media_count = 0
    media_object_count = 0
    photos_unchecked = 0
    known_bytes = unknown = 0
    complete = True
    for snapshot in snapshots:
        issue_id = snapshot["id"]
        for entry in snapshot.get("media", []):
            if entry.get("removed"):
                continue
            original, frame_directory = _media_relative_paths(source_board, issue_id, entry)
            media_count += 1
            media_object_count += 1
            if entry.get("content_type") in {"image/jpeg", "image/png", "image/heic"}:
                photos_unchecked += 1
            identity = scan.entries.get(original)
            if identity is not None and identity[1].kind == "file":
                known_bytes += identity[1].size
            else:
                unknown += 1

            if _media_dir_unreadable(scan, frame_directory):
                complete = False
                continue
            frame_identity = scan.entries.get(frame_directory)
            if frame_identity is None:
                if not _safe_missing_parent(scan, frame_directory):
                    complete = False
                continue
            if frame_identity[1].kind != "dir":
                complete = False
                continue
            for _path, child in _direct_children(scan, frame_directory):
                media_object_count += 1
                if child.kind == "file":
                    known_bytes += child.size
                else:
                    unknown += 1
    return _MediaInventory(
        (),
        media_count,
        media_object_count,
        known_bytes,
        unknown,
        complete,
        photos_unchecked=photos_unchecked,
    )


def _copy_preflighted_media(
    lattice_fd: int,
    scan: _Scan,
    board: Path,
    inventory: _MediaInventory,
) -> set[str]:
    """Copy verified media, then append import-only identity changes for sanitized photos."""
    copied_paths = set()
    changed_by_issue: dict[str, list[tuple[_MediaSource, str]]] = {}
    for source in inventory.sources:
        original_raw = _read_file(lattice_fd, source.original_path, scan)
        if (
            len(original_raw) != source.original_size
            or hashlib.sha256(original_raw).hexdigest() != source.original_sha256
        ):
            raise _changed(source.original_path)
        original = source.spool_path.read_bytes() if source.spool_path else original_raw
        if (
            len(original) != source.stored_size
            or hashlib.sha256(original).hexdigest() != source.stored_sha256
        ):
            raise OpError("INTEGRITY_ERROR", "private import media spool failed verification")
        destination_id = generate_media_id() if source.spool_path else source.media_id
        frames = []
        for frame in source.frames:
            source_content = _read_file(lattice_fd, frame.path, scan)
            if (
                len(source_content) != frame.source_size
                or hashlib.sha256(source_content).hexdigest() != frame.source_sha256
            ):
                raise _changed(frame.path)
            content = frame.spool_path.read_bytes() if frame.spool_path else source_content
            if (
                len(content) != frame.stored_size
                or hashlib.sha256(content).hexdigest() != frame.stored_sha256
            ):
                raise OpError("INTEGRITY_ERROR", "private import frame spool failed verification")
            frames.append((frame.t_ms, content))
        store_media(
            board,
            source.issue_id,
            {"id": destination_id, "content_type": source.content_type},
            original,
            frames,
        )
        copied_paths.add(source.original_path)
        copied_paths.update(frame.path for frame in source.frames)
        if source.spool_path is not None:
            changed_by_issue.setdefault(source.issue_id, []).append((source, destination_id))

    import_origin = {
        "op": "server.project.import",
        "op_id": generate_op_id(),
        "reported": {},
    }
    for issue_id, changed in changed_by_issue.items():
        events = read_issue_events(board, issue_id)
        snapshot = replay_issue(events)
        if snapshot is None:
            raise OpError("INTEGRITY_ERROR", f"Import refused: issue {issue_id} has no event log.")
        appended = []
        for source, media_id in changed:
            remove_event = create_issue_event(
                "issue_media_removed",
                issue_id,
                "system:import",
                {
                    "media_id": source.media_id,
                    "n": source.n,
                    "reason": "photo_metadata_removed_on_import",
                },
            )
            remove_event["origin"] = import_origin
            snapshot = apply_issue_event(snapshot, remove_event)
            appended.append(remove_event)
            add_data = {
                "media_id": media_id,
                "n": next_media_n(snapshot),
                "kind": "photo",
                "content_type": source.content_type,
                "size_bytes": source.stored_size,
                "sha256": source.stored_sha256,
            }
            if source.original_name is not None:
                add_data["original_name"] = source.original_name
            if source.width is not None:
                add_data["width"] = source.width
            if source.height is not None:
                add_data["height"] = source.height
            if source.converted_from is not None:
                add_data["converted_from"] = source.converted_from
            add_event = create_issue_event(
                "issue_media_added", issue_id, "system:import", add_data
            )
            add_event["origin"] = import_origin
            snapshot = apply_issue_event(snapshot, add_event)
            appended.append(add_event)
        write_issue_events(board, issue_id, appended, snapshot)
    return copied_paths


def _not_copied(scan: _Scan, copied_media_paths: set[str]) -> list[dict]:
    copied_dirs = set()
    for path in copied_media_paths:
        for parent in PurePosixPath(path).parents:
            if parent.parts and parent.as_posix().startswith("issues/media"):
                copied_dirs.add(parent.as_posix())
    result = []
    for path, path_class in scan.not_copied:
        normalized = path.rstrip("/")
        if normalized in copied_media_paths or normalized in copied_dirs:
            continue
        result.append({"path": path, "class": path_class})
    return result


def _unsafe(path: str, what: str) -> OpError:
    return OpError(
        "VALIDATION_ERROR",
        f"Import refused: .lattice/{path} {what}. Only real directories and regular files "
        "can be imported; nothing was created.",
        {"path": path},
    )


def _changed(path: str) -> OpError:
    return OpError(
        "CONFLICT",
        f"Import refused: .lattice/{path} changed while it was being copied. Stop every "
        "writer of the board (agents, dashboards, MCP servers) and import again; nothing "
        "was created.",
        {"path": path, "reason": "SOURCE_CHANGED"},
    )


def _unreadable(path: str, exc: OSError) -> OpError:
    return OpError(
        "VALIDATION_ERROR",
        f"Import refused: cannot read .lattice/{path} ({exc.strerror or exc}), so the import "
        "cannot list every path it would leave behind; nothing was created.",
        {"path": path},
    )


def _open_dir(parent_fd: int, name: str, rel: str, expected: _Identity | None) -> int:
    """Open one directory component without following a link; check it is the one scanned."""
    try:
        fd = os.open(name, _DIR_FLAGS, dir_fd=parent_fd)
    except OSError as exc:
        if exc.errno in (errno.ELOOP, errno.ENOTDIR, errno.ENOENT):
            raise _changed(rel) from None
        raise _unreadable(rel, exc) from None
    if expected is not None:
        st = os.fstat(fd)
        if (st.st_dev, st.st_ino) != (expected.dev, expected.ino):
            os.close(fd)
            raise _changed(rel)
    return fd


def _scan(lattice_fd: int, *, tolerate_media_errors: bool = False) -> _Scan:
    """Walk the whole board below *lattice_fd* without following links (step 2)."""
    scan = _Scan()
    scan.entries[_ROOT] = (PathClass.DURABLE, _Identity.of(os.fstat(lattice_fd)))

    def visit(dir_fd: int, rel: PurePosixPath) -> None:
        where = rel.as_posix() if rel.parts else _ROOT
        try:
            names = sorted(os.listdir(dir_fd))
        except OSError as exc:
            if (
                tolerate_media_errors
                and where != _ROOT
                and classify_path(where) is PathClass.ISSUE_MEDIA
            ):
                scan.unreadable_media_dirs.add(where)
                return
            raise _unreadable(where, exc) from None
        for name in names:
            child = rel / name
            path = child.as_posix()
            path_class = classify_path(child)
            try:
                st = os.stat(name, dir_fd=dir_fd, follow_symlinks=False)
            except FileNotFoundError:
                if tolerate_media_errors and path_class is PathClass.ISSUE_MEDIA:
                    scan.entries[path] = (path_class, _Identity("unknown", 0, 0))
                    continue
                raise _changed(path) from None
            except OSError as exc:
                if tolerate_media_errors and path_class is PathClass.ISSUE_MEDIA:
                    scan.entries[path] = (path_class, _Identity("unknown", 0, 0))
                    continue
                raise _unreadable(path, exc) from None
            identity = _Identity.of(st)
            if path_class in _COPIED_CLASSES:
                if identity.kind == "link":
                    raise _unsafe(path, "is a symbolic link")
                if identity.kind == "other":
                    raise _unsafe(path, "is not a regular file or a directory")
            scan.entries[path] = (path_class, identity)
            if identity.kind == "dir":
                try:
                    fd = _open_dir(dir_fd, name, path, identity)
                except OpError:
                    if tolerate_media_errors and path_class is PathClass.ISSUE_MEDIA:
                        scan.unreadable_media_dirs.add(path)
                        continue
                    raise
                try:
                    visit(fd, child)
                finally:
                    os.close(fd)

    visit(lattice_fd, PurePosixPath())
    return scan


def _read_file(lattice_fd: int, path: str, scan: _Scan) -> bytes:
    """Read one scanned file through no-follow descriptors, checking its identity (step 3)."""
    parts = PurePosixPath(path).parts
    fds: list[int] = []
    try:
        dir_fd = lattice_fd
        for depth, name in enumerate(parts[:-1], start=1):
            rel = "/".join(parts[:depth])
            dir_fd = _open_dir(dir_fd, name, rel, scan.identity(rel))
            fds.append(dir_fd)
        try:
            fd = os.open(parts[-1], _FILE_FLAGS, dir_fd=dir_fd)
        except OSError as exc:
            if exc.errno in (errno.ELOOP, errno.ENOENT, errno.ENXIO):
                raise _changed(path) from None
            raise _unreadable(path, exc) from None
        fds.append(fd)
        expected = scan.identity(path)
        before = os.fstat(fd)
        if not stat.S_ISREG(before.st_mode) or _Identity.of(before) != expected:
            raise _changed(path)
        chunks = []
        while chunk := os.read(fd, 1 << 20):
            chunks.append(chunk)
        data = b"".join(chunks)
        if _Identity.of(os.fstat(fd)) != expected or len(data) != expected.size:
            raise _changed(path)
        return data
    finally:
        for fd in reversed(fds):
            os.close(fd)


def _open_source(source: Path) -> int:
    """Check ``--from`` and open its ``.lattice/`` without following a link (step 1)."""
    if not source.exists():
        raise OpError("NOT_FOUND", f"--from {source} does not exist.", {"path": str(source)})
    if not source.is_dir():
        raise OpError(
            "VALIDATION_ERROR", f"--from {source} is not a directory.", {"path": str(source)}
        )
    try:
        st = os.stat(source / LATTICE_DIR, follow_symlinks=False)
    except FileNotFoundError:
        raise OpError(
            "VALIDATION_ERROR",
            f"--from {source} contains no {LATTICE_DIR}/ board.",
            {"path": str(source)},
        ) from None
    if stat.S_ISLNK(st.st_mode):
        raise _unsafe(_ROOT, "(the board directory itself) is a symbolic link")
    if not stat.S_ISDIR(st.st_mode):
        raise _unsafe(_ROOT, "(the board directory itself) is not a directory")
    try:
        parent = os.open(source, os.O_RDONLY | os.O_DIRECTORY)
    except OSError as exc:
        raise _unreadable(_ROOT, exc) from None
    try:
        return _open_dir(parent, LATTICE_DIR, _ROOT, _Identity.of(st))
    finally:
        os.close(parent)


def _source_unchanged(source: Path, scan: _Scan, *, tolerate_media_errors: bool = False) -> None:
    """Reopen the board by its path and rescan it; refuse on any difference (step 4)."""
    try:
        fd = _open_source(source)
    except OpError as exc:
        if exc.code == "NOT_FOUND" or exc.details.get("path") == _ROOT:
            raise _changed(_ROOT) from None
        raise
    try:
        rescan = _scan(fd, tolerate_media_errors=tolerate_media_errors)
    finally:
        os.close(fd)
    if (
        rescan.entries != scan.entries
        or rescan.unreadable_media_dirs != scan.unreadable_media_dirs
    ):
        for path in sorted(scan.entries.keys() | rescan.entries.keys()):
            if scan.entries.get(path) != rescan.entries.get(path):
                raise _changed(path)
        changed_unreadable = scan.unreadable_media_dirs.symmetric_difference(
            rescan.unreadable_media_dirs
        )
        if changed_unreadable:
            raise _changed(sorted(changed_unreadable)[0])


def _non_canonical(scan: _Scan) -> list[str]:
    """Files under the prose directories that are not ``<task_id>.md`` for a task of the board."""
    files = scan.copied_files
    task_ids = {
        PurePosixPath(path).stem
        for path in files
        if PurePosixPath(path).parent.as_posix() in ("events", "archive/events")
        and PurePosixPath(path).name.startswith("task_")
        and path.endswith(".jsonl")
    }
    listed = []
    for path in files:
        parts = PurePosixPath(path).parts
        for prose in _PROSE_DIRS:
            if parts[: len(prose)] != prose:
                continue
            rest = parts[len(prose) :]
            name = rest[0] if len(rest) == 1 else ""
            if not (name.endswith(".md") and name[:-3] in task_ids):
                listed.append(path)
            break
    return sorted(listed)


def _move_steps(slug: str) -> list[dict]:
    """The guide's move steps (SPEC §11), in order, with the attach command filled in."""
    return [
        {
            "step": 1,
            "text": "Stop every writer of the local board: agents, dashboards, MCP servers.",
            "commands": [],
        },
        {
            "step": 2,
            "text": "Import a copy of the board on the server host (done). Read both lists: "
            "paths not copied stay only in the old board. If import refuses, run its next "
            "step on the local board (its writers are already stopped), commit nothing yet, "
            "copy the board again, and repeat this step.",
            "commands": [],
        },
        {
            "step": 3,
            "text": "In the checkout, move the old board aside (never delete it) and ignore it; "
            "if board files are tracked in git, stage their removal.",
            "commands": [
                "mv .lattice .lattice.pre-hosted-$(date -u +%Y%m%d-%H%M%S)",
                "echo '/.lattice.pre-hosted-*/' >> .gitignore",
                "git rm -r --cached -q --ignore-unmatch .lattice",
            ],
        },
        {
            "step": 4,
            "text": "Attach the checkout. <alias> is your name for this server "
            "('lattice remote list' shows it).",
            "commands": [f"lattice remote attach <alias> {slug}"],
        },
        {
            "step": 5,
            "text": "Commit the binding, .gitignore, and the staged removal, and push; then "
            "list branches that still track board files.",
            "commands": [
                "git add .lattice-remote.json .gitignore",
                'git commit -m "Move the Lattice board to the server"',
                "git push",
                "lattice remote status",
            ],
        },
    ]


def import_project(
    root: Path,
    slug: str,
    source: Path,
    *,
    omit_media: bool = False,
    keep_photo_metadata: bool = False,
) -> dict:
    """Import the board at ``<source>/.lattice/`` as project *slug* (SPEC §11)."""
    root = Path(root)
    check_slug(slug)
    require_root(root)
    final = project_dir(root, slug)
    if final.exists():
        raise OpError("CONFLICT", f"Project '{slug}' already exists at {final}.")
    try:
        server_config = load_config(root)
        audit_config = server_config.audit
    except ServerConfigError as exc:
        raise OpError("VALIDATION_ERROR", f"{root / SERVER_JSON}: {exc}") from exc
    source = Path(source)
    lattice_fd = _open_source(source)
    staging: Path | None = None
    spool: Path | None = None
    try:
        scan = _scan(lattice_fd, tolerate_media_errors=omit_media)
        source_board = source / LATTICE_DIR
        snapshots = _issue_snapshots_for_import(lattice_fd, scan, source_board)
        media_inventory = (
            _inventory_media_omit(scan, source_board, snapshots) if omit_media else None
        )
        if not omit_media:
            spool = Path(tempfile.mkdtemp(prefix=".import-media-", dir=root / PROJECTS_DIR))
            media_inventory = _preflight_media_copy(
                lattice_fd,
                scan,
                source_board,
                snapshots,
                server_config.limits,
                spool,
                keep_photo_metadata=keep_photo_metadata,
            )
        assert media_inventory is not None
        # The media preflight is deliberately complete before this first project
        # directory or imported file is written.
        _source_unchanged(source, scan, tolerate_media_errors=omit_media)
        staging = root / PROJECTS_DIR / f".importing-{slug}-{generate_instance_id()[5:]}"
        board = staging / LATTICE_DIR
        with owning_board(board):
            ensure_dir(board / HOSTED_DIR)
            fd = try_owner_flock(board)
            if fd is None:  # a fresh directory nobody else knows about
                raise OpError("BOARD_BUSY", f"could not lock {board}")
            try:
                for path in scan.copied_dirs:
                    ensure_dir(board / path)
                for path in scan.copied_files:
                    atomic_write(board / path, _read_file(lattice_fd, path, scan))
                copied_media_paths = (
                    set()
                    if omit_media
                    else _copy_preflighted_media(lattice_fd, scan, board, media_inventory)
                )
                _source_unchanged(source, scan, tolerate_media_errors=omit_media)
                report = check_board(board)
                findings = [_clean(f, board, source / LATTICE_DIR) for f in report.findings]
                if report.errors:
                    raise _doctor_refusal(report, findings)
                try:
                    repair_task_derived_files(board, reconcile_placement=False)
                except AuthoritativeLogError as exc:
                    message = _as_source(str(exc), board, source / LATTICE_DIR)
                    raise OpError(
                        "INTEGRITY_ERROR", f"Import refused: short-ID repair failed: {message}"
                    ) from exc
                try:
                    rebuild_issue_snapshots(board)
                except OpError as exc:
                    message = _as_source(exc.message, board, source / LATTICE_DIR)
                    raise OpError(
                        "INTEGRITY_ERROR", f"Import refused: issue-log rebuild failed: {message}"
                    ) from exc
                except (ValueError, KeyError, TypeError, AttributeError) as exc:
                    message = _as_source(str(exc), board, source / LATTICE_DIR)
                    raise OpError(
                        "INTEGRITY_ERROR", f"Import refused: issue-log rebuild failed: {message}"
                    ) from exc
                journal = seal_new_board(board)
            finally:
                release_owner_flock(fd)
        audit_state = _create_audit_repo(staging, audit_config, epoch=journal.epoch)
        with admin_lock(root):
            if final.exists():
                raise OpError("CONFLICT", f"Project '{slug}' already exists at {final}.")
            os.rename(staging, final)
    except BaseException:
        if staging is not None:
            shutil.rmtree(staging, ignore_errors=True)
        raise
    finally:
        if spool is not None:
            shutil.rmtree(spool, ignore_errors=True)
        os.close(lattice_fd)

    return {
        "slug": slug,
        "path": str(final),
        "source": str(source),
        "project_code": _project_code(final / LATTICE_DIR),
        "epoch": journal.epoch,
        "head_seq": 0,
        "audit": audit_state,
        "copied": len(scan.copied_files)
        + (0 if omit_media else media_inventory.media_object_count),
        "not_copied": _not_copied(scan, copied_media_paths),
        "media_omitted": omit_media,
        "media_count": media_inventory.media_count,
        "media_object_count": media_inventory.media_object_count,
        "media_bytes": media_inventory.media_bytes,
        "media_known_bytes": media_inventory.media_known_bytes,
        "media_unknown_size_count": media_inventory.media_unknown_size_count,
        "media_inventory_complete": media_inventory.media_inventory_complete,
        "photos_sanitized": media_inventory.photos_sanitized,
        "frames_sanitized": media_inventory.frames_sanitized,
        "photos_unchecked": media_inventory.photos_unchecked,
        "non_canonical": _non_canonical(scan),
        "doctor": {
            "findings": findings,
            "summary": _summary(report),
        },
        "move_steps": _move_steps(slug),
    }


def _project_code(board: Path) -> str | None:
    try:
        return json.loads((board / "config.json").read_text(encoding="utf-8")).get("project_code")
    except (OSError, ValueError, AttributeError):
        return None


def _as_source(text: str, staged: Path, source: Path) -> str:
    """Name the source board, not the staging copy that doctor read (and that is removed)."""
    for form in {str(staged.resolve()), str(staged)}:
        text = text.replace(form, str(source))
    return text


def _clean(finding: dict, staged: Path, source: Path) -> dict:
    """A doctor finding as ``lattice doctor --json`` prints it, with source paths."""
    return {
        "level": finding["level"],
        "check": finding["check"],
        "message": _as_source(finding["message"], staged, source),
        "task_id": finding.get("task_id"),
    }


def _summary(report: DoctorReport) -> dict:
    return {
        "tasks": report.task_count,
        "events": report.event_count,
        "artifacts": report.artifact_count,
        "resources": report.resource_count,
        "warnings": report.warnings,
        "errors": report.errors,
    }


#: The step an import refusal ends with (SPEC §11, AC-17).
NEXT_STEP = (
    "Next step: on the board, run 'lattice doctor --fix --actor <you>' with Lattice 2 "
    "(it only appends events), confirm that 'lattice doctor' is clean, and import again."
)


def _doctor_refusal(report: DoctorReport, findings: list[dict]) -> OpError:
    lines = [f"  {f['level']}: {f['message']}" for f in findings]
    noun = "error" if report.errors == 1 else "errors"
    return OpError(
        "INTEGRITY_ERROR",
        f"Import refused: the board fails lattice doctor ({report.errors} {noun}); "
        "nothing was created. Findings:\n" + "\n".join(lines) + "\n" + NEXT_STEP,
        {"findings": findings, "summary": _summary(report)},
    )
