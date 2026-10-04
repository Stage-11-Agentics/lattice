"""Private hosted issue-media staging, publication, and recovery.

Raw uploads live beside, never inside, the board until an issue operation
commits. A manifest contains only object identifiers and verified storage
metadata; the generic journal supplies the stable operation ID and outcome.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import stat
import threading
import time
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from pathlib import PurePosixPath
from typing import Any

from lattice.core.errors import OpError
from lattice.core.ids import validate_id
from lattice.core.issue_media import (
    PhotoMetadataError,
    frame_name,
    media_ext,
    sniff_media,
    strip_photo_metadata,
)
from lattice.storage.fs import atomic_write, ensure_dir, remove_dir, unlink_entry
from lattice.storage.issue_media import delete_media_files, frames_dir, media_path
from lattice.storage.issues import list_issue_snapshots, read_issue_snapshot

SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
STAGE_TTL_SECONDS = 24 * 60 * 60
DEFAULT_FILE_BYTES = 100 * 1024 * 1024
DEFAULT_ISSUE_BYTES = 250 * 1024 * 1024
DEFAULT_PROJECT_BYTES = 10 * 1024 * 1024 * 1024
MAX_RANGE_BYTES = 1024 * 1024


def validate_sha256(value: object) -> str:
    """Return a lowercase digest or refuse before it can become a path/header."""
    if not isinstance(value, str) or not SHA256_RE.fullmatch(value):
        raise OpError("VALIDATION_ERROR", "sha256 must be 64 lowercase hexadecimal characters.")
    return value


def _open_private_child(parent_fd: int, name: str) -> int:
    """Create or open one private directory without following a symlink."""
    try:
        os.mkdir(name, mode=0o700, dir_fd=parent_fd)
    except FileExistsError:
        pass
    try:
        fd = os.open(
            name,
            os.O_RDONLY | os.O_DIRECTORY | getattr(os, "O_NOFOLLOW", 0),
            dir_fd=parent_fd,
        )
    except OSError as exc:
        raise OpError("INTEGRITY_ERROR", f"private media directory is unsafe: {name}") from exc
    try:
        os.fchmod(fd, 0o700)
    except BaseException:
        os.close(fd)
        raise
    return fd


def _write_private(path: Path, text: str) -> None:
    """Write a mode-0600 file outside the board: temp file, fsync, rename.

    The staging area sits beside the board, so the board-rooted storage
    primitives (which refuse paths outside it) are not used here.
    """
    temp = path.with_name(f".{path.name}.{os.getpid()}.{threading.get_ident()}.tmp")
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(temp, flags, 0o600)
    try:
        view = memoryview(text.encode("utf-8"))
        while view:
            view = view[os.write(fd, view) :]
        os.fsync(fd)
    except BaseException:
        os.close(fd)
        temp.unlink(missing_ok=True)
        raise
    os.close(fd)
    try:
        os.replace(temp, path)
    except BaseException:
        temp.unlink(missing_ok=True)
        raise


def _safe_regular(path: Path) -> os.stat_result | None:
    try:
        info = os.lstat(path)
    except FileNotFoundError:
        return None
    if not stat.S_ISREG(info.st_mode):
        raise OpError("INTEGRITY_ERROR", f"media object is not a regular file: {path.name}")
    return info


def _digest_file(path: Path) -> tuple[str, int, bytes]:
    info = _safe_regular(path)
    if info is None:
        raise OpError("NOT_FOUND", f"staged media object {path.stem} not found.")
    digest = hashlib.sha256()
    head = bytearray()
    size = 0
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0)
    fd = os.open(path, flags)
    try:
        opened = os.fstat(fd)
        if not stat.S_ISREG(opened.st_mode) or (opened.st_dev, opened.st_ino) != (
            info.st_dev,
            info.st_ino,
        ):
            raise OpError("INTEGRITY_ERROR", f"media object changed while opening: {path.name}")
        while True:
            chunk = os.read(fd, 1024 * 1024)
            if not chunk:
                break
            if len(head) < 64:
                head.extend(chunk[: 64 - len(head)])
            size += len(chunk)
            digest.update(chunk)
    finally:
        os.close(fd)
    return digest.hexdigest(), size, bytes(head)


def _open_board_file(board: Path, path: Path) -> int:
    """Open a board file by directory descriptors, refusing symlinks at every level."""
    relative = path.relative_to(board)
    parts = PurePosixPath(relative.as_posix()).parts
    if not parts or PurePosixPath(relative.as_posix()).is_absolute() or ".." in parts:
        raise OpError("VALIDATION_ERROR", "issue-media path is outside the board")
    nofollow = getattr(os, "O_NOFOLLOW", 0)
    directory_flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | nofollow
    fd = os.open(board, directory_flags)
    try:
        for part in parts[:-1]:
            child = os.open(part, directory_flags, dir_fd=fd)
            os.close(fd)
            fd = child
        result = os.open(
            parts[-1], os.O_RDONLY | nofollow | getattr(os, "O_CLOEXEC", 0), dir_fd=fd
        )
    finally:
        os.close(fd)
    info = os.fstat(result)
    if not stat.S_ISREG(info.st_mode):
        os.close(result)
        raise OpError("INTEGRITY_ERROR", "issue-media object is not a regular file")
    return result


def _read_board_file(board: Path, path: Path) -> tuple[str, int, bytes, bytes]:
    fd = _open_board_file(board, path)
    try:
        digest = hashlib.sha256()
        head = bytearray()
        content = bytearray()
        size = 0
        while True:
            chunk = os.read(fd, 1024 * 1024)
            if not chunk:
                break
            if len(head) < 64:
                head.extend(chunk[: 64 - len(head)])
            digest.update(chunk)
            content.extend(chunk)
            size += len(chunk)
        return digest.hexdigest(), size, bytes(head), bytes(content)
    finally:
        os.close(fd)


def _remove_tree(path: Path) -> None:
    """Remove *path* and everything under it without following a link.

    ``unlink_entry`` removes files, links and special files only; a directory
    (an orphaned issue directory, a detached video's ``.frames``) needs its
    children removed first.
    """
    if path.is_symlink() or not path.is_dir():
        unlink_entry(path)
        return
    for child in list(path.iterdir()):
        _remove_tree(child)
    remove_dir(path)


@dataclass
class Upload:
    owner: HostedIssueMedia
    sha256: str
    declared_size: int
    temporary: Path
    reserved: bool
    fd: int
    digest: Any
    head: bytearray
    token_id: str | None = None
    token_quota_reserved: bool = False
    keep_photo_metadata: bool = False
    written: int = 0
    finished: bool = False
    #: Set once the upload has succeeded or been aborted; a later abort is a
    #: no-op, so a stale abort never removes a newer upload's reservation.
    done: bool = False

    def write(self, chunk: bytes) -> None:
        if self.finished:
            raise RuntimeError("upload is already finished")
        if self.written + len(chunk) > self.declared_size:
            raise OpError("VALIDATION_ERROR", "media upload exceeded Content-Length.")
        view = memoryview(chunk)
        while view:
            count = os.write(self.fd, view)
            view = view[count:]
        if len(self.head) < 64:
            self.head.extend(chunk[: 64 - len(self.head)])
        self.digest.update(chunk)
        self.written += len(chunk)

    def finish(self) -> dict:
        if self.finished:
            raise RuntimeError("upload is already finished")
        self.finished = True
        canonical_sha256: str | None = None
        published_new_blob = False
        canonical_reservation_created = False
        try:
            os.fsync(self.fd)
            os.close(self.fd)
            self.fd = -1
            if self.written != self.declared_size:
                raise OpError(
                    "VALIDATION_ERROR", "media upload size does not match Content-Length."
                )
            if self.digest.hexdigest() != self.sha256:
                raise OpError("VALIDATION_ERROR", "media upload sha256 does not match its body.")
            content_type = sniff_media(bytes(self.head))
            if content_type is None:
                raise OpError(
                    "VALIDATION_ERROR", "uploaded content is not a supported photo or video."
                )
            raw = self.temporary.read_bytes()
            photo_status = "not_applicable"
            if content_type in {"image/jpeg", "image/png"}:
                try:
                    raw = strip_photo_metadata(raw, content_type)
                    photo_status = "stripped"
                except PhotoMetadataError as exc:
                    if not self.keep_photo_metadata:
                        raise OpError(
                            "VALIDATION_ERROR",
                            "photo metadata could not be stripped; repair the JPEG/PNG or "
                            "retry with X-Lattice-Keep-Photo-Metadata: true.",
                            {"reason": "PHOTO_METADATA_UNSTRIPPED"},
                        ) from exc
                    photo_status = "kept"
            elif content_type == "image/heic":
                if not self.keep_photo_metadata:
                    raise OpError(
                        "VALIDATION_ERROR",
                        "HEIC uploads require X-Lattice-Keep-Photo-Metadata: true; "
                        "convert to JPEG before upload when possible.",
                        {"reason": "PHOTO_METADATA_UNSTRIPPED"},
                    )
                photo_status = "kept"

            canonical_sha256 = hashlib.sha256(raw).hexdigest()
            canonical_size = len(raw)
            self.owner._write_upload_bytes(self.temporary, raw)
            target = self.owner._blob_path(canonical_sha256)
            metadata_path = self.owner._metadata_path(canonical_sha256)

            with self.owner.lock:
                if canonical_sha256 != self.sha256 and canonical_sha256 in self.owner._inflight:
                    raise OpError("BOARD_BUSY", "canonical media bytes are already being staged.")
                if canonical_sha256 != self.sha256:
                    self.owner._inflight.add(canonical_sha256)
                expected = self.owner._read_stage_metadata(canonical_sha256)
                target_exists = _safe_regular(target) is not None
                canonical_exists = expected is not None and target_exists
                if expected is not None and (
                    expected.get("size_bytes"),
                    expected.get("content_type"),
                    expected.get("photo_metadata_status"),
                ) != (canonical_size, content_type, photo_status):
                    raise OpError(
                        "CONFLICT", "staged sha256 was already used with different media."
                    )
                owners = self.owner._stage_owners(expected or {})
                if self.token_id is not None and self.token_id not in owners:
                    owners.append(self.token_id)
                raw_reservation = self.owner._reserved.get(self.sha256, 0) if self.reserved else 0
                current = self.owner._published_unique_bytes() + self.owner._staged_unique_bytes()
                target_usage = (
                    current - raw_reservation + (0 if canonical_exists else canonical_size)
                )
                if target_usage > self.owner.max_project_bytes:
                    raise OpError(
                        "MEDIA_QUOTA_EXCEEDED",
                        f"project media quota of {self.owner.max_project_bytes} bytes would be exceeded.",
                        {"limit_bytes": self.owner.max_project_bytes, "used_bytes": current},
                    )

                if not canonical_exists:
                    self.owner._reserved[canonical_sha256] = canonical_size
                    canonical_reservation_created = True
                    self.owner._write_reservation(canonical_sha256, canonical_size)
                self.owner._reserve_path(self.sha256).unlink(missing_ok=True)
                self.owner._reserved.pop(self.sha256, None)
                os.replace(self.temporary, target)
                published_new_blob = not canonical_exists
                os.chmod(target, 0o600)
                metadata = dict(expected or {})
                metadata.update(
                    {
                        "sha256": canonical_sha256,
                        "size_bytes": canonical_size,
                        "content_type": content_type,
                        "photo_metadata_status": photo_status,
                        "created_at": metadata.get("created_at", time.time()),
                        "owners": owners,
                    }
                )
                _write_private(
                    metadata_path,
                    json.dumps(metadata, sort_keys=True, separators=(",", ":")) + "\n",
                )
                os.chmod(metadata_path, 0o600)
                self.owner._reserve_path(canonical_sha256).unlink(missing_ok=True)
                self.owner._reserved.pop(canonical_sha256, None)
                alias = {
                    "upload_sha256": self.sha256,
                    "upload_size_bytes": self.written,
                    **metadata,
                }
                self.owner._write_alias(self.sha256, alias)
                if canonical_sha256 != self.sha256:
                    self.owner._inflight.discard(canonical_sha256)
            self.done = True
            return {
                "upload_sha256": self.sha256,
                "sha256": canonical_sha256,
                "size_bytes": canonical_size,
                "content_type": content_type,
                "photo_metadata_status": photo_status,
                "staged": True,
            }
        except BaseException:
            if canonical_reservation_created and canonical_sha256 is not None:
                with self.owner.lock:
                    self.owner._reserve_path(canonical_sha256).unlink(missing_ok=True)
                    self.owner._reserved.pop(canonical_sha256, None)
                    if published_new_blob:
                        self.owner._blob_path(canonical_sha256).unlink(missing_ok=True)
                        self.owner._metadata_path(canonical_sha256).unlink(missing_ok=True)
            self.abort()
            raise
        finally:
            if canonical_sha256 is not None:
                with self.owner.lock:
                    self.owner._inflight.discard(canonical_sha256)
            self.owner._release_upload(self.sha256, self.reserved)
            self.owner._release_token_quota(self.token_id, self.sha256, self.token_quota_reserved)

    def abort(self) -> None:
        if self.done:
            return
        self.done = True
        if self.fd >= 0:
            try:
                os.close(self.fd)
            except OSError:
                pass
            self.fd = -1
        self.temporary.unlink(missing_ok=True)
        if self.reserved or self.token_quota_reserved:
            self.owner._reserve_path(self.sha256).unlink(missing_ok=True)
        self.owner._release_upload(self.sha256, self.reserved)
        self.owner._release_token_quota(self.token_id, self.sha256, self.token_quota_reserved)
        self.finished = True


class HostedIssueMedia:
    """One project's host-private media manager, used under its work lock for commits."""

    def __init__(
        self,
        project_dir: Path,
        board: Path,
        *,
        max_file_bytes: int = DEFAULT_FILE_BYTES,
        max_issue_bytes: int = DEFAULT_ISSUE_BYTES,
        max_project_bytes: int = DEFAULT_PROJECT_BYTES,
        log: Any | None = None,
        slug: str | None = None,
    ) -> None:
        #: The project's server log (``ServerLog``); reconcile actions are logged.
        self.log = log
        self.slug = slug
        self.project_dir = Path(project_dir)
        self.board = Path(board)
        self.root = self.project_dir / ".runtime" / "issue-media"
        self.staging = self.root / "staging"
        self.aliases = self.staging / "aliases"
        self.manifests = self.root / "manifests"
        self.max_file_bytes = max_file_bytes
        self.max_issue_bytes = max_issue_bytes
        self.max_project_bytes = max_project_bytes
        self.lock = threading.Lock()
        self._inflight: set[str] = set()
        self._reserved: dict[str, int] = {}
        #: Re-uploads of an existing shared hash reserve quota for its new owner
        #: in memory until verified bytes are installed and metadata is updated.
        self._pending_token_bytes: dict[str, dict[str, int]] = {}
        #: Bytes of every published object, one per stored path (not per hash).
        self.published_bytes = 0

    def _log(self, level: str, event: str, **fields: Any) -> None:
        if self.log is not None:
            self.log.emit(level, event, project=self.slug, **fields)

    def _make_layout(self) -> None:
        flags = os.O_RDONLY | os.O_DIRECTORY | getattr(os, "O_NOFOLLOW", 0)
        try:
            project_fd = os.open(self.project_dir, flags)
        except OSError as exc:
            raise OpError("INTEGRITY_ERROR", "project media storage root is unsafe") from exc
        opened = [project_fd]
        try:
            runtime_fd = _open_private_child(project_fd, ".runtime")
            opened.append(runtime_fd)
            root_fd = _open_private_child(runtime_fd, "issue-media")
            opened.append(root_fd)
            staging_fd = _open_private_child(root_fd, "staging")
            opened.append(staging_fd)
            opened.append(_open_private_child(staging_fd, "aliases"))
            opened.append(_open_private_child(root_fd, "manifests"))
        finally:
            for fd in reversed(opened):
                os.close(fd)

    def _blob_path(self, sha256: str) -> Path:
        return self.staging / f"{validate_sha256(sha256)}.blob"

    def _metadata_path(self, sha256: str) -> Path:
        return self.staging / f"{validate_sha256(sha256)}.json"

    def _reserve_path(self, sha256: str) -> Path:
        return self.staging / f"{validate_sha256(sha256)}.reserve"

    def _alias_path(self, upload_sha256: str) -> Path:
        return self.aliases / f"{validate_sha256(upload_sha256)}.json"

    def _write_upload_bytes(self, path: Path, content: bytes) -> None:
        flags = os.O_WRONLY | os.O_TRUNC | getattr(os, "O_NOFOLLOW", 0)
        fd = os.open(path, flags)
        try:
            view = memoryview(content)
            while view:
                view = view[os.write(fd, view) :]
            os.fsync(fd)
        finally:
            os.close(fd)

    def _write_reservation(self, sha256: str, size_bytes: int) -> None:
        path = self._reserve_path(sha256)
        _write_private(
            path,
            json.dumps(
                {"sha256": sha256, "size_bytes": size_bytes, "created_at": time.time()},
                separators=(",", ":"),
            )
            + "\n",
        )
        os.chmod(path, 0o600)

    def _write_alias(self, upload_sha256: str, alias: dict) -> None:
        _write_private(
            self._alias_path(upload_sha256),
            json.dumps(alias, sort_keys=True, separators=(",", ":")) + "\n",
        )
        os.chmod(self._alias_path(upload_sha256), 0o600)

    def _manifest_path(self, op_id: str) -> Path:
        if not re.fullmatch(r"op_[0-9A-HJKMNP-TV-Z]{26}", op_id):
            raise OpError("VALIDATION_ERROR", "invalid issue-media operation ID")
        return self.manifests / f"{op_id}.json"

    def _read_stage_metadata(self, sha256: str) -> dict | None:
        path = self._metadata_path(sha256)
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return None
        except (OSError, ValueError) as exc:
            raise OpError(
                "INTEGRITY_ERROR", f"staged media metadata is unreadable: {path.name}"
            ) from exc
        if (
            not isinstance(value, dict)
            or value.get("sha256") != sha256
            or not isinstance(value.get("size_bytes"), int)
            or value["size_bytes"] < 0
            or not isinstance(value.get("content_type"), str)
            or (
                "owners" in value
                and (
                    not isinstance(value["owners"], list)
                    or any(not isinstance(owner, str) or not owner for owner in value["owners"])
                )
            )
        ):
            raise OpError("INTEGRITY_ERROR", f"staged media metadata is invalid: {path.name}")
        return value

    @staticmethod
    def _stage_owners(metadata: dict) -> list[str]:
        owners = metadata.get("owners")
        return list(owners) if isinstance(owners, list) else []

    def _published_unique_bytes(self) -> int:
        return self.published_bytes

    def _staged_unique_bytes(self) -> int:
        sizes = dict(self._reserved)
        for path in self.staging.glob("*.reserve"):
            try:
                raw = json.loads(path.read_text(encoding="utf-8"))
                sha = validate_sha256(raw.get("sha256"))
                size = raw.get("size_bytes")
                if isinstance(size, int) and not isinstance(size, bool) and size >= 0:
                    sizes.setdefault(sha, size)
            except (OSError, ValueError, TypeError, OpError):
                continue
        for path in self.staging.glob("*.json"):
            try:
                raw = json.loads(path.read_text(encoding="utf-8"))
                sha = validate_sha256(raw.get("sha256"))
                if _safe_regular(self._blob_path(sha)) is not None:
                    sizes.setdefault(sha, int(raw["size_bytes"]))
            except (OSError, ValueError, TypeError, KeyError, OpError):
                continue
        return sum(sizes.values())

    def begin_upload(
        self,
        sha256: str,
        size_bytes: int,
        *,
        token_id: str | None = None,
        max_staged_bytes: int | None = None,
        keep_photo_metadata: bool = False,
    ) -> Upload:
        sha256 = validate_sha256(sha256)
        if isinstance(size_bytes, bool) or not isinstance(size_bytes, int) or size_bytes < 0:
            raise OpError(
                "VALIDATION_ERROR", "media Content-Length must be a non-negative integer."
            )
        if size_bytes > self.max_file_bytes:
            raise OpError(
                "PAYLOAD_TOO_LARGE",
                f"media object is over the {self.max_file_bytes} byte per-file limit.",
                {"limit_bytes": self.max_file_bytes},
            )
        self._make_layout()
        with self.lock:
            self._expire_staging_locked(time.time())
            if sha256 in self._inflight:
                raise OpError("BOARD_BUSY", "this media object is already being uploaded.")
            metadata = self._read_stage_metadata(sha256)
            blob = self._blob_path(sha256)
            token_quota_reserved = False
            if token_id is not None and max_staged_bytes is not None:
                already_owned = metadata is not None and token_id in self._stage_owners(metadata)
                pending = self._pending_token_staged_bytes(token_id)
                used = self._token_staged_bytes(token_id) + pending
                if not already_owned and used + size_bytes > max_staged_bytes:
                    raise OpError(
                        "MEDIA_QUOTA_EXCEEDED",
                        f"token staged-media quota of {max_staged_bytes} bytes would be exceeded.",
                        {
                            "scope": "token",
                            "limit_bytes": max_staged_bytes,
                            "used_bytes": used,
                        },
                    )
                token_quota_reserved = (
                    not already_owned and metadata is not None and _safe_regular(blob) is not None
                )
            if metadata is not None and _safe_regular(blob) is not None:
                if metadata["size_bytes"] != size_bytes:
                    raise OpError(
                        "CONFLICT", "staged sha256 was already used with a different size."
                    )
                reserved = False
            else:
                # Storage is not deduplicated: a published hash staged again is
                # a new stored object, so it counts against the quota.
                reserved = True
                current = self._published_unique_bytes() + self._staged_unique_bytes()
                if reserved and current + size_bytes > self.max_project_bytes:
                    raise OpError(
                        "MEDIA_QUOTA_EXCEEDED",
                        f"project media quota of {self.max_project_bytes} bytes would be exceeded.",
                        {"limit_bytes": self.max_project_bytes, "used_bytes": current},
                    )
                self._reserved[sha256] = size_bytes if reserved else 0
                _write_private(
                    self._reserve_path(sha256),
                    json.dumps(
                        {
                            "sha256": sha256,
                            "size_bytes": size_bytes,
                            "created_at": time.time(),
                            "owners": [token_id] if token_id is not None else [],
                        },
                        separators=(",", ":"),
                    )
                    + "\n",
                )
                os.chmod(self._reserve_path(sha256), 0o600)
            self._inflight.add(sha256)
            temp = self.staging / f".{sha256}.{os.getpid()}.{threading.get_ident()}.part"
            flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
            try:
                fd = os.open(temp, flags, 0o600)
            except BaseException:
                self._inflight.discard(sha256)
                if reserved:
                    self._reserved.pop(sha256, None)
                    self._reserve_path(sha256).unlink(missing_ok=True)
                raise
            if token_quota_reserved and token_id is not None:
                self._pending_token_bytes.setdefault(token_id, {})[sha256] = size_bytes
            return Upload(
                self,
                sha256,
                size_bytes,
                temp,
                reserved,
                fd,
                hashlib.sha256(),
                bytearray(),
                token_id,
                token_quota_reserved,
                keep_photo_metadata,
            )

    def _pending_token_staged_bytes(self, token_id: str) -> int:
        pending = 0
        for sha256, size in self._pending_token_bytes.get(token_id, {}).items():
            metadata = self._read_stage_metadata(sha256)
            if metadata is None or token_id not in self._stage_owners(metadata):
                pending += size
        return pending

    def _token_staged_bytes(self, token_id: str) -> int:
        referenced: set[str] = set()
        for path in self.manifests.glob("op_*.json"):
            try:
                manifest = json.loads(path.read_text(encoding="utf-8"))
                referenced.update(
                    validate_sha256(item.get("sha256")) for item in manifest.get("objects", [])
                )
            except (OSError, ValueError, TypeError, AttributeError, OpError):
                continue
        sizes: dict[str, int] = {}
        for path in (*self.staging.glob("*.json"), *self.staging.glob("*.reserve")):
            try:
                raw = json.loads(path.read_text(encoding="utf-8"))
                sha = validate_sha256(raw.get("sha256"))
                owners = raw.get("owners", [])
                size = raw.get("size_bytes")
                if (
                    sha not in referenced
                    and token_id in owners
                    and isinstance(size, int)
                    and not isinstance(size, bool)
                    and size >= 0
                    and (
                        path.suffix == ".reserve"
                        or _safe_regular(self._blob_path(sha)) is not None
                    )
                ):
                    sizes[sha] = size
            except (OSError, ValueError, TypeError, OpError):
                continue
        return sum(sizes.values())

    def _release_upload(self, sha256: str, reserved: bool) -> None:
        with self.lock:
            self._inflight.discard(sha256)
            if reserved:
                metadata = self._read_stage_metadata(sha256)
                if metadata is None:
                    self._reserved.pop(sha256, None)
            else:
                self._reserved.pop(sha256, None)

    def _release_token_quota(self, token_id: str | None, sha256: str, reserved: bool) -> None:
        if token_id is None or not reserved:
            return
        with self.lock:
            pending = self._pending_token_bytes.get(token_id)
            if pending is not None:
                pending.pop(sha256, None)
                if not pending:
                    self._pending_token_bytes.pop(token_id, None)

    def _expire_staging_locked(self, now: float) -> int:
        """Remove expired unreferenced uploads while holding ``self.lock``."""
        referenced: set[str] = set()
        for path in self.manifests.glob("op_*.json"):
            try:
                manifest = json.loads(path.read_text(encoding="utf-8"))
                referenced.update(
                    validate_sha256(item.get("sha256")) for item in manifest.get("objects", [])
                )
            except (OSError, ValueError, TypeError, AttributeError, OpError):
                continue
        removed = 0
        for path in self.staging.glob("*.json"):
            try:
                raw = json.loads(path.read_text(encoding="utf-8"))
                sha = validate_sha256(raw.get("sha256"))
                if (
                    sha in referenced
                    or sha in self._inflight
                    or now - float(raw.get("created_at", 0)) < STAGE_TTL_SECONDS
                ):
                    continue
                self._blob_path(sha).unlink(missing_ok=True)
                self._reserve_path(sha).unlink(missing_ok=True)
                path.unlink(missing_ok=True)
                self._reserved.pop(sha, None)
                removed += 1
            except (OSError, ValueError, TypeError, OpError):
                continue
        for path in self.staging.glob("*.reserve"):
            try:
                raw = json.loads(path.read_text(encoding="utf-8"))
                sha = validate_sha256(raw.get("sha256"))
                if (
                    sha not in self._inflight
                    and now - float(raw.get("created_at", 0)) >= STAGE_TTL_SECONDS
                ):
                    path.unlink(missing_ok=True)
                    self._reserved.pop(sha, None)
            except (OSError, ValueError, TypeError, OpError):
                continue
        for path in self.aliases.glob("*.json"):
            try:
                raw = json.loads(path.read_text(encoding="utf-8"))
                if now - float(raw.get("created_at", 0)) >= STAGE_TTL_SECONDS:
                    path.unlink(missing_ok=True)
            except (OSError, ValueError, TypeError):
                continue
        return removed

    def expire_staging(self, now: float | None = None) -> int:
        """Remove expired unreferenced uploads and their reservation records."""
        with self.lock:
            return self._expire_staging_locked(time.time() if now is None else now)

    def verify_staged(
        self,
        sha256: str,
        size_bytes: int,
        *,
        token_id: str | None = None,
        require_owner: bool = False,
    ) -> dict:
        sha256 = validate_sha256(sha256)
        metadata = self._read_stage_metadata(sha256)
        if metadata is None:
            raise OpError("NOT_FOUND", f"staged media object {sha256} not found for this project.")
        if metadata["size_bytes"] != size_bytes:
            raise OpError("VALIDATION_ERROR", "staged media size does not match the operation.")
        if require_owner and (token_id is None or token_id not in self._stage_owners(metadata)):
            raise OpError("NOT_FOUND", f"staged media object {sha256} not found for this project.")
        actual_hash, actual_size, head = _digest_file(self._blob_path(sha256))
        content_type = sniff_media(head)
        if (
            actual_hash != sha256
            or actual_size != size_bytes
            or content_type != metadata["content_type"]
        ):
            raise OpError(
                "INTEGRITY_ERROR", "staged media bytes failed hash, size, or type verification."
            )
        if not isinstance(metadata.get("photo_metadata_status"), str):
            metadata["photo_metadata_status"] = "unverified"
        return metadata

    def add_manifest(self, op_id: str, issue_id: str, objects: list[dict]) -> None:
        if not validate_id(issue_id, "iss"):
            raise OpError("VALIDATION_ERROR", "invalid issue ID for staged media")
        path = self._manifest_path(op_id)
        normalized = []
        for raw in objects:
            sha = validate_sha256(raw.get("sha256"))
            size = raw.get("size_bytes")
            if isinstance(size, bool) or not isinstance(size, int) or size < 0:
                raise OpError("VALIDATION_ERROR", "invalid staged media size")
            media_id = raw.get("media_id")
            if not validate_id(media_id, "med"):
                raise OpError("VALIDATION_ERROR", "invalid media ID for staged media")
            t_ms = raw.get("t_ms")
            if t_ms is not None and (
                isinstance(t_ms, bool) or not isinstance(t_ms, int) or t_ms < 0
            ):
                raise OpError("VALIDATION_ERROR", "invalid frame timestamp for staged media")
            metadata = self.verify_staged(sha, size)
            target = raw.get("target")
            expected = (
                f"issues/media/{issue_id}/{media_id}{media_ext(metadata['content_type'])}"
                if t_ms is None
                else f"issues/media/{issue_id}/{media_id}.frames/{frame_name(t_ms)}"
            )
            if target != expected:
                raise OpError(
                    "VALIDATION_ERROR", "staged media target does not match its metadata"
                )
            normalized.append(
                {
                    "media_id": media_id,
                    "t_ms": t_ms,
                    "sha256": sha,
                    "size_bytes": size,
                    "content_type": metadata["content_type"],
                    "staged_path": f".runtime/issue-media/staging/{sha}.blob",
                    "target": expected,
                }
            )
        body = {
            "version": 1,
            "op_id": op_id,
            "issue_id": issue_id,
            "created_at": time.time(),
            "objects": normalized,
        }
        if path.exists():
            try:
                existing = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError) as exc:
                raise OpError(
                    "INTEGRITY_ERROR", "issue-media operation manifest is unreadable"
                ) from exc
            if existing.get("op_id") != op_id or existing.get("objects") != normalized:
                raise OpError("CONFLICT", "operation ID already has a different media manifest")
            return
        _write_private(path, json.dumps(body, sort_keys=True, indent=2) + "\n")
        os.chmod(path, 0o600)

    def abort_operation(self, op_id: str, *, keep: frozenset[str] = frozenset()) -> None:
        """Drop an uncommitted operation's manifest and its now-unused stages.
        A stage whose hash is in *keep* (bytes a snapshot still needs) stays."""
        path = self._manifest_path(op_id)
        try:
            manifest = json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return
        except (OSError, ValueError):
            return
        path.unlink(missing_ok=True)
        self._remove_unreferenced_stage(
            {item.get("sha256") for item in manifest.get("objects", []) if isinstance(item, dict)},
            keep=keep,
        )

    def finalize_operation(self, op_id: str) -> bool:
        """Publish one committed operation's objects from its durable manifest."""
        path = self._manifest_path(op_id)
        try:
            manifest = json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return False
        except (OSError, ValueError) as exc:
            raise OpError(
                "INTEGRITY_ERROR", "issue-media operation manifest is unreadable"
            ) from exc
        issue_id = manifest.get("issue_id")
        if not validate_id(issue_id, "iss") or manifest.get("op_id") != op_id:
            raise OpError("INTEGRITY_ERROR", "issue-media operation manifest has invalid identity")
        objects = manifest.get("objects")
        if not isinstance(objects, list):
            raise OpError("INTEGRITY_ERROR", "issue-media operation manifest has invalid objects")
        snapshot = read_issue_snapshot(self.board, issue_id)
        present = {
            m.get("id"): m for m in (snapshot or {}).get("media", []) if not m.get("removed")
        }
        referenced = []
        for item in objects:
            if not isinstance(item, dict):
                raise OpError(
                    "INTEGRITY_ERROR", "issue-media operation manifest has an invalid object"
                )
            sha = validate_sha256(item.get("sha256"))
            media_id = item.get("media_id")
            if not validate_id(media_id, "med"):
                raise OpError(
                    "INTEGRITY_ERROR", "issue-media operation manifest has an invalid media ID"
                )
            size_bytes = item.get("size_bytes")
            if isinstance(size_bytes, bool) or not isinstance(size_bytes, int) or size_bytes < 0:
                raise OpError(
                    "INTEGRITY_ERROR", "issue-media operation manifest has an invalid size"
                )
            t_ms = item.get("t_ms")
            if t_ms is not None and (
                isinstance(t_ms, bool)
                or not isinstance(t_ms, int)
                or t_ms < 0
                or t_ms > 86_400_000
            ):
                raise OpError(
                    "INTEGRITY_ERROR",
                    "issue-media operation manifest has an invalid frame timestamp",
                )
            entry = present.get(media_id)
            if entry is None:
                raise OpError(
                    "INTEGRITY_ERROR",
                    "committed issue-media object is missing from the current issue snapshot",
                )
            if t_ms is None and entry.get("sha256") != sha:
                raise OpError(
                    "INTEGRITY_ERROR",
                    "committed issue-media hash does not match the current issue snapshot",
                )
            if t_ms is not None:
                parent = frames_dir(self.board, issue_id, entry)
                if parent is None:
                    raise OpError("INTEGRITY_ERROR", "issue-media frame target is invalid")
                target = parent / frame_name(t_ms)
                expected_target = target.relative_to(self.board).as_posix()
                if item.get("target") != expected_target:
                    raise OpError("INTEGRITY_ERROR", "issue-media frame target is invalid")
            else:
                expected_path = media_path(self.board, issue_id, entry)
                if expected_path is None:
                    raise OpError(
                        "INTEGRITY_ERROR", "issue-media target does not match the issue snapshot"
                    )
                target = expected_path
                expected_target = target.relative_to(self.board).as_posix()
                if item.get("target") != expected_target:
                    raise OpError(
                        "INTEGRITY_ERROR", "issue-media target does not match the issue snapshot"
                    )
            if self._matches(target, sha, size_bytes):
                referenced.append(item)
                continue
            metadata = self.verify_staged(sha, size_bytes)
            ensure_dir(target.parent)
            data = self._blob_path(sha).read_bytes()
            if len(data) != size_bytes or hashlib.sha256(data).hexdigest() != sha:
                raise OpError("INTEGRITY_ERROR", "staged media changed during finalization")
            if item.get("t_ms") is not None and metadata["content_type"] != "image/jpeg":
                raise OpError("INTEGRITY_ERROR", "video frame is not a JPEG")
            atomic_write(target, data)
            os.chmod(target, 0o600)
            if not self._matches(target, sha, size_bytes):
                raise OpError(
                    "INTEGRITY_ERROR", "published issue media failed final-path verification"
                )
            referenced.append(item)
        path.unlink(missing_ok=True)
        self._remove_unreferenced_stage(
            {item.get("sha256") for item in manifest.get("objects", []) if isinstance(item, dict)}
        )
        self.published_bytes = self.scan_published()
        return True

    def _matches(self, path: Path, sha256: str, size_bytes: int) -> bool:
        try:
            actual, size, _head, _content = _read_board_file(self.board, path)
        except (OSError, OpError, ValueError):
            return False
        return actual == sha256 and size == size_bytes

    def _remove_unreferenced_stage(
        self, hashes: set[object], *, keep: frozenset[str] = frozenset()
    ) -> None:
        still_used: set[str] = set()
        for path in self.manifests.glob("op_*.json"):
            try:
                manifest = json.loads(path.read_text(encoding="utf-8"))
                still_used.update(
                    validate_sha256(item.get("sha256")) for item in manifest.get("objects", [])
                )
            except (OSError, ValueError, TypeError, AttributeError, OpError):
                continue
        for value in hashes:
            try:
                sha = validate_sha256(value)
            except OpError:
                continue
            if sha in still_used or sha in self._inflight or sha in keep:
                continue
            self._blob_path(sha).unlink(missing_ok=True)
            self._metadata_path(sha).unlink(missing_ok=True)
            self._reserve_path(sha).unlink(missing_ok=True)
            self._reserved.pop(sha, None)

    def reconcile(self, journal: Any) -> None:
        """Resolve leftover manifests once the undo logs are settled (project load).

        An operation committed when its issue snapshot lists its originals
        (media IDs are fresh per operation, and an unfinished operation has
        already been rolled back). The current epoch's journal is a second
        witness, never the only one: a maintenance or restore rotation between a
        crash and this load moves the committing line into a kept journal. Staged
        bytes a snapshot still needs are never removed. Every action is logged.
        """
        self._make_layout()
        with self.lock:
            # Crash leftovers: temp files and reservation records of uploads that
            # are not running now (a reload can overlap a live upload).
            for leftover in (*self.staging.glob(".*.part"), *self.staging.glob(".*.tmp")):
                if leftover.name.split(".")[1] not in self._inflight:
                    leftover.unlink(missing_ok=True)
                    self._log("info", "issue_media_reconcile_sweep", path=leftover.name)
            for record in self.staging.glob("*.reserve"):
                sha = record.name[: -len(".reserve")]
                if sha not in self._inflight:
                    record.unlink(missing_ok=True)
                    self._reserved.pop(sha, None)
                    self._log("info", "issue_media_reconcile_sweep", path=record.name)
        for leftover in self.manifests.glob(".*.tmp"):
            leftover.unlink(missing_ok=True)
            self._log("info", "issue_media_reconcile_sweep", path=leftover.name)
        expired = self.expire_staging()
        if expired:
            self._log("info", "issue_media_reconcile_expired", stages=expired)
        committed = {
            entry.get("op_id")
            for entry in journal.read_entries()
            if entry.get("op") in {"issue.file", "issue.attach"}
            and isinstance(entry.get("op_id"), str)
        }
        needed = self._unpublished_snapshot_hashes()
        for path in sorted(self.manifests.glob("op_*.json")):
            op_id = path.stem
            verdict = op_id in committed or self._listed_by_snapshot(path)
            if verdict is None:
                self._log(
                    "warning",
                    "issue_media_reconcile_kept",
                    op_id=op_id,
                    reason="the manifest or its issue snapshot is unreadable",
                )
            elif verdict:
                self._log("info", "issue_media_reconcile_finalize", op_id=op_id)
                try:
                    self.finalize_operation(op_id)
                except (OpError, OSError) as exc:
                    # A committed operation whose bytes cannot be published (the
                    # staged copy was lost or unreadable, a media folder is
                    # read-only, the disk errored) must not take the whole project
                    # offline: keep the manifest, say so, and let doctor and the
                    # media route report the missing object.
                    self._log(
                        "error",
                        "issue_media_reconcile_failed",
                        op_id=op_id,
                        code=exc.code if isinstance(exc, OpError) else type(exc).__name__,
                        reason="its media could not be published; the project still loads",
                    )
            else:
                self._log("info", "issue_media_reconcile_abort", op_id=op_id)
                self.abort_operation(op_id, keep=needed)
        self.sweep_orphans()
        self.published_bytes = self.scan_published()

    def _listed_by_snapshot(self, path: Path) -> bool | None:
        """Whether the issue snapshot lists the manifest's originals (then the
        operation committed); ``None`` when that cannot be read."""
        try:
            manifest = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        if not isinstance(manifest, dict) or not isinstance(manifest.get("objects"), list):
            return None
        issue_id = manifest.get("issue_id")
        originals = {
            (item.get("media_id"), item.get("sha256"))
            for item in manifest["objects"]
            if isinstance(item, dict) and item.get("t_ms") is None
        }
        if not validate_id(issue_id, "iss") or not originals:
            return False
        try:
            snapshot = read_issue_snapshot(self.board, issue_id)
        except OpError:
            return None
        listed = {
            (entry.get("id"), entry.get("sha256"))
            for entry in (snapshot or {}).get("media", [])
            if not entry.get("removed")
        }
        return bool(originals & listed)

    def _unpublished_snapshot_hashes(self) -> frozenset[str]:
        """Hashes a present snapshot lists whose published original is missing or
        the wrong size: a staged copy may be their only bytes (lstat only)."""
        needed: set[str] = set()
        for snapshot in list_issue_snapshots(self.board, on_unreadable=lambda *_: None):
            issue_id = snapshot.get("id")
            if not validate_id(issue_id, "iss"):
                continue
            for entry in snapshot.get("media", []):
                if entry.get("removed") or not isinstance(entry.get("sha256"), str):
                    continue
                try:
                    path = media_path(self.board, issue_id, entry)
                    info = os.lstat(path) if path is not None else None
                except (OSError, ValueError, OpError):
                    info = None
                if (
                    info is None
                    or not stat.S_ISREG(info.st_mode)
                    or info.st_size != entry.get("size_bytes")
                ):
                    needed.add(entry["sha256"])
        return frozenset(needed)

    def finalize_removed(self, events: list | tuple) -> None:
        """Unlink detached media only after its removal event has committed."""
        for event in events:
            if not isinstance(event, dict) or event.get("type") != "issue_media_removed":
                continue
            issue_id = event.get("issue_id")
            media_id = event.get("data", {}).get("media_id")
            if not validate_id(issue_id, "iss") or not validate_id(media_id, "med"):
                raise OpError("INTEGRITY_ERROR", "committed media removal has an invalid identity")
            snapshot = read_issue_snapshot(self.board, issue_id)
            entry = next(
                (row for row in (snapshot or {}).get("media", []) if row.get("id") == media_id),
                None,
            )
            if entry is not None and entry.get("removed"):
                delete_media_files(self.board, issue_id, entry)
        # The freed bytes leave the project quota now, not at the next reload.
        self.published_bytes = self.scan_published()

    def sweep_orphans(self) -> None:
        """Delete paths no longer referenced by a present issue snapshot."""
        root = self.board / "issues" / "media"
        if not os.path.lexists(root):
            return
        info = os.lstat(root)
        if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
            _remove_tree(root)
            return
        active: dict[str, set[str]] = {}
        unreadable: set[str] = set()
        for snapshot in list_issue_snapshots(
            self.board, on_unreadable=lambda path, _exc: unreadable.add(Path(path).stem)
        ):
            issue_id = snapshot.get("id")
            if not validate_id(issue_id, "iss"):
                continue
            active[issue_id] = {
                str(entry.get("id"))
                for entry in snapshot.get("media", [])
                if not entry.get("removed")
            }
        for issue_dir in list(root.iterdir()):
            if issue_dir.name in unreadable:
                continue  # its snapshot could not be read: keep its media for repair
            if issue_dir.name not in active or not validate_id(issue_dir.name, "iss"):
                _remove_tree(issue_dir)
                continue
            issue_ids = active[issue_dir.name]
            if issue_dir.is_symlink() or not issue_dir.is_dir():
                _remove_tree(issue_dir)
                continue
            for child in list(issue_dir.iterdir()):
                media_id = child.name.split(".", 1)[0]
                is_frame_dir = child.name.endswith(".frames")
                if media_id not in issue_ids or not validate_id(media_id, "med"):
                    _remove_tree(child)
                elif is_frame_dir and (child.is_symlink() or not child.is_dir()):
                    _remove_tree(child)
                elif not is_frame_dir and (child.is_symlink() or not child.is_file()):
                    _remove_tree(child)
            if not any(issue_dir.iterdir()):
                _remove_tree(issue_dir)

    def scan_published(self) -> int:
        """Bytes of every regular file under the private media tree (no hashing)."""
        root = self.board / "issues" / "media"
        total = 0
        if not root.is_dir() or root.is_symlink():
            return total
        for directory, dirs, files in os.walk(root, followlinks=False):
            base = Path(directory)
            dirs[:] = [name for name in dirs if not (base / name).is_symlink()]
            for name in files:
                try:
                    info = os.lstat(base / name)
                except OSError:
                    continue
                if stat.S_ISREG(info.st_mode):
                    total += info.st_size
        return total

    def issue_bytes(self, issue_id: str) -> int:
        """Actual regular-file bytes for a present issue, including frame sidecars."""
        if not validate_id(issue_id, "iss"):
            return 0
        root = self.board / "issues" / "media" / issue_id
        total = 0
        if not root.is_dir() or root.is_symlink():
            return 0
        for directory, dirs, files in os.walk(root, followlinks=False):
            base = Path(directory)
            dirs[:] = [name for name in dirs if not (base / name).is_symlink()]
            for name in files:
                path = base / name
                try:
                    info = os.lstat(path)
                except OSError:
                    continue
                if stat.S_ISREG(info.st_mode):
                    total += info.st_size
        return total

    def check_issue_quota(self, issue_id: str, additions: list[int]) -> None:
        """Refuse additions that would exceed the server's issue quota, frames included."""
        current = self.issue_bytes(issue_id)
        requested = current + sum(additions)
        if requested > self.max_issue_bytes:
            raise OpError(
                "PAYLOAD_TOO_LARGE",
                f"issue {issue_id} would hold {requested} bytes of media; the server limit is "
                f"{self.max_issue_bytes} bytes per issue, including frames.",
                {"limit_bytes": self.max_issue_bytes, "size_bytes": requested},
            )


