"""Artifact metadata and linkage."""

from __future__ import annotations

import json
from datetime import datetime, timezone

# ---------------------------------------------------------------------------
# Artifact types (section 10.2 of ProjectRequirements_v1)
# ---------------------------------------------------------------------------

ARTIFACT_TYPES: frozenset[str] = frozenset(
    {
        "conversation",
        "note",
        "prompt",
        "file",
        "log",
        "reference",
    }
)


# ---------------------------------------------------------------------------
# Metadata construction
# ---------------------------------------------------------------------------


def create_artifact_metadata(
    art_id: str,
    type: str,
    title: str,
    *,
    created_by: str,
    created_at: str | None = None,
    summary: str | None = None,
    model: str | None = None,
    tags: list[str] | None = None,
    payload_file: str | None = None,
    content_type: str | None = None,
    size_bytes: int | None = None,
    sensitive: bool = False,
    custom_fields: dict | None = None,
) -> dict:
    """Build an artifact metadata dict.

    Flat ``payload_file``, ``content_type``, and ``size_bytes`` parameters are
    assembled into a nested ``payload`` object in the output.

    If *created_at* is not provided, the current UTC time is used.
    """
    if created_at is None:
        created_at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    return {
        "schema_version": 1,
        "id": art_id,
        "type": type,
        "title": title,
        "summary": summary,
        "created_at": created_at,
        "created_by": created_by,
        "model": model,
        "tags": tags if tags is not None else [],
        "payload": {
            "file": payload_file,
            "content_type": content_type,
            "size_bytes": size_bytes,
        },
        "token_usage": None,
        "sensitive": sensitive,
        "custom_fields": custom_fields if custom_fields is not None else {},
    }


# ---------------------------------------------------------------------------
# Serialization
# ---------------------------------------------------------------------------


def serialize_artifact(metadata: dict) -> str:
    """Serialize artifact metadata as sorted, pretty JSON with trailing newline."""
    return json.dumps(metadata, sort_keys=True, indent=2) + "\n"
