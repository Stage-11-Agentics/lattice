"""Shared preparation and staging for hosted dashboard and reporter uploads."""

from __future__ import annotations

import hashlib

from lattice.core.errors import OpError
from lattice.core.issue_media import clean_original_name, media_kind, sniff_media
from lattice.dashboard.media_prep import prepare_issue_media
from lattice.ops.issue_common import check_media_items
from lattice.ops.task_attach import decode_payload, encode_payload


def prepare_media_file(
    filename: str,
    content: bytes,
    *,
    refuse_video_without_ffmpeg: bool,
    refuse_unstrippable_photos: bool = False,
) -> dict:
    """Run shared photo/video privacy preparation and validate the result shape.

    Public reporter links can opt into refusing photo formats for which shared
    preparation has no metadata-stripping result. Dashboard callers retain
    their existing behavior.
    """
    name = clean_original_name(filename) or "attachment"
    items = prepare_issue_media(
        [{"payload": encode_payload(name, content)}],
        refuse_video_without_ffmpeg=refuse_video_without_ffmpeg,
    )
    if len(items) != 1:
        raise OpError("WRITE_ERROR", "media preparation returned an invalid item count.")
    item = items[0]
    check_media_items((item,))
    _name, prepared = decode_payload(item["payload"])
    content_type = sniff_media(prepared[:64])
    kind = media_kind(content_type) if content_type is not None else None
    if content_type is None or kind not in {"photo", "video"}:
        raise OpError("VALIDATION_ERROR", "Choose a photo or video file.")
    if (
        refuse_unstrippable_photos
        and kind == "photo"
        and content_type not in {"image/jpeg", "image/png"}
    ):
        raise OpError(
            "VALIDATION_ERROR",
            "This photo format could not be made private.",
            {"reason": "PHOTO_METADATA_UNSTRIPPED"},
        )
    return item


def stage_prepared_media(
    project,
    filename: str,
    content: bytes,
    *,
    token_id: str | None = None,
    max_staged_bytes: int | None = None,
    dashboard: bool = False,
    refuse_video_without_ffmpeg: bool = True,
) -> dict:
    """Prepare one file and stage its stored original plus prepared frames.

    For reporter links, the caller runs this inside its per-link worker lock.
    Dashboard callers preserve their existing session-only, unowned staging.
    """
    prepared = prepare_media_file(
        filename, content, refuse_video_without_ffmpeg=refuse_video_without_ffmpeg
    )
    return stage_prepared_item(
        project,
        prepared,
        token_id=token_id,
        max_staged_bytes=max_staged_bytes,
        dashboard=dashboard,
    )


def stage_prepared_item(
    project,
    prepared: dict,
    *,
    token_id: str | None,
    max_staged_bytes: int | None,
    dashboard: bool = False,
) -> dict:
    """Commit one already-prepared media item and its frame sidecars."""
    payloads = [prepared["payload"]]
    payloads.extend(frame["payload"] for frame in prepared.get("frames", []))
    digests = {payload["sha256"] for payload in payloads}
    previous_owned: set[str] = set()
    if token_id is not None:
        for digest in digests:
            metadata = project.issue_media._read_stage_metadata(digest)
            if metadata is not None and token_id in project.issue_media._stage_owners(metadata):
                previous_owned.add(digest)

    staged_hashes: set[str] = set()

    def stage(payload: dict) -> dict:
        item_name, data = decode_payload(payload)
        digest = hashlib.sha256(data).hexdigest()
        upload = project.issue_media.begin_upload(
            digest,
            len(data),
            token_id=token_id,
            max_staged_bytes=max_staged_bytes,
            keep_photo_metadata=False,
            dashboard=dashboard,
        )
        try:
            upload.write(data)
            result = upload.finish()
        except BaseException:
            upload.abort()
            raise
        staged_hashes.add(result["sha256"])
        return {
            "filename": item_name,
            "sha256": result["sha256"],
            "size": result["size_bytes"],
            "staged": True,
        }

    try:
        result = {
            key: value for key, value in prepared.items() if key not in {"payload", "frames"}
        }
        result["payload"] = stage(prepared["payload"])
        frames = [
            {"t_ms": frame["t_ms"], "payload": stage(frame["payload"])}
            for frame in prepared.get("frames", [])
        ]
        if frames:
            result["frames"] = frames
        return result
    except BaseException:
        if token_id is not None:
            project.issue_media.cleanup_token_stages(token_id, staged_hashes - previous_owned)
        raise