@dataclass(frozen=True)
class MediaRead:
    body: bytes
    content_type: str
    sha256: str
    size_bytes: int
    status: int
    content_range: str | None


@dataclass(frozen=True)
class MediaPlan:
    """What one read needs, decided under the project lock from the snapshot."""

    board: Path
    path: Path
    content_type: str
    expected_hash: str | None
    expected_size: int | None
    is_frame: bool


_VERIFIED: dict[tuple[int, int, int, int], str] = {}
_VERIFIED_LOCK = threading.Lock()
_VERIFIED_MAX = 8192
_CHUNK = 1024 * 1024


def _verified_digest(fd: int, info: os.stat_result) -> str:
    """The object's SHA-256, computed once per file identity (device, inode, size,
    mtime). Published media is only ever replaced atomically, so an unchanged
    identity is unchanged bytes; the hash is streamed, never held in memory."""
    key = (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns)
    with _VERIFIED_LOCK:
        known = _VERIFIED.get(key)
    if known is not None:
        return known
    digest = hashlib.sha256()
    offset = 0
    while True:
        chunk = os.pread(fd, _CHUNK, offset)
        if not chunk:
            break
        digest.update(chunk)
        offset += len(chunk)
    value = digest.hexdigest()
    with _VERIFIED_LOCK:
        if len(_VERIFIED) >= _VERIFIED_MAX:
            _VERIFIED.clear()
        _VERIFIED[key] = value
    return value


