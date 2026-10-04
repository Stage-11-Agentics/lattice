"""What the ``issue.*`` operations share (LAT-361). Not an operation module.

Every issue operation first checks :func:`require_issue_log`: a bound client
routes writes to the server, server-owned boards run them transactionally, and
the issue log must be enabled (``ISSUES_DISABLED``). Writes go through
:func:`append`, which replays the issue's log under its lock, lets the caller
decide, and writes the event and the new snapshot.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import re
from collections.abc import Callable
from dataclasses import dataclass

from lattice.core.config import issues_enabled
from lattice.core.events import create_issue_event
from lattice.core.ids import generate_media_id
from lattice.core.issue_media import (
    ACCEPTED_FORMATS_TEXT,
    ISSUE_MEDIA_TOO_LARGE,
    MAX_FRAME_BYTES,
    MAX_FRAMES,
    MEDIA_FILE_TOO_LARGE,
    PhotoMetadataError,
    SNIFF_BYTES,
    clean_original_name,
    file_too_large_message,
    format_size,
    image_dimensions,
    issue_too_large_message,
    media_kind,
    media_limits,
    sniff_media,
    strip_photo_metadata,
)
from lattice.core.issues import (
    apply_issue_event,
    hosted_issues_disabled_message,
    issues_disabled_message,
)
from lattice.core.visibility import require_not_tombstoned
from lattice.ops.base import CommonParams, OpContext, OpError, OpResult
from lattice.ops.task_attach import decode_payload
from lattice.storage.issue_media import delete_media_files, store_media
from lattice.storage.issues import (
    current_issue,
    has_issue_metadata,
    issue_views,
    issue_write_context,
    read_issue_events,
    resolve_issue,
    write_issue_events,
)
from lattice.storage.operations import read_task_authority
from lattice.storage.ownership import board_state

LOCAL_ONLY_MESSAGE = (
    "The issue log writes through its owning server; this board is a read-only {state} mirror."
)


def require_issue_log(ctx: OpContext) -> None:
    """Refuse direct writes on a cache; require the issue log to be enabled.

    A server-owned board is allowed through this feature gate. Its storage
    primitives still require the server's owner flag before the first write,
    which also protects a locally-opened ``LocalBoard`` pointed at that path.
    """
    state = board_state(ctx.lattice_dir)
    if state == "cache":
        raise OpError("LOCAL_ONLY", LOCAL_ONLY_MESSAGE.format(state=state), {"board": state})
    if not issues_enabled(ctx.config):
        message = (
            hosted_issues_disabled_message(
                has_issue_metadata(ctx.lattice_dir), ctx.lattice_dir.parent.name
            )
            if state == "hosted"
            else issues_disabled_message(has_issue_metadata(ctx.lattice_dir))
        )
        raise OpError("ISSUES_DISABLED", message)


@dataclass(frozen=True, kw_only=True)
class IssueParams(CommonParams):
    """``issue``: an issue ID as the caller gave it (``LAT-I3``, ``I3`` or ``iss_...``)."""

    issue: str


#: ``decide(snapshot)`` returns the event to append as ``(type, data)``, or
#: ``None`` when there is nothing to do; it raises ``OpError`` to refuse.
Decide = Callable[[dict], "tuple[str, dict] | None"]


def append(
    ctx: OpContext,
    issue_id: str,
    decide: Decide,
    p: CommonParams,
    *,
    reason: bool = True,
) -> tuple[dict, list[dict]]:
    """Under the issue's lock: replay, decide, append, snapshot.

    Returns the issue's snapshot and the events written (empty when *decide*
    had nothing to do).
    """
    with issue_write_context(ctx.lattice_dir, issue_id):
        snapshot = current_issue(ctx.lattice_dir, issue_id)
        if snapshot is None:
            raise OpError("NOT_FOUND", f"Issue {issue_id} not found.")
        decision = decide(snapshot)
        if decision is None:
            return snapshot, []
        event_type, data = decision
        event = create_issue_event(
            event_type, issue_id, ctx.actor, data, **p.provenance(reason=reason)
        )
        snapshot = apply_issue_event(snapshot, event)
        write_issue_events(ctx.lattice_dir, issue_id, [event], snapshot)
    return snapshot, [event]


def display(snapshot: dict) -> str:
    return snapshot.get("short_id") or snapshot["id"]


def refuse_closed(snapshot: dict, doing: str) -> None:
    """``CONFLICT`` when the issue is dismissed or a duplicate."""
    closure = snapshot.get("closure")
    if closure:
        name = display(snapshot)
        kind = "a duplicate" if closure["kind"] == "duplicate" else "dismissed"
        raise OpError(
            "CONFLICT",
            f"Issue {name} is {kind}; run 'lattice issue reopen {name}' before you {doing}.",
            {"issue": snapshot["id"], "closure": closure},
        )


def linkable_task(ctx: OpContext, raw_task: str) -> dict:
    """The task *raw_task* names, active or archived; ``NOT_FOUND`` / ``TASK_ERASED``."""
    task_id = ctx.resolve_task(raw_task)
    authority = read_task_authority(ctx.lattice_dir, task_id, allow_missing=True)
    if authority is None:
        raise OpError("NOT_FOUND", f"Task {raw_task} not found.")
    require_not_tombstoned(authority.snapshot)
    return authority.snapshot


def link_one(
    ctx: OpContext, issue_id: str, task_id: str, p: CommonParams
) -> tuple[dict, list[dict]]:
    """Link one issue to one task; idempotent when already linked."""

    def decide(snapshot: dict) -> tuple[str, dict] | None:
        refuse_closed(snapshot, "link it")
        if any(link["task_id"] == task_id for link in snapshot["links"]):
            return None
        return "issue_linked", {"task_id": task_id}

    return append(ctx, issue_id, decide, p)


def resolve(ctx: OpContext, raw: str) -> str:
    return resolve_issue(ctx.lattice_dir, raw)


def view(ctx: OpContext, snapshot: dict) -> dict:
    value = issue_views(ctx.lattice_dir, [snapshot])[0]
    if ctx.issue_media is not None:
        for media in value.get("media", []):
            if media.get("removed"):
                continue
            media.update(path=None, available="remote", missing=False)
            for frame in media.get("frames", []):
                frame.update(path=None, available="remote", missing=False)
    return value


def result(ctx: OpContext, snapshot: dict, events: list[dict]) -> OpResult:
    """The ``OpResult`` of an issue operation: the issue's view is its ``--json`` data."""
    return OpResult(events=events, value=view(ctx, snapshot), idempotent=not events)


