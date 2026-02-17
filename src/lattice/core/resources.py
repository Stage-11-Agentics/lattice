"""Resource snapshot materialization, availability checks, TTL math, stale detection."""

from __future__ import annotations

import copy
import json
from datetime import datetime, timezone


# ---------------------------------------------------------------------------
# Snapshot materialization
# ---------------------------------------------------------------------------


def apply_resource_event_to_snapshot(snapshot: dict | None, event: dict) -> dict:
    """Apply a single *event* to an existing resource *snapshot* (or ``None``).

    This is the single materialization path used by both incremental writes
    and full rebuild.  All timestamps are sourced from ``event["ts"]`` —
    never from wall clock — to guarantee rebuild determinism.

    Returns a new (or mutated) snapshot dict.
    """
    etype = event["type"]

    if etype == "resource_created":
        snap = _init_resource_snapshot(event)
    else:
        if snapshot is None:
            msg = (
                f"Cannot apply event type '{etype}' without an existing "
                "snapshot (expected 'resource_created' first)"
            )
            raise ValueError(msg)
        snap = copy.deepcopy(snapshot)
        _apply_resource_mutation(snap, etype, event)

    snap["last_event_id"] = event["id"]
    snap["updated_at"] = event["ts"]
    return snap


# ---------------------------------------------------------------------------
# Serialization
# ---------------------------------------------------------------------------


def serialize_resource_snapshot(snapshot: dict) -> str:
    """Pretty-print a resource snapshot as sorted JSON with trailing newline."""
    return json.dumps(snapshot, sort_keys=True, indent=2) + "\n"


# ---------------------------------------------------------------------------
# Availability & TTL
# ---------------------------------------------------------------------------


def is_holder_stale(holder: dict, now: str | None = None) -> bool:
    """Return True if a holder's TTL has expired.

    *now* is an RFC 3339 UTC timestamp.  Defaults to actual wall clock.
    """
    expires_at = holder.get("expires_at")
    if expires_at is None:
        return False
    if now is None:
        now = _utc_now()
    return expires_at < now


def evict_stale_holders(snapshot: dict, now: str | None = None) -> list[dict]:
    """Remove stale holders from snapshot in-place.

    Returns the list of evicted holder dicts (for emitting resource_expired events).
    """
    if now is None:
        now = _utc_now()
    holders = snapshot.get("holders", [])
    stale = [h for h in holders if is_holder_stale(h, now)]
    snapshot["holders"] = [h for h in holders if not is_holder_stale(h, now)]
    return stale


def is_resource_available(snapshot: dict, now: str | None = None) -> bool:
    """Return True if the resource can accept another holder.

    Evaluates TTL at *now* (stale holders don't count).
    """
    if now is None:
        now = _utc_now()
    active = [h for h in snapshot.get("holders", []) if not is_holder_stale(h, now)]
    max_holders = snapshot.get("max_holders", 1)
    return len(active) < max_holders


def find_holder(snapshot: dict, actor: str) -> dict | None:
    """Return the holder entry for *actor*, or None."""
    for h in snapshot.get("holders", []):
        if h.get("actor") == actor:
            return h
    return None