def plan_media_read(
    board: Path, issue_id: str, media_id: str, *, frame_name_value: str | None = None
) -> MediaPlan:
    """Resolve one read against the current snapshot (call under the project lock)."""
    from lattice.core.issue_media import parse_frame_name

    if not validate_id(issue_id, "iss") or not validate_id(media_id, "med"):
        raise OpError("NOT_FOUND", "issue media not found.")
    snapshot = read_issue_snapshot(board, issue_id)
    entry = next(
        (
            item
            for item in (snapshot or {}).get("media", [])
            if item.get("id") == media_id and not item.get("removed")
        ),
        None,
    )
    if entry is None:
        raise OpError("NOT_FOUND", "issue media not found.")

    if frame_name_value is None:
        sha = validate_sha256(entry.get("sha256"))
        size_expected = entry.get("size_bytes")
        if (
            isinstance(size_expected, bool)
            or not isinstance(size_expected, int)
            or size_expected < 0
        ):
            raise OpError("INTEGRITY_ERROR", "issue media size is invalid")
        path = media_path(board, issue_id, entry)
        if path is None:
            raise OpError("INTEGRITY_ERROR", "issue media path is invalid")
        content_type = entry.get("content_type")
        if not isinstance(content_type, str) or media_ext(content_type) is None:
            raise OpError("INTEGRITY_ERROR", "issue media content type is invalid")
        return MediaPlan(board, path, content_type, sha, size_expected, False)
    t_ms = parse_frame_name(frame_name_value)
    if t_ms is None or t_ms > 86_400_000:
        raise OpError("NOT_FOUND", "issue media frame not found.")
    parent = frames_dir(board, issue_id, entry)
    if parent is None:
        raise OpError("INTEGRITY_ERROR", "issue media frame path is invalid")
    return MediaPlan(board, parent / frame_name_value, "image/jpeg", None, None, True)


