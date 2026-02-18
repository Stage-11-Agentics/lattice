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
        "short_id",
        "created_at",
        "created_by",
        "updated_at",
        "done_at",
        "last_event_id",
        "status",
        "assigned_to",
        "relationships_out",
        "evidence_refs",
        "branch_links",
        "comment_count",
        "reopened_count",
        "custom_fields",
    }
)

# Canonical workflow order used for backwards-transition detection in snapshots.
_DEFAULT_STATUS_ORDER: tuple[str, ...] = (
    "backlog",
    "in_planning",
    "planned",
    "in_progress",
    "review",
    "done",
    "blocked",
    "needs_human",
    "cancelled",
)
_DEFAULT_STATUS_RANK: dict[str, int] = {
    status: index for index, status in enumerate(_DEFAULT_STATUS_ORDER)
}


def is_backward_status_transition(
    from_status: str | None,
    to_status: str | None,
    status_rank: dict[str, int] | None = None,
) -> bool:
    """Return True when *to_status* is earlier than *from_status* in workflow order."""
    if not isinstance(from_status, str) or not isinstance(to_status, str):
        return False
    rank = status_rank if status_rank is not None else _DEFAULT_STATUS_RANK
    from_rank = rank.get(from_status)
    to_rank = rank.get(to_status)
    if from_rank is None or to_rank is None:
        return False
    return to_rank < from_rank


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
    result = {
        "id": snapshot.get("id"),
        "title": snapshot.get("title"),
        "status": snapshot.get("status"),
        "priority": snapshot.get("priority"),
        "urgency": snapshot.get("urgency"),
        "complexity": snapshot.get("complexity"),
        "type": snapshot.get("type"),
        "assigned_to": snapshot.get("assigned_to"),
        "tags": snapshot.get("tags"),
        "done_at": snapshot.get("done_at"),
        "comment_count": snapshot.get("comment_count", 0),
        "reopened_count": snapshot.get("reopened_count", 0),
        "relationships_out_count": len(snapshot.get("relationships_out", [])),
        "evidence_ref_count": len(snapshot.get("evidence_refs", [])),
        "branch_link_count": len(snapshot.get("branch_links", [])),
    }
    short_id = snapshot.get("short_id")
    if short_id is not None:
        result["short_id"] = short_id
    return result


# ---------------------------------------------------------------------------
# Internal: snapshot initialization from task_created
# ---------------------------------------------------------------------------


def _init_snapshot(event: dict) -> dict:
    """Build a brand-new snapshot from a ``task_created`` event."""
    data = event["data"]
    snap: dict = {
        "schema_version": 1,
        "id": event["task_id"],
        "title": data.get("title"),
        "status": data.get("status"),
        "priority": data.get("priority"),
        "urgency": data.get("urgency"),
        "complexity": data.get("complexity"),
        "type": data.get("type"),
        "description": data.get("description"),
        "tags": data.get("tags"),
        "assigned_to": data.get("assigned_to"),
        "created_by": event["actor"],
        "created_at": event["ts"],
        "updated_at": event["ts"],
        "done_at": event["ts"] if data.get("status") == "done" else None,
        "relationships_out": [],
        "evidence_refs": [],
        "branch_links": [],
        "comment_count": 0,
        "reopened_count": 0,
        "custom_fields": data.get("custom_fields") or {},
        "last_event_id": event["id"],
    }
    short_id = data.get("short_id")
    if short_id is not None:
        snap["short_id"] = short_id
    return snap


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
        "comment_edited",
        "reaction_added",
        "reaction_removed",
        "git_event",
        "task_archived",
        "task_unarchived",
    }
)


