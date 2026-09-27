"""``task.attach``: the ``lattice attach`` command's rules.

A file source travels as ``payload: {filename, content_b64, sha256}`` (SPEC
§3.8). ``filename`` is metadata only: it supplies the default title, the
content-type guess, and the stored name's suffix; it is never a path. The
payload is stored at ``artifacts/payload/<artifact_id><suffix>`` with
``atomic_write``. A URL travels as ``source``. A ``source`` that is not a URL
names a file the client could not read, and is reported as not found.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import json
import mimetypes
import re
from dataclasses import dataclass
from pathlib import PurePosixPath

from lattice.core.acceptance_criteria import normalize_criterion_ids
from lattice.core.artifacts import ARTIFACT_TYPES, create_artifact_metadata, serialize_artifact
from lattice.core.config import get_configured_roles
from lattice.core.events import create_event
from lattice.core.ids import generate_artifact_id, validate_id
from lattice.ops.base import CommonParams, OpContext, OpError, OpResult, operation
from lattice.storage.fs import atomic_write, ensure_artifact_dirs
from lattice.storage.operations import (
    AuthoritativeLogError,
    TaskMutationDecision,
    TaskPlacementError,
)

_PAYLOAD_KEYS = {"filename", "content_b64", "sha256"}
_SHA256_RE = re.compile(r"[0-9a-f]{64}")
_CONTROL_RE = re.compile(r"[\x00-\x1f\x7f-\x9f]")
#: The name an inline attachment's payload carries (its suffix and content type).
INLINE_FILENAME = "inline.md"


def encode_payload(filename: str, content: bytes) -> dict:
    """The ``payload`` param for *content* under the metadata name *filename*."""
    return {
        "filename": filename,
        "content_b64": base64.b64encode(content).decode("ascii"),
        "sha256": hashlib.sha256(content).hexdigest(),
    }


def _decode_payload(payload: dict) -> tuple[str, bytes]:
    """``(filename, content)`` from a ``payload`` param, or ``VALIDATION_ERROR``."""
    if set(payload) != _PAYLOAD_KEYS or not all(isinstance(v, str) for v in payload.values()):
        raise OpError(
            "VALIDATION_ERROR",
            "payload must be an object of strings with exactly: filename, content_b64, sha256.",
            {"reason": "WRONG_TYPE", "param": "payload"},
        )
    filename = payload["filename"]
    name = PurePosixPath(filename).name
    if not name or _CONTROL_RE.search(filename) or "\\" in PurePosixPath(filename).suffix:
        raise OpError(
            "VALIDATION_ERROR",
            f"Invalid payload filename {filename!r}.",
            {"reason": "UNSAFE_NAME", "param": "payload"},
        )
    try:
        content = base64.b64decode(payload["content_b64"], validate=True)
    except (binascii.Error, ValueError) as exc:
        raise OpError("VALIDATION_ERROR", "payload content_b64 is not valid base64.") from exc
    expected = payload["sha256"].lower()
    if not _SHA256_RE.fullmatch(expected) or hashlib.sha256(content).hexdigest() != expected:
        raise OpError("VALIDATION_ERROR", "payload sha256 does not match its content.")
    return filename, content


def _is_url(source: str) -> bool:
    return source.startswith("http://") or source.startswith("https://")


@dataclass(frozen=True, kw_only=True)
class AttachParams(CommonParams):
    task: str
    source: str | None = None  # a URL; a file source travels as ``payload``
    payload: dict | None = None
    type: str | None = None
    title: str | None = None
    summary: str | None = None
    sensitive: bool = False
    role: str | None = None
    criterion: tuple[str, ...] = ()
    inline: str | None = None
    id: str | None = None

    def check(self) -> None:
        if self.source is not None and self.payload is not None:
            raise OpError("VALIDATION_ERROR", "Provide either SOURCE or a payload, not both.")
        given = self.source is not None or self.payload is not None
        if given and self.inline is not None:
            raise OpError("VALIDATION_ERROR", "Provide either SOURCE or --inline, not both.")
        if not given and self.inline is None:
            raise OpError(
                "VALIDATION_ERROR", "Provide either a SOURCE (file/URL) or --inline text."
            )
        if self.inline is not None and self.type is not None and self.type not in {"note", "file"}:
            raise OpError(
                "VALIDATION_ERROR",
                f"When using --inline, --type must be 'note' or 'file' (got '{self.type}').",
            )
        if self.payload is not None:
            _decode_payload(self.payload)


@operation("task.attach")
class Attach:
    Params = AttachParams

    def run(self, ctx: OpContext, p: AttachParams) -> OpResult:
        lattice_dir = ctx.lattice_dir
        if p.role is not None:
            configured_roles = get_configured_roles(ctx.config)
            if configured_roles and p.role not in configured_roles:
                raise OpError(
                    "INVALID_ROLE",
                    f"Unknown role: '{p.role}'. "
                    f"Valid roles: {', '.join(sorted(configured_roles))}.",
                )
        task_id = ctx.resolve_task(p.task)

        # Reject missing/archived tasks and malformed criterion links from
        # strict replay before writing any shared metadata or payload file.
        # The same validation runs again in the attaching mutation, closing the
        # retry race without trusting the snapshot cache.
        def validate_target(context):  # noqa: ANN001, ANN202
            try:
                normalize_criterion_ids(p.criterion, snapshot=context.snapshot)
            except ValueError as exc:
                raise OpError("VALIDATION_ERROR", str(exc)) from exc
            return TaskMutationDecision(idempotent=True)

        try:
            ctx.mutate(task_id, validate_target)
        except AuthoritativeLogError as exc:
            # Absent or archived is today's NOT_FOUND; a log that fails strict
            # replay propagates to execute (INTEGRITY_ERROR, SPEC §3.1).
            if isinstance(exc, TaskPlacementError) or "no authoritative event log exists" in str(
                exc
            ):
                raise OpError("NOT_FOUND", f"Task {task_id} not found.") from exc
            raise

        art_type, title = p.type, p.title
        filename: str | None = None
        content: bytes | None = None
        if p.inline is not None:
            filename, content = INLINE_FILENAME, p.inline.encode("utf-8")
            if title is None:
                title = f"Inline: {p.role}" if p.role else "inline attachment"
            if art_type is None:
                art_type = "note"
        elif p.payload is not None:
            filename, content = _decode_payload(p.payload)
        is_url = p.source is not None and _is_url(p.source)
        if art_type is None:
            art_type = "reference" if is_url else "file"
        if art_type not in ARTIFACT_TYPES:
            raise OpError(
                "VALIDATION_ERROR",
                f"Invalid artifact type: '{art_type}'. "
                f"Valid types: {', '.join(sorted(ARTIFACT_TYPES))}.",
            )
        if p.id is not None:
            if not validate_id(p.id, "art"):
                raise OpError("INVALID_ID", f"Invalid artifact ID format: '{p.id}'.")
            art_id = p.id
        else:
            art_id = generate_artifact_id()
        if p.source is not None and not is_url:
            raise OpError("NOT_FOUND", f"Source file not found: '{p.source}'.")
        if title is None:
            title = p.source if is_url else PurePosixPath(filename or "").name

        payload_file: str | None = None
        if content is not None:
            payload_file = f"{art_id}{PurePosixPath(filename or '').suffix}"

        # meta/ and payload/ are scaffolded at init but empty dirs aren't
        # git-tracked, so cloned installs may lack them (LAT-239).
        ensure_artifact_dirs(lattice_dir)

        # With a caller-supplied ID whose metadata exists, the same data is an
        # idempotent attach and different data a conflict; checked before any
        # payload is written, so a conflict orphans nothing.
        meta_path = lattice_dir / "artifacts" / "meta" / f"{art_id}.json"
        existing_metadata: dict | None = None
        if meta_path.exists():
            existing = json.loads(meta_path.read_text())
            if existing.get("type") != art_type or existing.get("title") != title:
                conflict = True
            elif is_url:
                conflict = (existing.get("custom_fields") or {}).get("url") != p.source
            else:
                conflict = (existing.get("payload") or {}).get("file") != payload_file
            if conflict:
                raise OpError(
                    "CONFLICT", f"Conflict: artifact {art_id} exists with different data."
                )
            existing_metadata = existing

        content_type: str | None = None
        size_bytes: int | None = None
        custom_fields: dict | None = None
        if existing_metadata is not None:
            pass
        elif is_url:
            custom_fields = {"url": p.source}
        else:
            assert content is not None and payload_file is not None
            content_type, _ = mimetypes.guess_type(PurePosixPath(filename or "").name)
            size_bytes = len(content)
            atomic_write(lattice_dir / "artifacts" / "payload" / payload_file, content)

        event_data: dict = {"artifact_id": art_id}
        if p.role is not None:
            event_data["role"] = p.role
        # The metadata carries the timestamp of an event built now, as always.
        stamp = create_event("artifact_attached", task_id, ctx.actor, event_data)["ts"]
        metadata = existing_metadata or create_artifact_metadata(
            art_id,
            art_type,
            title,
            created_by=ctx.actor,
            created_at=stamp,
            summary=p.summary,
            model=p.model,
            tags=None,
            payload_file=payload_file,
            content_type=content_type,
            size_bytes=size_bytes,
            sensitive=p.sensitive,
            custom_fields=custom_fields,
        )
        if existing_metadata is None:
            atomic_write(meta_path, serialize_artifact(metadata))

        def decide(context):  # noqa: ANN001, ANN202
            try:
                normalized_ids = normalize_criterion_ids(p.criterion, snapshot=context.snapshot)
            except ValueError as exc:
                raise OpError("CONFLICT", str(exc)) from exc
            requested_key = (p.role, normalized_ids)
            for existing_event in context.events:
                if existing_event.get("type") != "artifact_attached":
                    continue
                data = existing_event.get("data", {})
                if data.get("artifact_id") != art_id:
                    continue
                if (data.get("role"), data.get("criterion_ids", [])) != requested_key:
                    raise OpError(
                        "CONFLICT",
                        f"Artifact {art_id} is already attached to {task_id} "
                        "with different role or criterion links.",
                    )
                return TaskMutationDecision(idempotent=True)
            attachment_data: dict = {"artifact_id": art_id}
            if p.role is not None:
                attachment_data["role"] = p.role
            if normalized_ids:
                attachment_data["criterion_ids"] = normalized_ids
            return TaskMutationDecision(
                events=[ctx.event("artifact_attached", task_id, attachment_data, p)]
            )

        result = ctx.mutate(task_id, decide)
        return OpResult(
            task=result.snapshot,
            events=result.appended_events,
            value=metadata,
            idempotent=result.idempotent,
        )