#: Bytes per disk read when a response streams an object (never the whole file).
STREAM_CHUNK = 1024 * 1024


@dataclass(frozen=True)
class MediaStream:
    """A verified object span to stream: what to send, and the file identity
    (device, inode, size, mtime) its hash was verified at."""

    plan: MediaPlan
    identity: tuple[int, int, int, int]
    start: int
    length: int
    content_type: str
    sha256: str
    size_bytes: int
    status: int
    content_range: str | None


def _open_planned(plan: MediaPlan) -> int:
    try:
        return _open_board_file(plan.board, plan.path)
    except FileNotFoundError:
        raise OpError("NOT_FOUND", "issue media bytes are not available on this server.") from None
    except OpError:
        raise
    except OSError as exc:
        if getattr(exc, "errno", None) in {2, 20, 40}:  # ENOENT, ENOTDIR, ELOOP
            raise OpError(
                "NOT_FOUND", "issue media bytes are not available on this server."
            ) from None
        raise OpError("INTEGRITY_ERROR", "issue media could not be read safely.") from exc


def _identity(info: os.stat_result) -> tuple[int, int, int, int]:
    return (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns)


def open_media(plan: MediaPlan, range_header: str | None = None) -> MediaStream:
    """Verify a planned object and decide its one bounded range, reading no body.
    Takes no project lock: it opens the file by descriptor and hashes it once per
    identity (streamed, never held in memory)."""
    from lattice.core.issue_media import MAX_FRAME_BYTES

    fd = _open_planned(plan)
    try:
        info = os.fstat(fd)
        actual_size = info.st_size
        actual_hash = _verified_digest(fd, info)
        if plan.expected_hash is not None and (
            actual_hash != plan.expected_hash or actual_size != plan.expected_size
        ):
            raise OpError(
                "INTEGRITY_ERROR", "issue media bytes do not match their recorded hash and size."
            )
        if plan.is_frame and (
            actual_size > MAX_FRAME_BYTES or sniff_media(os.pread(fd, 64, 0)) != "image/jpeg"
        ):
            raise OpError("INTEGRITY_ERROR", "issue media frame is not a supported JPEG object.")
        start, end, status = _range_bounds(range_header, actual_size)
        if status == 206:
            length, content_range = end - start + 1, f"bytes {start}-{end}/{actual_size}"
        else:
            start, length, content_range = 0, actual_size, None
        return MediaStream(
            plan,
            _identity(info),
            start,
            length,
            plan.content_type,
            actual_hash,
            actual_size,
            status,
            content_range,
        )
    finally:
        os.close(fd)


