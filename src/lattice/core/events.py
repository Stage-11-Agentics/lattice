"""Event creation, schema, and types."""

from __future__ import annotations

import json
from datetime import datetime, timezone

from lattice.core.ids import generate_event_id

# ---------------------------------------------------------------------------
# Built-in event types (section 9.3 of ProjectRequirements_v1)
# ---------------------------------------------------------------------------

BUILTIN_EVENT_TYPES: frozenset[str] = frozenset(
    {
        "task_created",
        "task_archived",
        "task_unarchived",
        "task_short_id_assigned",
        "status_changed",
        "assignment_changed",
        "field_updated",
        "comment_added",
        "relationship_added",
        "relationship_removed",
        "artifact_attached",
        "git_event",
    }
)

# Only lifecycle events go to _lifecycle.jsonl (section 9.1).
LIFECYCLE_EVENT_TYPES: frozenset[str] = frozenset(
    {
        "task_created",
        "task_archived",
        "task_unarchived",
    }
)


# ---------------------------------------------------------------------------
# Event construction
# ---------------------------------------------------------------------------


def create_event(
    type: str,
    task_id: str,
    actor: str,
    data: dict,
    *,
    event_id: str | None = None,
    ts: str | None = None,
    model: str | None = None,
    session: str | None = None,
) -> dict:
    """Build a complete event dict.

    Auto-generates ``id`` and ``ts`` when not supplied.  The ``agent_meta``
    object is included **only** when *model* or *session* is provided.
    """
    event: dict = {
        "schema_version": 1,
        "id": event_id if event_id is not None else generate_event_id(),
        "ts": ts if ts is not None else utc_now(),
        "type": type,
        "task_id": task_id,
        "actor": actor,
        "data": data,
    }

    if model is not None or session is not None:
        event["agent_meta"] = {"model": model, "session": session}

    return event


# ---------------------------------------------------------------------------
# Serialization
# ---------------------------------------------------------------------------


def serialize_event(event: dict) -> str:
    """Serialize an event to compact JSONL (one line, trailing newline)."""
    return json.dumps(event, sort_keys=True, separators=(",", ":")) + "\n"


# ---------------------------------------------------------------------------
# Custom event-type validation
# ---------------------------------------------------------------------------


def validate_custom_event_type(event_type: str) -> bool:
    """Return ``True`` if *event_type* is a valid custom type.

    Custom types must start with ``x_`` and must **not** collide with any
    built-in type name.
    """
    if not isinstance(event_type, str) or not event_type:
        return False
    return event_type.startswith("x_") and event_type not in BUILTIN_EVENT_TYPES


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def utc_now() -> str:
    """Return the current UTC time as an RFC 3339 string with ``Z`` suffix."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