# ---------------------------------------------------------------------------
# Media (LAT-366)
# ---------------------------------------------------------------------------
#
# A media item travels as ``{"payload": {filename, content_b64, sha256}}``,
# plus for a video ``"video": {width, height, duration_ms}`` and ``"frames":
# [{"t_ms", "payload"}]`` (JPEG stills the client made), and ``"converted_from":
# {content_type, size_bytes, sha256}`` (the source's) when the client transcoded
# or converted it. The type is sniffed here from the bytes; the filename is
# metadata only. Content is "the same" when either hash matches, so a source
# attached twice is one file even though each attach transcodes it anew.

_MEDIA_ITEM_KEYS = frozenset({"payload", "video", "frames", "converted_from"})
_VIDEO_KEYS = frozenset({"width", "height", "duration_ms"})
_CONVERTED_KEYS = frozenset({"content_type", "size_bytes", "sha256"})
_CONTENT_TYPE_RE = re.compile(r"^[a-z]+/[a-z0-9.+-]{1,64}$")
_SHA256_RE = re.compile(r"[0-9a-f]{64}\Z")
MAX_VIDEO_DIMENSION = 100_000
MAX_VIDEO_DURATION_MS = 86_400_000


def _invalid(message: str) -> OpError:
    return OpError("VALIDATION_ERROR", message, {"reason": "WRONG_TYPE", "param": "media"})