def compute_expires_at(ttl_seconds: int, now: str | None = None) -> str:
    """Compute expiration timestamp from TTL seconds and a base time.

    *now* is an RFC 3339 UTC timestamp.  Defaults to wall clock.
    """
    if now is None:
        now = _utc_now()
    base = datetime.strptime(now, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    from datetime import timedelta

    expires = base + timedelta(seconds=ttl_seconds)
    return expires.strftime("%Y-%m-%dT%H:%M:%SZ")


def format_duration_ago(ts: str, now: str | None = None) -> str:
    """Format a timestamp as a human-readable 'Xm ago' or 'Xh ago' string."""
    if now is None:
        now = _utc_now()
    base = datetime.strptime(now, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    target = datetime.strptime(ts, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    delta = base - target
    total_seconds = int(delta.total_seconds())
    if total_seconds < 0:
        return "in the future"
    if total_seconds < 60:
        return f"{total_seconds}s ago"
    minutes = total_seconds // 60
    if minutes < 60:
        return f"{minutes}m ago"
    hours = minutes // 60
    return f"{hours}h ago"


def format_duration_remaining(ts: str, now: str | None = None) -> str:
    """Format a timestamp as a human-readable 'expires in Xm' string."""
    if now is None:
        now = _utc_now()
    base = datetime.strptime(now, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    target = datetime.strptime(ts, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    delta = target - base
    total_seconds = int(delta.total_seconds())
    if total_seconds <= 0:
        return "expired"
    if total_seconds < 60:
        return f"{total_seconds}s"
    minutes = total_seconds // 60
    if minutes < 60:
        return f"{minutes}m"
    hours = minutes // 60
    return f"{hours}h"


# ---------------------------------------------------------------------------
# Internal: snapshot initialization
# ---------------------------------------------------------------------------


def _init_resource_snapshot(event: dict) -> dict:
    """Build a brand-new resource snapshot from a ``resource_created`` event."""
    data = event["data"]
    return {
        "schema_version": 1,
        "id": event["resource_id"],
        "name": data.get("name"),
        "description": data.get("description"),
        "max_holders": data.get("max_holders", 1),
        "ttl_seconds": data.get("ttl_seconds", 300),
        "holders": [],
        "created_by": event["actor"],
        "created_at": event["ts"],
        "updated_at": event["ts"],
        "last_event_id": event["id"],
    }


# ---------------------------------------------------------------------------
# Internal: mutation handlers
# ---------------------------------------------------------------------------

_RESOURCE_MUTATION_HANDLERS: dict[str, callable] = {}


def _register_resource_mutation(etype: str):  # noqa: ANN202
    def decorator(fn):  # noqa: ANN001, ANN202
        _RESOURCE_MUTATION_HANDLERS[etype] = fn
        return fn

    return decorator


@_register_resource_mutation("resource_acquired")
def _mut_resource_acquired(snap: dict, event: dict) -> None:
    data = event["data"]
    holder = {
        "actor": data["holder"],
        "acquired_at": event["ts"],
        "expires_at": data.get("expires_at"),
        "last_heartbeat": event["ts"],
    }
    if data.get("task_id"):
        holder["task_id"] = data["task_id"]
    snap.setdefault("holders", []).append(holder)


@_register_resource_mutation("resource_released")
def _mut_resource_released(snap: dict, event: dict) -> None:
    data = event["data"]
    holder_actor = data["holder"]
    snap["holders"] = [h for h in snap.get("holders", []) if h.get("actor") != holder_actor]


@_register_resource_mutation("resource_heartbeat")
def _mut_resource_heartbeat(snap: dict, event: dict) -> None:
    data = event["data"]
    holder_actor = data["holder"]
    for h in snap.get("holders", []):
        if h.get("actor") == holder_actor:
            h["expires_at"] = data.get("expires_at")
            h["last_heartbeat"] = event["ts"]
            break


@_register_resource_mutation("resource_expired")
def _mut_resource_expired(snap: dict, event: dict) -> None:
    data = event["data"]
    holder_actor = data["holder"]
    snap["holders"] = [h for h in snap.get("holders", []) if h.get("actor") != holder_actor]


@_register_resource_mutation("resource_updated")
def _mut_resource_updated(snap: dict, event: dict) -> None:
    data = event["data"]
    field = data["field"]
    snap[field] = data["new_value"]


def _apply_resource_mutation(snap: dict, etype: str, event: dict) -> None:
    handler = _RESOURCE_MUTATION_HANDLERS.get(etype)
    if handler is not None:
        handler(snap, event)
    else:
        import sys

        print(
            f"Warning: unknown resource event type '{etype}' ignored during snapshot materialization",
            file=sys.stderr,
        )


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
