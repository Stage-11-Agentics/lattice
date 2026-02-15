"""Task CRUD and snapshot materialization."""

from __future__ import annotations

import copy
import json
import sys

# Fields that cannot be overwritten by field_updated events.  These are
# managed exclusively by internal bookkeeping or dedicated event types.
PROTECTED_FIELDS: frozenset[str] = frozenset(
    {
        "schema_version",
        "id",
        "created_at",
        "created_by",
        "updated_at",
        "last_event_id",
        "status",
        "assigned_to",
        "relationships_out",
        "artifact_refs",
        "custom_fields",
    }
)


# ---------------------------------------------------------------------------
# Snapshot materialization
# ---------------------------------------------------------------------------


def apply_event_to_snapshot(snapshot: dict | None, event: dict) -> dict:
    """Apply a single *event* to an existing *snapshot* (or ``None``).

    This is the **single materialization path** used by both incremental
    writes and full rebuild.  All timestamps are sourced from ``event["ts"]``
    -- never from wall clock -- to guarantee rebuild determinism.

    Returns a new (or mutated) snapshot dict.
    """
    etype = event["type"]

    if etype == "task_created":
        snap = _init_snapshot(event)
    else:
        if snapshot is None:
            msg = (
                f"Cannot apply event type '{etype}' without an existing "
                "snapshot (expected 'task_created' first)"
            )
            raise ValueError(msg)
        # Deep copy so callers keep the original intact (including nested
        # dicts like custom_fields and lists like relationships_out).
        snap = copy.deepcopy(snapshot)
        _apply_mutation(snap, etype, event)

    # Every event updates bookkeeping fields.
    snap["last_event_id"] = event["id"]
    snap["updated_at"] = event["ts"]
    return snap


# ---------------------------------------------------------------------------
# Serialization helpers
# ---------------------------------------------------------------------------


def serialize_snapshot(snapshot: dict) -> str:
    """Pretty-print a snapshot as sorted JSON with trailing newline."""
    return json.dumps(snapshot, sort_keys=True, indent=2) + "\n"


def compact_snapshot(snapshot: dict) -> dict:
    """Return a compact view suitable for list/board operations.

    Includes counts for relationships and artifacts instead of full arrays.
    """
    return {
        "id": snapshot.get("id"),
        "title": snapshot.get("title"),
        "status": snapshot.get("status"),
        "priority": snapshot.get("priority"),
        "urgency": snapshot.get("urgency"),
        "type": snapshot.get("type"),
        "assigned_to": snapshot.get("assigned_to"),
        "tags": snapshot.get("tags"),
        "relationships_out_count": len(snapshot.get("relationships_out", [])),
        "artifact_ref_count": len(snapshot.get("artifact_refs", [])),
    }


# ---------------------------------------------------------------------------
# Internal: snapshot initialization from task_created
# ---------------------------------------------------------------------------


def _init_snapshot(event: dict) -> dict:
    """Build a brand-new snapshot from a ``task_created`` event."""
    data = event["data"]
    return {
        "schema_version": 1,
        "id": event["task_id"],
        "title": data.get("title"),
        "status": data.get("status"),
        "priority": data.get("priority"),
        "urgency": data.get("urgency"),
        "type": data.get("type"),
        "description": data.get("description"),
        "tags": data.get("tags"),
        "assigned_to": data.get("assigned_to"),
        "created_by": event["actor"],
        "created_at": event["ts"],
        "updated_at": event["ts"],
        "relationships_out": [],
        "artifact_refs": [],
        "custom_fields": data.get("custom_fields") or {},
        "last_event_id": event["id"],
    }


# ---------------------------------------------------------------------------
# Internal: mutation registry
# ---------------------------------------------------------------------------

# Handler registry: maps event type to a function(snap, event) that mutates
# the snapshot in-place.  Handlers are registered via @_register_mutation.
_MUTATION_HANDLERS: dict[str, callable] = {}


def _register_mutation(etype: str):  # noqa: ANN202
    """Decorator that registers a snapshot mutation handler for *etype*."""

    def decorator(fn):  # noqa: ANN001, ANN202
        _MUTATION_HANDLERS[etype] = fn
        return fn

    return decorator


# Recognised event types that don't modify snapshot fields beyond the
# bookkeeping (last_event_id, updated_at) handled by the caller.
_NOOP_EVENT_TYPES: frozenset[str] = frozenset(
    {
        "comment_added",
        "git_event",
        "task_archived",
        "task_unarchived",
    }
)


@_register_mutation("status_changed")
def _mut_status_changed(snap: dict, event: dict) -> None:
    snap["status"] = event["data"]["to"]


@_register_mutation("assignment_changed")
def _mut_assignment_changed(snap: dict, event: dict) -> None:
    snap["assigned_to"] = event["data"]["to"]


@_register_mutation("field_updated")
def _mut_field_updated(snap: dict, event: dict) -> None:
    data = event["data"]
    field = data["field"]
    value = data["to"]
    if field.startswith("custom_fields."):
        key = field[len("custom_fields.") :]
        if snap.get("custom_fields") is None:
            snap["custom_fields"] = {}
        snap["custom_fields"][key] = value
    elif field in PROTECTED_FIELDS:
        raise ValueError(
            f"Cannot update protected field '{field}' via field_updated. "
            "Use the dedicated command (e.g., status, assign) instead."
        )
    else:
        snap[field] = value


@_register_mutation("relationship_added")
def _mut_relationship_added(snap: dict, event: dict) -> None:
    data = event["data"]
    record = {
        "type": data["type"],
        "target_task_id": data["target_task_id"],
        "created_at": event["ts"],
        "created_by": event["actor"],
        "note": data.get("note"),
    }
    snap.setdefault("relationships_out", []).append(record)


@_register_mutation("relationship_removed")
def _mut_relationship_removed(snap: dict, event: dict) -> None:
    data = event["data"]
    rm_type = data["type"]
    rm_target = data["target_task_id"]
    rels = [
        r
        for r in snap.get("relationships_out", [])
        if not (r["type"] == rm_type and r["target_task_id"] == rm_target)
    ]
    snap["relationships_out"] = rels


@_register_mutation("artifact_attached")
def _mut_artifact_attached(snap: dict, event: dict) -> None:
    snap.setdefault("artifact_refs", []).append(event["data"]["artifact_id"])


def _apply_mutation(snap: dict, etype: str, event: dict) -> None:
    """Mutate *snap* in-place based on event type.

    ``last_event_id`` and ``updated_at`` are handled by the caller so that
    they are applied uniformly for **all** event types, including no-op ones
    like ``comment_added``.
    """
    handler = _MUTATION_HANDLERS.get(etype)
    if handler is not None:
        handler(snap, event)
    elif etype in _NOOP_EVENT_TYPES:
        pass
    elif etype.startswith("x_"):
        # Custom event type -- no snapshot field changes.
        pass
    else:
        # Unknown built-in types: warn for discoverability but don't fail,
        # to preserve forward compatibility (section 6).
        print(
            f"Warning: unknown event type '{etype}' ignored during snapshot materialization",
            file=sys.stderr,
        )