def _pread_chunk(fd: int, length: int, offset: int) -> bytes:
    return os.pread(fd, length, offset)


def iter_media(stream: MediaStream) -> Iterator[bytes]:
    """The span's bytes in reads of at most :data:`STREAM_CHUNK`. The file is
    opened only when iteration starts (an unsent response holds no descriptor),
    and a file whose identity changed since verification is refused."""
    fd = _open_planned(stream.plan)
    try:
        if _identity(os.fstat(fd)) != stream.identity:
            raise OpError("INTEGRITY_ERROR", "issue media changed while it was being served.")
        offset, remaining = stream.start, stream.length
        while remaining > 0:
            chunk = _pread_chunk(fd, min(STREAM_CHUNK, remaining), offset)
            if not chunk:
                raise OpError("INTEGRITY_ERROR", "issue media shrank while it was being served.")
            offset += len(chunk)
            remaining -= len(chunk)
            yield chunk
    finally:
        os.close(fd)


def serve_media(plan: MediaPlan, range_header: str | None = None) -> MediaRead:
    """Verify and read a planned object (or its one bounded range) into memory.
    The HTTP routes stream with :func:`open_media` and :func:`iter_media`."""
    stream = open_media(plan, range_header)
    body = b"".join(iter_media(stream))
    return MediaRead(
        body,
        stream.content_type,
        stream.sha256,
        stream.size_bytes,
        stream.status,
        stream.content_range,
    )


