"""What the ``issue.*`` operations share (LAT-361). Not an operation module.

Every issue operation first checks :func:`require_issue_log`: the issue log
works only on a local board (``LOCAL_ONLY``), and only when the board turned
it on (``ISSUES_DISABLED``). Writes go through :func:`append`, which replays
the issue's log under its lock, lets the caller decide, and writes the event
and the new snapshot.
"""

from __future__ import annotations

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
    SNIFF_BYTES,
    clean_original_name,
    file_too_large_message,
    format_size,
    image_dimensions,
    issue_too_large_message,
    media_kind,
    media_limits,
    sniff_media,
)
from lattice.core.issues import apply_issue_event, issues_disabled_message
from lattice.core.visibility import require_not_tombstoned
from lattice.ops.base import CommonParams, OpContext, OpError, OpResult
from lattice.ops.task_attach import decode_payload
from lattice.storage.issue_media import store_media
from lattice.storage.issues import (
    current_issue,
    issue_views,
    issue_write_context,
    issues_dir,
    resolve_issue,
    write_issue_events,
)
from lattice.storage.operations import read_task_authority
from lattice.storage.ownership import board_state

LOCAL_ONLY_MESSAGE = "The issue log works only on local boards for now; this board is {state}."


def require_issue_log(ctx: OpContext) -> None:
    """``LOCAL_ONLY`` on a hosted board or a cache; ``ISSUES_DISABLED`` when it is off."""
    state = board_state(ctx.lattice_dir)
    if state != "local":
        raise OpError("LOCAL_ONLY", LOCAL_ONLY_MESSAGE.format(state=state), {"board": state})
    if not issues_enabled(ctx.config):
        raise OpError(
            "ISSUES_DISABLED", issues_disabled_message(issues_dir(ctx.lattice_dir).is_dir())
        )


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
    return issue_views(ctx.lattice_dir, [snapshot])[0]


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
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


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
    content: bytes
    sha256: str
    width: int | None = None
    height: int | None = None
    duration_ms: int | None = None
    frames: tuple[tuple[int, bytes], ...] = ()
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
        ):
            raise _invalid(
                f"media item {i}: video must hold non-negative integers "
                "width, height, duration_ms."
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
                    or not isinstance(frame["payload"], dict)
                ):
                    raise _invalid(f"media item {i}: each frame must be {{t_ms, payload}}.")
            times = [frame["t_ms"] for frame in frames]
            if len(set(times)) != len(times):
                raise _invalid(f"media item {i}: two frames have the same t_ms.")


def _decode_one(item: dict, per_file: int, nothing: str) -> DecodedMedia:
    filename = item["payload"].get("filename")
    name = clean_original_name(filename) if isinstance(filename, str) else "?"
    if _b64_size(item["payload"]) > per_file + 2:
        size = _b64_size(item["payload"])
        raise _too_large_file(name, size, per_file, nothing)
    filename, content = decode_payload(item["payload"])
    name = clean_original_name(filename)
    if len(content) > per_file:
        raise _too_large_file(name, len(content), per_file, nothing)
    content_type = sniff_media(content[:SNIFF_BYTES])
    if content_type is None:
        raise OpError(
            "VALIDATION_ERROR",
            f"{name} is not a photo or video by its content. Accepted: {ACCEPTED_FORMATS_TEXT}.",
            {"reason": "NOT_MEDIA", "param": "media"},
        )
    kind = media_kind(content_type) or ""
    video = item.get("video") or {}
    frames: list[tuple[int, bytes]] = []
    if kind != "video" and (item.get("video") is not None or item.get("frames")):
        raise _invalid(f"{name} is a photo; only a video carries video metadata or frames.")
    for frame in item.get("frames") or []:
        if _b64_size(frame["payload"]) > MAX_FRAME_BYTES + 2:
            raise _invalid(f"{name}: a frame is over {format_size(MAX_FRAME_BYTES)}.")
        _frame_name, data = decode_payload(frame["payload"])
        if sniff_media(data[:SNIFF_BYTES]) != "image/jpeg" or len(data) > MAX_FRAME_BYTES:
            raise _invalid(f"{name}: every frame must be a JPEG of at most 2 MB.")
        frames.append((frame["t_ms"], data))
    if kind == "photo":
        dims = image_dimensions(content_type, content)
        width, height = dims if dims else (None, None)
    else:
        width, height = video.get("width"), video.get("height")
    return DecodedMedia(
        content_type=content_type,
        kind=kind,
        original_name=name,
        content=content,
        sha256=hashlib.sha256(content).hexdigest(),
        width=width,
        height=height,
        duration_ms=video.get("duration_ms"),
        frames=tuple(sorted(frames)),
        converted_from=dict(item["converted_from"]) if item.get("converted_from") else None,
    )


def _too_large_file(name: str, size: int, limit: int, nothing: str) -> OpError:
    return OpError(
        "PAYLOAD_TOO_LARGE",
        file_too_large_message(name, size, limit, nothing=nothing),
        {"reason": MEDIA_FILE_TOO_LARGE, "size_bytes": size, "limit_bytes": limit},
    )


def decode_media(items: tuple[dict, ...], config: dict, *, nothing: str) -> list[DecodedMedia]:
    """Decode and check every item, each within ``issues.max_media_mb``; the same
    content twice is kept once. *nothing*: the refusal's last sentence."""
    per_file, _per_issue = media_limits(config)
    decoded: list[DecodedMedia] = []
    seen: set[str] = set()
    for item in items:
        one = _decode_one(item, per_file, nothing)
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
    total = existing + sum(len(d.content) for d in new)
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
        "size_bytes": len(decoded.content),
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
    for offset, item in enumerate(decoded):
        media_id = generate_media_id()
        data = media_added_data(item, media_id, first_n + offset)
        store_media(
            ctx.lattice_dir,
            issue_id,
            {"id": media_id, "content_type": item.content_type},
            item.content,
            list(item.frames),
        )
        events.append(
            create_issue_event("issue_media_added", issue_id, ctx.actor, data, **p.provenance())
        )
    return events