@_register_mutation("status_changed")
def _mut_status_changed(snap: dict, event: dict) -> None:
    data = event["data"]
    from_status = data.get("from")
    new_status = data["to"]
    if is_backward_status_transition(from_status, new_status):
        snap["reopened_count"] = snap.get("reopened_count", 0) + 1
    else:
        snap.setdefault("reopened_count", 0)
    snap["status"] = new_status
    if new_status == "done":
        snap["done_at"] = event["ts"]
    elif snap.get("done_at") is not None:
        # Transitioning away from done (reopened task) — clear done_at
        snap["done_at"] = None


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
    data = event["data"]
    art_id = data["artifact_id"]
    role = data.get("role")
    refs = snap.setdefault("evidence_refs", [])
    # Deduplicate by artifact ID
    for ref in refs:
        if ref.get("source_type") == "artifact":
            existing_id = ref["id"] if isinstance(ref, dict) else ref
            if existing_id == art_id:
                return
    refs.append({"id": art_id, "role": role, "source_type": "artifact"})


@_register_mutation("task_short_id_assigned")
def _mut_task_short_id_assigned(snap: dict, event: dict) -> None:
    snap["short_id"] = event["data"]["short_id"]


@_register_mutation("branch_linked")
def _mut_branch_linked(snap: dict, event: dict) -> None:
    data = event["data"]
    record = {
        "branch": data["branch"],
        "repo": data.get("repo"),
        "linked_at": event["ts"],
        "linked_by": event["actor"],
    }
    snap.setdefault("branch_links", []).append(record)


@_register_mutation("branch_unlinked")
def _mut_branch_unlinked(snap: dict, event: dict) -> None:
    data = event["data"]
    rm_branch = data["branch"]
    rm_repo = data.get("repo")
    links = [
        bl
        for bl in snap.get("branch_links", [])
        if not (bl["branch"] == rm_branch and bl.get("repo") == rm_repo)
    ]
    snap["branch_links"] = links


@_register_mutation("comment_added")
def _mut_comment_added(snap: dict, event: dict) -> None:
    snap["comment_count"] = snap.get("comment_count", 0) + 1
    role = event.get("data", {}).get("role")
    if role is not None:
        evidence_refs = snap.setdefault("evidence_refs", [])
        evidence_refs.append({"id": event["id"], "role": role, "source_type": "comment"})


@_register_mutation("comment_deleted")
def _mut_comment_deleted(snap: dict, event: dict) -> None:
    snap["comment_count"] = max(0, snap.get("comment_count", 0) - 1)
    comment_id = event.get("data", {}).get("comment_id")
    if comment_id and "evidence_refs" in snap:
        snap["evidence_refs"] = [
            er for er in snap["evidence_refs"]
            if not (er.get("source_type") == "comment" and er.get("id") == comment_id)
        ]


def get_artifact_roles(snapshot: dict) -> dict[str, str | None]:
    """Return ``{artifact_id: role}`` from a snapshot's evidence refs.

    Reads from ``evidence_refs`` (source_type=="artifact") first.  Falls back
    to the legacy ``artifact_refs`` field for old snapshots that haven't been
    rebuilt yet.  Handles bare string IDs (old format) and enriched dicts.
    """
    result: dict[str, str | None] = {}
    # New unified field
    for ref in snapshot.get("evidence_refs", []):
        if ref.get("source_type") == "artifact":
            result[ref["id"]] = ref.get("role")
    if result or "evidence_refs" in snapshot:
        return result
    # Legacy fallback
    for ref in snapshot.get("artifact_refs", []):
        if isinstance(ref, dict):
            result[ref["id"]] = ref.get("role")
        else:
            result[ref] = None
    return result


def get_comment_role_refs(snapshot: dict) -> dict[str, str | None]:
    """Return ``{comment_id: role}`` from a snapshot's evidence refs.

    Reads from ``evidence_refs`` (source_type=="comment") first.  Falls back
    to the legacy ``comment_role_refs`` field for old snapshots.
    """
    result: dict[str, str | None] = {}
    # New unified field
    for ref in snapshot.get("evidence_refs", []):
        if ref.get("source_type") == "comment":
            result[ref["id"]] = ref.get("role")
    if result or "evidence_refs" in snapshot:
        return result
    # Legacy fallback
    for ref in snapshot.get("comment_role_refs", []):
        result[ref["id"]] = ref.get("role")
    return result