def read_media(
    board: Path,
    issue_id: str,
    media_id: str,
    *,
    frame_name_value: str | None = None,
    range_header: str | None = None,
) -> MediaRead:
    """Plan and serve one read in a single call (the routes split the two phases)."""
    plan = plan_media_read(board, issue_id, media_id, frame_name_value=frame_name_value)
    return serve_media(plan, range_header)


def _range_bounds(value: str | None, size: int) -> tuple[int, int, int]:
    if value is None:
        return 0, max(0, size - 1), 200
    if len(value) > 128 or not value.startswith("bytes=") or "," in value:
        raise OpError(
            "RANGE_NOT_SATISFIABLE",
            "only one byte range is supported.",
            {"size_bytes": size},
        )
    spec = value[6:].strip()
    match = re.fullmatch(r"(\d*)-(\d*)", spec)
    if match is None or (not match[1] and not match[2]):
        raise OpError("RANGE_NOT_SATISFIABLE", "invalid byte range.", {"size_bytes": size})
    if len(match[1]) > 20 or len(match[2]) > 20:
        raise OpError(
            "RANGE_NOT_SATISFIABLE",
            "byte range is outside the media object.",
            {"size_bytes": size},
        )
    if size == 0:
        raise OpError("RANGE_NOT_SATISFIABLE", "byte range is empty.", {"size_bytes": size})
    if not match[1]:
        suffix = int(match[2])
        if suffix <= 0:
            raise OpError(
                "RANGE_NOT_SATISFIABLE", "invalid suffix byte range.", {"size_bytes": size}
            )
        start, end = max(0, size - suffix), size - 1
    else:
        start = int(match[1])
        end = int(match[2]) if match[2] else size - 1
        if start >= size or end < start:
            raise OpError(
                "RANGE_NOT_SATISFIABLE",
                "byte range is outside the media object.",
                {"size_bytes": size},
            )
        end = min(end, size - 1)
    # Each response carries at most MAX_RANGE_BYTES (as the local dashboard's range
    # helper does): a longer or open-ended range is shortened, so a browser's
    # ``bytes=0-`` on a larger video gets its first megabyte and asks for the next.
    end = min(end, start + MAX_RANGE_BYTES - 1)
    return start, end, 206


