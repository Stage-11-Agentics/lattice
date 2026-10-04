"""Prepare dashboard-filed media exactly as ``lattice issue file --evidence`` does.

Issue media is committed with the board, so a video from a phone must not keep
its location tags: a dashboard filing runs the same ffmpeg step as the CLI
(``integrations.ffmpeg.prepare_video``: transcode or metadata-free remux, then
ffmpeg frames) and a HEIC photo is converted to JPEG. The step runs before the
board lock is taken because a transcode can take minutes.

Without ffmpeg the local dashboard keeps the browser's own dimensions and
frames. Hosted staging refuses a video instead: the browser cannot prove that
location metadata was removed before the server stores it.
"""

from __future__ import annotations

import tempfile
from pathlib import Path

from lattice.core.errors import OpError
from lattice.core.issue_media import (
    PhotoMetadataError,
    SNIFF_BYTES,
    clean_original_name,
    frame_name,
    media_kind,
    sniff_heic,
    sniff_media,
    strip_photo_metadata,
)
from lattice.ops.task_attach import decode_payload, encode_payload


def prepare_issue_media(
    items: list[dict], *, refuse_video_without_ffmpeg: bool = False
) -> list[dict]:
    """Each media item as the CLI would send it; items that fail validation pass through
    unchanged so ``issue.file`` refuses them with its own message. Hosted staging sets
    ``refuse_video_without_ffmpeg`` because it cannot trust client-side metadata stripping."""
    return [
        _prepare_item(item, refuse_video_without_ffmpeg=refuse_video_without_ffmpeg)
        for item in items
    ]


def _prepare_item(item: dict, *, refuse_video_without_ffmpeg: bool = False) -> dict:
    payload = item.get("payload")
    if not isinstance(payload, dict):
        return item
    try:
        filename, content = decode_payload(payload)
    except OpError:
        return item
    head = content[:SNIFF_BYTES]
    content_type = sniff_media(head)
    name = clean_original_name(filename) or "file"
    if content_type is not None and media_kind(content_type) == "video":
        return _prepare_video(
            item,
            name,
            content,
            content_type,
            payload["sha256"].lower(),
            refuse_video_without_ffmpeg=refuse_video_without_ffmpeg,
        )
    if content_type == "image/heic" or (content_type is None and sniff_heic(head)):
        return _convert_heic(item, name, content, payload["sha256"].lower())
    if content_type in {"image/jpeg", "image/png"}:
        try:
            clean = strip_photo_metadata(content, content_type)
        except PhotoMetadataError:
            return item
        if clean != content:
            return {**_unknown_keys(item), "payload": encode_payload(name, clean)}
    return item


def _unknown_keys(item: dict) -> dict:
    """Keys the preparation does not own, kept so ``issue.file`` refuses them whether or
    not ffmpeg is installed."""
    return {
        k: v for k, v in item.items() if k not in ("payload", "video", "frames", "converted_from")
    }


def _suffix(name: str) -> str:
    suffix = Path(name).suffix
    return suffix if suffix and suffix.isascii() and len(suffix) <= 8 else ".bin"


def _prepare_video(
    item: dict,
    name: str,
    content: bytes,
    content_type: str,
    sha256: str,
    *,
    refuse_video_without_ffmpeg: bool = False,
) -> dict:
    from lattice.integrations.ffmpeg import prepare_video

    with tempfile.TemporaryDirectory(prefix="lattice-dashboard-media-") as tmp:
        src = Path(tmp) / f"video{_suffix(name)}"
        src.write_bytes(content)
        prepared = prepare_video(src, content, content_type, sha256)
    ffmpeg_missing = ("no_frames", "ffmpeg_not_found") in prepared.notes
    if refuse_video_without_ffmpeg and prepared.converted_from is None:
        message = (
            "This server cannot remove location data from videos yet; ask the board admin to install ffmpeg"
            if ffmpeg_missing
            else "This server could not remove location data from this video; ask the board admin to check its ffmpeg"
        )
        raise OpError("MEDIA_STAGE_UNAVAILABLE", message)
    if ffmpeg_missing:
        return item
    out: dict = {**_unknown_keys(item), "payload": encode_payload(name, prepared.content)}
    if prepared.video:
        out["video"] = prepared.video
    if prepared.frames:
        out["frames"] = [
            {"t_ms": t_ms, "payload": encode_payload(frame_name(t_ms), data)}
            for t_ms, data in prepared.frames
        ]
    if prepared.converted_from:
        out["converted_from"] = prepared.converted_from
    return out


def _convert_heic(item: dict, name: str, content: bytes, sha256: str) -> dict:
    from lattice.integrations.ffmpeg import convert_heic

    with tempfile.TemporaryDirectory(prefix="lattice-dashboard-media-") as tmp:
        src = Path(tmp) / f"photo{_suffix(name)}"
        src.write_bytes(content)
        converted = convert_heic(src)
    if converted is None:
        return item
    try:
        converted = strip_photo_metadata(converted, "image/jpeg")
    except PhotoMetadataError:
        return item
    return {
        **_unknown_keys(item),
        "payload": encode_payload(name, converted),
        "converted_from": {
            "content_type": "image/heic",
            "size_bytes": len(content),
            "sha256": sha256,
        },
    }