def get_evidence_roles(snapshot: dict) -> set[str]:
    """Return the set of all non-None roles from evidence refs.

    Reads from ``evidence_refs`` first.  Falls back to legacy
    ``artifact_refs`` + ``comment_role_refs`` for old snapshots.
    Used by completion policy validation.
    """
    roles: set[str] = set()
    evidence_refs = snapshot.get("evidence_refs")
    if evidence_refs is not None:
        for ref in evidence_refs:
            role = ref.get("role")
            if role is not None:
                roles.add(role)
        return roles
    # Legacy fallback: merge artifact_refs + comment_role_refs
    for ref in snapshot.get("artifact_refs", []):
        if isinstance(ref, dict):
            role = ref.get("role")
        else:
            role = None
        if role is not None:
            roles.add(role)
    for ref in snapshot.get("comment_role_refs", []):
        role = ref.get("role")
        if role is not None:
            roles.add(role)
    return roles


# ---------------------------------------------------------------------------
# Epic derived status
# ---------------------------------------------------------------------------

# Precedence order for deriving epic status from children (highest urgency first)
_EPIC_STATUS_PRECEDENCE = [
    "needs_human",
    "blocked",
    "in_progress",
    "review",
    "planned",
    "in_planning",
    "backlog",
]


def compute_epic_derived_status(
    epic_id: str,
    all_snapshots: list[dict],
    graph_links: list[dict],
) -> dict:
    """Compute derived status for an epic based on its subtasks.

    Parameters
    ----------
    epic_id:
        The task ID of the epic.
    all_snapshots:
        List of all task snapshot dicts (active tasks).
    graph_links:
        List of relationship link dicts with ``source``, ``target``, ``type`` keys.
        A ``subtask_of`` link has source=child, target=parent.

    Returns
    -------
    dict with keys:
        - derived_status: str | None (highest-urgency child status, or None)
        - progress: {done: int, total: int, cancelled: int}
        - health: {blocked: int, needs_human: int}
        - child_ids: list[str]
    """
    # Build snapshot lookup
    snap_by_id: dict[str, dict] = {s["id"]: s for s in all_snapshots if "id" in s}

    # Build parent -> children map from subtask_of links
    # subtask_of: source is child, target is parent
    parent_to_children: dict[str, list[str]] = {}
    for link in graph_links:
        if link.get("type") == "subtask_of":
            parent = link["target"]
            child = link["source"]
            parent_to_children.setdefault(parent, []).append(child)

    # Recursively collect all descendant task IDs
    child_ids: list[str] = []
    visited: set[str] = set()

    def _collect(parent: str) -> None:
        for cid in parent_to_children.get(parent, []):
            if cid not in visited:
                visited.add(cid)
                child_ids.append(cid)
                _collect(cid)

    _collect(epic_id)

    # Compute progress and health from children
    done = 0
    cancelled = 0
    blocked_count = 0
    needs_human_count = 0
    active_statuses: list[str] = []

    for cid in child_ids:
        snap = snap_by_id.get(cid)
        if snap is None:
            continue
        status = snap.get("status", "backlog")
        if status == "done":
            done += 1
        elif status == "cancelled":
            cancelled += 1
        else:
            active_statuses.append(status)
            if status == "blocked":
                blocked_count += 1
            elif status == "needs_human":
                needs_human_count += 1

    total = len(child_ids) - cancelled

    # Derive status by precedence
    derived_status = None
    if active_statuses:
        for candidate in _EPIC_STATUS_PRECEDENCE:
            if candidate in active_statuses:
                derived_status = candidate
                break
    elif child_ids:
        # All children are done or cancelled
        derived_status = "done"

    return {
        "derived_status": derived_status,
        "progress": {"done": done, "total": total, "cancelled": cancelled},
        "health": {"blocked": blocked_count, "needs_human": needs_human_count},
        "child_ids": child_ids,
    }


def _apply_mutation(snap: dict, etype: str, event: dict) -> None:
    """Mutate *snap* in-place based on event type.

    ``last_event_id`` and ``updated_at`` are handled by the caller so that
    they are applied uniformly for **all** event types, including no-op ones
    like ``comment_edited``.
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