def _facts(board: Path, path: Path, *, digest: bool) -> tuple[int, bytes, str | None]:
    """``(size, first 64 bytes, digest or None)`` of a regular file opened by
    descriptor. The digest is the cached once-per-identity hash; without it
    nothing is read beyond the head."""
    fd = _open_board_file(board, path)
    try:
        info = os.fstat(fd)
        head = os.pread(fd, 64, 0)
        return info.st_size, head, (_verified_digest(fd, info) if digest else None)
    finally:
        os.close(fd)


def available_media(board: Path, issue_ids: list[str]) -> dict:
    """Object metadata for present issues; never returns media bytes.

    An original is reported when it is a regular file of the recorded size and
    type; the client verifies its hash on download, and a read verifies it again.
    A frame's hash is reported (it is derived data the snapshot does not carry),
    computed once per file identity."""
    from lattice.storage.issue_media import list_frames
    from lattice.storage.issue_media import media_path as board_media_path

    result: dict[str, list[dict]] = {}
    for issue_id in issue_ids:
        if not validate_id(issue_id, "iss"):
            raise OpError("VALIDATION_ERROR", f"invalid issue ID {issue_id!r}")
        snapshot = read_issue_snapshot(board, issue_id)
        entries = []
        if snapshot is not None:
            for entry in snapshot.get("media", []):
                if entry.get("removed"):
                    continue
                sha = validate_sha256(entry.get("sha256"))
                path = board_media_path(board, issue_id, entry)
                try:
                    size, head, _ = (
                        _facts(board, path, digest=False) if path is not None else (-1, b"", None)
                    )
                except (OSError, OpError):
                    continue
                if size != entry.get("size_bytes") or sniff_media(head) != entry.get(
                    "content_type"
                ):
                    continue
                frames = []
                for t_ms, frame in list_frames(board, issue_id, entry):
                    try:
                        frame_size, frame_head, frame_digest = _facts(board, frame, digest=True)
                    except (OSError, OpError):
                        continue
                    if frame_size > 2 * 1024 * 1024 or sniff_media(frame_head) != "image/jpeg":
                        continue
                    frames.append({"t_ms": t_ms, "sha256": frame_digest, "size_bytes": frame_size})
                entries.append(
                    {
                        "media_id": entry["id"],
                        "sha256": sha,
                        "size_bytes": size,
                        "content_type": entry["content_type"],
                        "frames": frames,
                    }
                )
        result[issue_id] = entries
    return {"issues": result}


__all__ = [
    "HostedIssueMedia",
    "MediaRead",
    "MediaStream",
    "Upload",
    "MediaPlan",
    "available_media",
    "iter_media",
    "open_media",
    "plan_media_read",
    "read_media",
    "serve_media",
    "validate_sha256",
]
