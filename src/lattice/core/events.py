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
        "comment_edited",
        "comment_deleted",
        "reaction_added",
        "reaction_removed",
        "relationship_added",
        "relationship_removed",
        "artifact_attached",
        "alert_raised",
        "alert_cleared",
        "git_event",
        "branch_linked",
        "branch_unlinked",
        "file_linked",
        "file_unlinked",
        "resource_created",
        "resource_acquired",
        "resource_released",
        "resource_heartbeat",
        "resource_expired",
        "resource_updated",
    }
)

RESOURCE_EVENT_TYPES: frozenset[str] = frozenset(
    {
        "resource_created",
        "resource_acquired",
        "resource_released",
        "resource_heartbeat",
        "resource_expired",
        "resource_updated",
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
    actor: str | dict,
    data: dict,
    *,
    event_id: str | None = None,
    ts: str | None = None,
    model: str | None = None,
    session: str | None = None,
    triggered_by: str | None = None,
    on_behalf_of: str | None = None,
    reason: str | None = None,
) -> dict:
    """Build a complete event dict.

    *actor* may be a legacy string (``"agent:claude"``) or a structured
    identity dict (from ``ActorIdentity.to_dict()``).  Both are stored
    directly in the ``actor`` field.

    When *actor* is a string and *model*/*session* are provided, they are
    stored in ``agent_meta`` for backward compatibility.  When *actor* is
    already a structured dict, ``agent_meta`` is omitted (the identity
    object carries model/session directly).

    The ``provenance`` object is included **only** when at least one of
    *triggered_by*, *on_behalf_of*, or *reason* is provided (sparse dict).
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

    # Only include agent_meta for legacy string actors
    if isinstance(actor, str) and (model is not None or session is not None):
        event["agent_meta"] = {"model": model, "session": session}

    if triggered_by is not None or on_behalf_of is not None or reason is not None:
        prov: dict = {}
        if triggered_by is not None:
            prov["triggered_by"] = triggered_by
        if on_behalf_of is not None:
            prov["on_behalf_of"] = on_behalf_of
        if reason is not None:
            prov["reason"] = reason
        event["provenance"] = prov

    return event


def create_resource_event(
    type: str,
    resource_id: str,
    actor: str | dict,
    data: dict,
    *,
    event_id: str | None = None,
    ts: str | None = None,
    model: str | None = None,
    session: str | None = None,
    triggered_by: str | None = None,
    on_behalf_of: str | None = None,
    reason: str | None = None,
) -> dict:
    """Build a complete resource event dict.

    Parallels ``create_event()`` but uses ``resource_id`` instead of
    ``task_id``.  Accepts both legacy string and structured dict actors.
    """
    event: dict = {
        "schema_version": 1,
        "id": event_id if event_id is not None else generate_event_id(),
        "ts": ts if ts is not None else utc_now(),
        "type": type,
        "resource_id": resource_id,
        "actor": actor,
        "data": data,
    }

    if isinstance(actor, str) and (model is not None or session is not None):
        event["agent_meta"] = {"model": model, "session": session}

    if triggered_by is not None or on_behalf_of is not None or reason is not None:
        prov: dict = {}
        if triggered_by is not None:
            prov["triggered_by"] = triggered_by
        if on_behalf_of is not None:
            prov["on_behalf_of"] = on_behalf_of
        if reason is not None:
            prov["reason"] = reason
        event["provenance"] = prov

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
# Actor helpers
# ---------------------------------------------------------------------------


def get_actor_display(actor: str | dict) -> str:
    """Return a human-readable display string for an actor.

    If *actor* is a structured dict, returns the ``name`` field (e.g.,
    ``"Argus-3"``).  If *actor* is a legacy string (``"agent:claude"``),
    returns it as-is.
    """
    if isinstance(actor, dict):
        return actor.get("name", str(actor))
    return actor


def get_actor_session(actor: str | dict) -> str | None:
    """Extract the session ULID from an actor, if available."""
    if isinstance(actor, dict):
        return actor.get("session")
    return None


# ---------------------------------------------------------------------------
# Review rework cycle counting
# ---------------------------------------------------------------------------


def count_review_rework_cycles(events: list[dict]) -> int:
    """Count the number of review-to-rework transitions in a task's event log.

    Counts status_changed events where from in ("review", "pr_open") and
    to in ("in_progress", "in_planning"). This is the number of times
    a task has been sent back from local review or an open PR for rework.
    """
    count = 0
    for event in events:
        if event.get("type") != "status_changed":
            continue
        data = event.get("data", {})
        if data.get("from") in ("review", "pr_open") and data.get("to") in (
            "in_progress",
            "in_planning",
        ):
            count += 1
    return count


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def utc_now() -> str:
    """Return the current UTC time as an RFC 3339 string with ``Z`` suffix."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# ---------------------------------------------------------------------------
# Alert payload validation (LAT-210)
# ---------------------------------------------------------------------------

# Field-length caps for alert payloads.  CLI raises on violation; replay
# truncates instead so historical events never break snapshot rebuild.
ALERT_SHORT_MAX = 200
ALERT_LONG_MAX = 4096
ALERT_PROMPT_MAX = 256


def validate_alert_payload(data: dict) -> tuple[bool, list[str]]:
    """Validate an ``alert_raised`` event ``data`` payload.

    Returns ``(True, [])`` when the payload conforms.  Returns
    ``(False, [error, ...])`` when it does not.  CLI callers should raise
    on errors; mutation handlers must truncate instead so an oversize
    legacy event still replays into a (slightly degraded) snapshot.
    """
    errors: list[str] = []

    name = data.get("name")
    if not isinstance(name, str) or not name:
        errors.append("alert.name is required and must be a non-empty string")

    short = data.get("short")
    if not isinstance(short, str) or not short:
        errors.append("alert.short is required and must be a non-empty string")
    elif len(short) > ALERT_SHORT_MAX:
        errors.append(f"alert.short exceeds {ALERT_SHORT_MAX} chars")

    long = data.get("long")
    if long is not None:
        if not isinstance(long, str):
            errors.append("alert.long must be a string when set")
        elif len(long) > ALERT_LONG_MAX:
            errors.append(f"alert.long exceeds {ALERT_LONG_MAX} chars")

    prompt = data.get("prompt")
    if prompt is not None:
        if not isinstance(prompt, str):
            errors.append("alert.prompt must be a string when set")
        elif len(prompt) > ALERT_PROMPT_MAX:
            errors.append(f"alert.prompt exceeds {ALERT_PROMPT_MAX} chars")

    return (not errors, errors)


def truncate_alert_payload(data: dict) -> dict:
    """Return a copy of *data* with oversized string fields truncated.

    Used by the replay/mutation path so that an oversize historical event
    does not crash snapshot rebuild.  CLI writers should use
    ``validate_alert_payload`` and reject before writing.
    """
    out = dict(data)
    short = out.get("short")
    if isinstance(short, str) and len(short) > ALERT_SHORT_MAX:
        out["short"] = short[:ALERT_SHORT_MAX]
    long = out.get("long")
    if isinstance(long, str) and len(long) > ALERT_LONG_MAX:
        out["long"] = long[:ALERT_LONG_MAX]
    prompt = out.get("prompt")
    if isinstance(prompt, str) and len(prompt) > ALERT_PROMPT_MAX:
        out["prompt"] = prompt[:ALERT_PROMPT_MAX]
    return out