def _non_negative_int(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


def _b64_size(payload: object) -> int:
    """An upper bound of a payload's decoded size, from its text alone."""
    text = payload.get("content_b64") if isinstance(payload, dict) else None
    return (len(text) * 3) // 4 if isinstance(text, str) else 0


@dataclass(frozen=True)
class DecodedMedia:
    """One media item, decoded and checked."""

    content_type: str
    kind: str
    original_name: str
    content: bytes | None
    sha256: str
    size_bytes: int
    width: int | None = None
    height: int | None = None
    duration_ms: int | None = None
    frames: tuple[tuple[int, bytes], ...] = ()
    staged_frames: tuple[tuple[int, str, int], ...] = ()
    staged: bool = False
    converted_from: dict | None = None

    @property
    def hashes(self) -> set[str]:
        """The stored content's hash, and its source's when it was converted."""
        source = (self.converted_from or {}).get("sha256")
        return {self.sha256, source} if source else {self.sha256}


def check_media_items(items: tuple[dict, ...]) -> None:
    """The shape of each media item, without decoding anything (``VALIDATION_ERROR``)."""
    for i, item in enumerate(items, 1):
        extra = set(item) - _MEDIA_ITEM_KEYS
        if extra or "payload" not in item or not isinstance(item["payload"], dict):
            raise _invalid(
                f"media item {i} must be an object with a payload and optionally "
                f"video, frames, converted_from (got {sorted(item)})."
            )
        video = item.get("video")
        if video is not None and (
            not isinstance(video, dict)
            or set(video) - _VIDEO_KEYS
            or not all(_non_negative_int(v) for v in video.values())
            or any(video[k] > MAX_VIDEO_DIMENSION for k in ("width", "height") if k in video)
            or video.get("duration_ms", 0) > MAX_VIDEO_DURATION_MS
        ):
            raise _invalid(
                f"media item {i}: video dimensions must be at most {MAX_VIDEO_DIMENSION} and "
                f"duration_ms at most {MAX_VIDEO_DURATION_MS}."
            )
        converted = item.get("converted_from")
        if converted is not None and (
            not isinstance(converted, dict)
            or set(converted) != _CONVERTED_KEYS
            or not isinstance(converted["content_type"], str)
            or not _CONTENT_TYPE_RE.fullmatch(converted["content_type"])
            or not _non_negative_int(converted["size_bytes"])
            or not isinstance(converted["sha256"], str)
            or not _SHA256_RE.fullmatch(converted["sha256"])
        ):
            raise _invalid(
                f"media item {i}: converted_from must be {{content_type, size_bytes, sha256}}."
            )
        frames = item.get("frames")
        if frames is not None:
            if not isinstance(frames, list) or len(frames) > MAX_FRAMES:
                raise _invalid(f"media item {i}: frames must be a list of at most {MAX_FRAMES}.")
            for frame in frames:
                if (
                    not isinstance(frame, dict)
                    or set(frame) != {"t_ms", "payload"}
                    or not _non_negative_int(frame["t_ms"])
                    or frame["t_ms"] > MAX_VIDEO_DURATION_MS
                    or not isinstance(frame["payload"], dict)
                ):
                    raise _invalid(
                        f"media item {i}: each frame needs t_ms from 0 to "
                        f"{MAX_VIDEO_DURATION_MS} and a payload."
                    )
            times = [frame["t_ms"] for frame in frames]
            if len(set(times)) != len(times):
                raise _invalid(f"media item {i}: two frames have the same t_ms.")


def _photo_metadata_refusal(name: str) -> OpError:
    return OpError(
        "VALIDATION_ERROR",
        f"{name} could not be converted to a metadata-free photo. Convert HEIC to JPEG, "
        "repair malformed JPEG/PNG, or use the CLI fallback "
        "`lattice issue file --evidence <photo> --keep-photo-metadata` (or "
        "`lattice issue attach <issue> <photo> --keep-photo-metadata`) to store the original.",
        {"reason": "PHOTO_METADATA_UNSTRIPPED", "param": "media"},
    )


def _decode_one(
    item: dict,
    per_file: int,
    nothing: str,
    *,
    stage_manager=None,
    token_id: str | None = None,
    require_stage_owner: bool = False,
    keep_photo_metadata: bool = False,
) -> DecodedMedia:
    filename = item["payload"].get("filename")
    name = clean_original_name(filename) if isinstance(filename, str) else "?"
    payload = item["payload"]
    staged = "staged" in payload
    source = item.get("converted_from")
    if source is not None and source["size_bytes"] > per_file:
        raise _too_large_file(
            f"original source for converted payload {name}",
            source["size_bytes"],
            per_file,
            nothing,
        )
    if staged:
        if stage_manager is None:
            raise OpError(
                "MEDIA_STAGE_UNAVAILABLE",
                "staged media is only accepted by a hosted issue operation.",
            )
        if (
            set(payload) != {"filename", "sha256", "size", "staged"}
            or payload.get("staged") is not True
        ):
            raise _invalid("a staged media payload needs filename, sha256, size, staged=true.")
        sha256 = payload.get("sha256")
        if not isinstance(sha256, str) or not _SHA256_RE.fullmatch(sha256):
            raise _invalid("media sha256 must be 64 lowercase hexadecimal characters.")
        size = payload.get("size")
        if not _non_negative_int(size):
            raise _invalid("staged media size must be a non-negative integer.")
        if size > per_file:
            raise _too_large_file(name, size, per_file, nothing)
        _validate_media_filename(filename)
        metadata = stage_manager.verify_staged(
            sha256, size, token_id=token_id, require_owner=require_stage_owner
        )
        content_type = metadata["content_type"]
        content = None
    else:
        if stage_manager is not None:
            raise OpError(
                "HOSTED_MEDIA_INLINE_UNSUPPORTED",
                "hosted issue media must be uploaded as raw staged objects; inline base64 is refused.",
            )
        if _b64_size(item["payload"]) > per_file + 2:
            size = _b64_size(item["payload"])
            raise _too_large_file(name, size, per_file, nothing)
        filename, content = decode_payload(item["payload"])
        name = clean_original_name(filename)
        if len(content) > per_file:
            raise _too_large_file(name, len(content), per_file, nothing)
        content_type = sniff_media(content[:SNIFF_BYTES])
        sha256 = hashlib.sha256(content).hexdigest()
    if content_type is None:
        raise OpError(
            "VALIDATION_ERROR",
            f"{name} is not a photo or video by its content. Accepted: {ACCEPTED_FORMATS_TEXT}.",
            {"reason": "NOT_MEDIA", "param": "media"},
        )
    kind = media_kind(content_type) or ""
    if kind == "photo":
        if staged:
            metadata_status = metadata.get("photo_metadata_status")
            allowed_statuses = {"stripped"}
            if keep_photo_metadata:
                allowed_statuses.add("kept")
            if content_type == "image/heic":
                if not keep_photo_metadata or metadata_status != "kept":
                    raise _photo_metadata_refusal(name)
            elif (
                content_type in {"image/jpeg", "image/png"}
                and metadata_status not in allowed_statuses
            ):
                raise _photo_metadata_refusal(name)
        elif content_type == "image/heic":
            if not keep_photo_metadata:
                raise _photo_metadata_refusal(name)
        elif content_type in {"image/jpeg", "image/png"}:
            try:
                assert content is not None
                content = strip_photo_metadata(content, content_type)
                sha256 = hashlib.sha256(content).hexdigest()
            except PhotoMetadataError as exc:
                if not keep_photo_metadata:
                    raise _photo_metadata_refusal(name) from exc
    video = item.get("video") or {}
    frames: list[tuple[int, bytes]] = []
    staged_frames: list[tuple[int, str, int]] = []
    if kind != "video" and (item.get("video") is not None or item.get("frames")):
        raise _invalid(f"{name} is a photo; only a video carries video metadata or frames.")
    for frame in item.get("frames") or []:
        frame_payload = frame["payload"]
        if "staged" in frame_payload:
            if stage_manager is None:
                raise OpError(
                    "MEDIA_STAGE_UNAVAILABLE",
                    "staged media is only accepted by a hosted issue operation.",
                )
            if (
                set(frame_payload) != {"filename", "sha256", "size", "staged"}
                or frame_payload.get("staged") is not True
            ):
                raise _invalid("a staged frame payload needs filename, sha256, size, staged=true.")
            frame_hash = frame_payload.get("sha256")
            if not isinstance(frame_hash, str) or not _SHA256_RE.fullmatch(frame_hash):
                raise _invalid("frame sha256 must be 64 lowercase hexadecimal characters.")
            frame_size = frame_payload.get("size")
            if not _non_negative_int(frame_size) or frame_size > MAX_FRAME_BYTES:
                raise _invalid(f"{name}: a frame is over {format_size(MAX_FRAME_BYTES)}.")
            _frame_name = frame_payload.get("filename")
            _validate_media_filename(_frame_name)
            frame_meta = stage_manager.verify_staged(
                frame_hash, frame_size, token_id=token_id, require_owner=require_stage_owner
            )
            if frame_meta["content_type"] != "image/jpeg":
                raise _invalid(f"{name}: every frame must be a JPEG of at most 2 MB.")
            allowed_statuses = {"stripped"}
            if keep_photo_metadata:
                allowed_statuses.add("kept")
            if frame_meta.get("photo_metadata_status") not in allowed_statuses:
                raise _photo_metadata_refusal(_frame_name)
            staged_frames.append((frame["t_ms"], frame_hash, frame_size))
        else:
            if stage_manager is not None:
                raise OpError(
                    "HOSTED_MEDIA_INLINE_UNSUPPORTED",
                    "hosted issue media must be uploaded as raw staged objects; inline base64 is refused.",
                )
            if _b64_size(frame_payload) > MAX_FRAME_BYTES + 2:
                raise _invalid(f"{name}: a frame is over {format_size(MAX_FRAME_BYTES)}.")
            if set(frame_payload) == {"filename", "content_b64"}:
                try:
                    inline_bytes = base64.b64decode(frame_payload["content_b64"], validate=True)
                except (binascii.Error, TypeError, ValueError):
                    inline_bytes = b""
                frame_payload = {
                    **frame_payload,
                    "sha256": hashlib.sha256(inline_bytes).hexdigest(),
                }
            _frame_name, data = decode_payload(frame_payload)
            if sniff_media(data[:SNIFF_BYTES]) != "image/jpeg" or len(data) > MAX_FRAME_BYTES:
                raise _invalid(f"{name}: every frame must be a JPEG of at most 2 MB.")
            try:
                data = strip_photo_metadata(data, "image/jpeg")
            except PhotoMetadataError as exc:
                if not keep_photo_metadata:
                    raise _photo_metadata_refusal(_frame_name) from exc
            frames.append((frame["t_ms"], data))
    if kind == "photo":
        dims = image_dimensions(content_type, content) if content is not None else (None, None)
        width, height = dims if dims else (None, None)
    else:
        width, height = video.get("width"), video.get("height")
    return DecodedMedia(
        content_type=content_type,
        kind=kind,
        original_name=name,
        content=content,
        sha256=sha256,
        size_bytes=len(content) if content is not None else size,
        width=width,
        height=height,
        duration_ms=video.get("duration_ms"),
        frames=tuple(sorted(frames)),
        staged_frames=tuple(sorted(staged_frames)),
        staged=staged,
        converted_from=dict(item["converted_from"]) if item.get("converted_from") else None,
    )


def _validate_media_filename(filename: object) -> None:
    """Apply the same filename restrictions to staged payloads as inline payloads."""
    from pathlib import PurePosixPath

    if not isinstance(filename, str):
        raise _invalid("media payload filename must be a string.")
    name = PurePosixPath(filename).name
    if (
        not name
        or re.search(r"[\x00-\x1f\x7f-\x9f]", filename)
        or "\\" in PurePosixPath(filename).suffix
    ):
        raise OpError(
            "VALIDATION_ERROR",
            f"Invalid payload filename {filename!r}.",
            {"reason": "UNSAFE_NAME", "param": "payload"},
        )


def _too_large_file(name: str, size: int, limit: int, nothing: str) -> OpError:
    if size.bit_length() > 1024:
        message = f"{name} exceeds the per-file limit of {format_size(limit)}. {nothing}"
    else:
        message = file_too_large_message(name, size, limit, nothing=nothing)
    return OpError(
        "PAYLOAD_TOO_LARGE",
        message,
        {"reason": MEDIA_FILE_TOO_LARGE, "size_bytes": size, "limit_bytes": limit},
    )


def decode_media(
    items: tuple[dict, ...],
    config: dict,
    *,
    nothing: str,
    stage_manager=None,
    token_id: str | None = None,
    require_stage_owner: bool = False,
    keep_photo_metadata: bool = False,
) -> list[DecodedMedia]:
    """Decode and check every item, each within ``issues.max_media_mb``; the same
    content twice is kept once. *nothing*: the refusal's last sentence."""
    if require_stage_owner and keep_photo_metadata:
        raise OpError("TOKEN_RESTRICTED", "filing-only tokens cannot keep photo metadata.")
    per_file, _per_issue = media_limits(config)
    decoded: list[DecodedMedia] = []
    seen: set[str] = set()
    for item in items:
        one = _decode_one(
            item,
            per_file,
            nothing,
            stage_manager=stage_manager,
            token_id=token_id,
            require_stage_owner=require_stage_owner,
            keep_photo_metadata=keep_photo_metadata,
        )
        if not seen & one.hashes:
            seen |= one.hashes
            decoded.append(one)
    return decoded


def held_hashes(entries: list[dict]) -> set[str]:
    """Every content hash of *entries*: each stored file's and its source's."""
    hashes = set()
    for entry in entries:
        hashes.add(entry.get("sha256"))
        hashes.add((entry.get("converted_from") or {}).get("sha256"))
    hashes.discard(None)
    return hashes


def check_issue_total(config: dict, issue: str, existing: int, new: list[DecodedMedia]) -> None:
    """``PAYLOAD_TOO_LARGE`` when the issue's present media would pass ``issues.max_issue_media_mb``."""
    _per_file, per_issue = media_limits(config)
    total = existing + sum(d.size_bytes for d in new)
    if new and total > per_issue:
        raise OpError(
            "PAYLOAD_TOO_LARGE",
            issue_too_large_message(issue, total, per_issue),
            {"reason": ISSUE_MEDIA_TOO_LARGE, "size_bytes": total, "limit_bytes": per_issue},
        )


def media_added_data(decoded: DecodedMedia, media_id: str, n: int) -> dict:
    """The ``issue_media_added`` event data; unknown dimensions are left out."""
    data: dict = {
        "media_id": media_id,
        "n": n,
        "kind": decoded.kind,
        "content_type": decoded.content_type,
        "original_name": decoded.original_name,
        "size_bytes": decoded.size_bytes,
        "sha256": decoded.sha256,
    }
    for key in ("width", "height", "duration_ms", "converted_from"):
        value = getattr(decoded, key)
        if value is not None:
            data[key] = value
    return data


def stage_media(
    ctx: OpContext,
    issue_id: str,
    decoded: list[DecodedMedia],
    first_n: int,
    p: CommonParams,
) -> list[dict]:
    """Write each item's file and frames; return their ``issue_media_added`` events.

    The caller holds the issue's lock and appends the events after this returns.
    """
    events = []
    entries = []
    staged_objects: list[dict] = []
    if ctx.issue_media is not None:
        additions = [item.size_bytes for item in decoded]
        additions.extend(size for item in decoded for _time, _sha, size in item.staged_frames)
        ctx.issue_media.check_issue_quota(issue_id, additions)
    try:
        for offset, item in enumerate(decoded):
            media_id = generate_media_id()
            entry = {"id": media_id, "content_type": item.content_type}
            entries.append(entry)
            data = media_added_data(item, media_id, first_n + offset)
            if ctx.issue_media is None:
                if item.content is None:
                    raise OpError(
                        "MEDIA_STAGE_UNAVAILABLE",
                        "staged media is only accepted by a hosted issue operation.",
                    )
                store_media(ctx.lattice_dir, issue_id, entry, item.content, list(item.frames))
            else:
                if not item.staged:
                    raise OpError(
                        "HOSTED_MEDIA_INLINE_UNSUPPORTED",
                        "hosted issue media must be uploaded as raw staged objects; inline base64 is refused.",
                    )
                from lattice.core.issue_media import frame_name, media_ext
                from lattice.storage.issue_media import frames_dir, media_path

                original_path = media_path(ctx.lattice_dir, issue_id, entry)
                if original_path is None or media_ext(item.content_type) is None:
                    raise OpError("VALIDATION_ERROR", "invalid issue-media destination")
                staged_objects.append(
                    {
                        "media_id": media_id,
                        "t_ms": None,
                        "sha256": item.sha256,
                        "size_bytes": item.size_bytes,
                        "target": original_path.relative_to(ctx.lattice_dir).as_posix(),
                    }
                )
                sidecar_dir = frames_dir(ctx.lattice_dir, issue_id, entry)
                for t_ms, sha256, size_bytes in item.staged_frames:
                    if sidecar_dir is None:
                        raise OpError("VALIDATION_ERROR", "invalid issue-media frame destination")
                    staged_objects.append(
                        {
                            "media_id": media_id,
                            "t_ms": t_ms,
                            "sha256": sha256,
                            "size_bytes": size_bytes,
                            "target": (sidecar_dir / frame_name(t_ms))
                            .relative_to(ctx.lattice_dir)
                            .as_posix(),
                        }
                    )
            events.append(
                create_issue_event(
                    "issue_media_added", issue_id, ctx.actor, data, **p.provenance()
                )
            )
        if staged_objects:
            op_id = ctx.caller.origin.get("op_id")
            if not isinstance(op_id, str):
                raise OpError(
                    "VALIDATION_ERROR", "hosted issue media operation has no operation ID"
                )
            ctx.issue_media.add_manifest(op_id, issue_id, staged_objects)
    except BaseException as failure:
        if ctx.issue_media is None:
            _cleanup_media_entries(ctx.lattice_dir, issue_id, entries, failure)
        raise
    return events


def _cleanup_media_entries(lattice_dir, issue_id: str, entries: list[dict], failure) -> None:  # noqa: ANN001
    for entry in reversed(entries):
        try:
            delete_media_files(lattice_dir, issue_id, entry)
        except Exception as cleanup_error:  # noqa: BLE001
            failure.add_note(f"Could not clean staged issue media {entry['id']}: {cleanup_error}")


def issue_filing_event_committed(lattice_dir, issue_id: str) -> bool:  # noqa: ANN001
    """Whether the authoritative log contains this new issue's filing event.

    If the log cannot be inspected after a write error, keep the reservation:
    deleting its media or sequence could break an event that reached disk.
    """
    try:
        return any(
            event.get("type") == "issue_filed"
            for event in read_issue_events(lattice_dir, issue_id)
        )
    except Exception:  # noqa: BLE001
        return True


def cleanup_uncommitted_media(lattice_dir, issue_id: str, events: list[dict], failure) -> None:  # noqa: ANN001
    """Remove staged media whose add event did not reach the authoritative log."""
    try:
        committed = {
            event.get("data", {}).get("media_id")
            for event in read_issue_events(lattice_dir, issue_id)
            if event.get("type") == "issue_media_added"
        }
    except Exception:  # noqa: BLE001
        committed = {event.get("data", {}).get("media_id") for event in events}
    entries = [
        {"id": media_id, "content_type": event.get("data", {}).get("content_type")}
        for event in events
        if event.get("type") == "issue_media_added"
        and (media_id := event.get("data", {}).get("media_id")) not in committed
    ]
    _cleanup_media_entries(lattice_dir, issue_id, entries, failure)
