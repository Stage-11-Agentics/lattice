"""Task CRUD and snapshot materialization."""

from __future__ import annotations

import copy
import json
import sys

from lattice.core.acceptance_criteria import (
    find_criterion,
    normalize_criterion_ids,
    normalize_outcome,
    validate_criterion_id,
)

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
        "last_status_changed_at",
        "status",
        "assigned_to",
        "relationships_out",
        "evidence_refs",
        "branch_links",
        "linked_files",
        "comment_count",
        "reopened_count",
        "custom_fields",
        "needs_human",
        "acceptance_criteria",
    }
)

# Canonical workflow order used for backwards-transition detection in snapshots.
_DEFAULT_STATUS_ORDER: tuple[str, ...] = (
    "backlog",
    "in_planning",
    "planned",
    "in_progress",
    "review",
    "in_validation",
    "pr_open",
    "done",
    "blocked",
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
        "last_status_changed_at": snapshot.get("last_status_changed_at"),
        "comment_count": snapshot.get("comment_count", 0),
        "reopened_count": snapshot.get("reopened_count", 0),
        "needs_human": snapshot.get("needs_human"),
        "relationships_out_count": len(snapshot.get("relationships_out", [])),
        "evidence_ref_count": len(snapshot.get("evidence_refs", [])),
        "branch_link_count": len(snapshot.get("branch_links", [])),
        "linked_file_count": len(snapshot.get("linked_files", [])),
        "acceptance_criteria_count": sum(
            1 for criterion in snapshot.get("acceptance_criteria", []) if not criterion["retired"]
        ),
        "retired_acceptance_criteria_count": sum(
            1 for criterion in snapshot.get("acceptance_criteria", []) if criterion["retired"]
        ),
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
        "last_status_changed_at": event["ts"],
        "relationships_out": [],
        "evidence_refs": [],
        "acceptance_criteria": [],
        "branch_links": [],
        "linked_files": [],
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
        "reaction_added",
        "reaction_removed",
        "git_event",
        "task_archived",
        "task_unarchived",
        # Auto-fire (LAT-211): logged for the audit trail (which review
        # was auto-spawned, when, by which transition) but does not
        # mutate the task snapshot — the eventual review artifact does.
        "auto_review_spawned",
    }
)


@_register_mutation("status_changed")
def _mut_status_changed(snap: dict, event: dict) -> None:
    data = event["data"]
    from_status = data.get("from")
    if "from" in data and from_status != snap.get("status"):
        raise ValueError(
            "status_changed from value does not match authoritative state: "
            f"expected {snap.get('status')!r}, got {from_status!r}"
        )
    new_status = data["to"]
    if is_backward_status_transition(from_status, new_status):
        snap["reopened_count"] = snap.get("reopened_count", 0) + 1
    else:
        snap.setdefault("reopened_count", 0)
    snap["status"] = new_status
    snap["last_status_changed_at"] = event["ts"]
    if new_status == "done":
        snap["done_at"] = event["ts"]
    elif snap.get("done_at") is not None:
        # Transitioning away from done (reopened task) — clear done_at
        snap["done_at"] = None


@_register_mutation("needs_human_flagged")
def _mut_needs_human_flagged(snap: dict, event: dict) -> None:
    snap["needs_human"] = {
        "flagged_by": event["actor"],
        "reason": event["data"]["reason"],
        "since": event["ts"],
    }


@_register_mutation("needs_human_cleared")
def _mut_needs_human_cleared(snap: dict, event: dict) -> None:
    snap["needs_human"] = None


@_register_mutation("assignment_changed")
def _mut_assignment_changed(snap: dict, event: dict) -> None:
    data = event["data"]
    if "from" in data and data["from"] != snap.get("assigned_to"):
        raise ValueError(
            "assignment_changed from value does not match authoritative state: "
            f"expected {snap.get('assigned_to')!r}, got {data['from']!r}"
        )
    snap["assigned_to"] = data["to"]


def _from_matches(recorded, current) -> bool:
    """Does a field_updated event's recorded ``from`` match the replayed state?

    Legacy allowance: writers before LAT-264's strict replay recorded ``from: []``
    for list fields that had never been initialized (the write-time snapshot
    defaulted them to ``[]``; raw replay leaves them absent, i.e. ``None``).
    115 of the acetate board's 1020 task logs carry that shape, so an unset
    field and an empty list are accepted as the same starting state.
    """
    if recorded == current:
        return True
    return recorded == [] and current is None


@_register_mutation("field_updated")
def _mut_field_updated(snap: dict, event: dict) -> None:
    data = event["data"]
    field = data["field"]
    value = data["to"]
    if field.startswith("custom_fields."):
        key = field[len("custom_fields.") :]
        current = (snap.get("custom_fields") or {}).get(key)
        if "from" in data and not _from_matches(data["from"], current):
            raise ValueError(
                "field_updated from value does not match authoritative state: "
                f"expected {current!r}, got {data['from']!r}"
            )
        if snap.get("custom_fields") is None:
            snap["custom_fields"] = {}
        snap["custom_fields"][key] = value
    elif field in PROTECTED_FIELDS:
        raise ValueError(
            f"Cannot update protected field '{field}' via field_updated. "
            "Use the dedicated command (e.g., status, assign) instead."
        )
    else:
        if "from" in data and not _from_matches(data["from"], snap.get(field)):
            raise ValueError(
                "field_updated from value does not match authoritative state: "
                f"expected {snap.get(field)!r}, got {data['from']!r}"
            )
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
    criterion_ids = normalize_criterion_ids(data.get("criterion_ids"), snapshot=snap)
    refs = snap.setdefault("evidence_refs", [])
    # Deduplicate by artifact ID
    for ref in refs:
        if ref.get("source_type") == "artifact":
            existing_id = ref["id"] if isinstance(ref, dict) else ref
            if existing_id == art_id:
                existing_key = (ref.get("role"), ref.get("criterion_ids", []))
                requested_key = (role, criterion_ids)
                if existing_key != requested_key:
                    raise ValueError(
                        f"Artifact {art_id} is already attached with different task-local linkage."
                    )
                return
    record: dict = {"id": art_id, "role": role, "source_type": "artifact"}
    if criterion_ids:
        record["criterion_ids"] = criterion_ids
    refs.append(record)


@_register_mutation("acceptance_criterion_added")
def _mut_acceptance_criterion_added(snap: dict, event: dict) -> None:
    data = event["data"]
    criterion_id = validate_criterion_id(data.get("criterion_id"))
    outcome = normalize_outcome(data.get("outcome"))
    if outcome != data.get("outcome"):
        raise ValueError("Acceptance-criterion event outcome must already be normalized.")
    if data.get("revision") != 1:
        raise ValueError("An added acceptance criterion must start at revision 1.")
    if find_criterion(snap, criterion_id) is not None:
        raise ValueError(f"Acceptance criterion {criterion_id} already exists.")
    revision = {
        "revision": 1,
        "outcome": outcome,
        "event_id": event["id"],
        "changed_at": event["ts"],
        "changed_by": event["actor"],
    }
    snap.setdefault("acceptance_criteria", []).append(
        {
            "id": criterion_id,
            "outcome": outcome,
            "revision": 1,
            "retired": False,
            "created_at": event["ts"],
            "created_by": event["actor"],
            "updated_at": event["ts"],
            "updated_by": event["actor"],
            "retired_at": None,
            "retired_by": None,
            "revisions": [revision],
        }
    )


@_register_mutation("acceptance_criterion_edited")
def _mut_acceptance_criterion_edited(snap: dict, event: dict) -> None:
    data = event["data"]
    criterion_id = validate_criterion_id(data.get("criterion_id"))
    criterion = find_criterion(snap, criterion_id)
    if criterion is None:
        raise ValueError(f"Acceptance criterion {criterion_id} does not exist.")
    if criterion["retired"]:
        raise ValueError(f"Acceptance criterion {criterion_id} is retired.")
    outcome = normalize_outcome(data.get("outcome"))
    if outcome != data.get("outcome"):
        raise ValueError("Acceptance-criterion event outcome must already be normalized.")
    if data.get("from_outcome") != criterion["outcome"]:
        raise ValueError("Acceptance-criterion edit from_outcome does not match authority.")
    expected_revision = criterion["revision"] + 1
    if data.get("revision") != expected_revision:
        raise ValueError(f"Acceptance-criterion edit revision must be {expected_revision}.")
    if outcome == criterion["outcome"]:
        raise ValueError("Acceptance-criterion edit event cannot be a no-op.")
    criterion["outcome"] = outcome
    criterion["revision"] = expected_revision
    criterion["updated_at"] = event["ts"]
    criterion["updated_by"] = event["actor"]
    criterion["revisions"].append(
        {
            "revision": expected_revision,
            "outcome": outcome,
            "event_id": event["id"],
            "changed_at": event["ts"],
            "changed_by": event["actor"],
        }
    )


@_register_mutation("acceptance_criterion_retired")
def _mut_acceptance_criterion_retired(snap: dict, event: dict) -> None:
    data = event["data"]
    criterion_id = validate_criterion_id(data.get("criterion_id"))
    criterion = find_criterion(snap, criterion_id)
    if criterion is None:
        raise ValueError(f"Acceptance criterion {criterion_id} does not exist.")
    if criterion["retired"]:
        raise ValueError(f"Acceptance criterion {criterion_id} is already retired.")
    if data.get("revision") != criterion["revision"]:
        raise ValueError(
            "Acceptance-criterion retirement must record the current outcome revision."
        )
    criterion["retired"] = True
    criterion["retired_at"] = event["ts"]
    criterion["retired_by"] = event["actor"]
    criterion["updated_at"] = event["ts"]
    criterion["updated_by"] = event["actor"]


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


@_register_mutation("file_linked")
def _mut_file_linked(snap: dict, event: dict) -> None:
    data = event["data"]
    paths = data.get("paths", [])
    linked = snap.setdefault("linked_files", [])
    for p in paths:
        if p not in linked:
            linked.append(p)


@_register_mutation("file_unlinked")
def _mut_file_unlinked(snap: dict, event: dict) -> None:
    data = event["data"]
    paths = data.get("paths", [])
    snap["linked_files"] = [f for f in snap.get("linked_files", []) if f not in paths]


@_register_mutation("comment_added")
def _mut_comment_added(snap: dict, event: dict) -> None:
    snap["comment_count"] = snap.get("comment_count", 0) + 1
    data = event.get("data", {})
    role = data.get("role")
    criterion_ids = normalize_criterion_ids(data.get("criterion_ids"), snapshot=snap)
    if role is not None or criterion_ids:
        evidence_refs = snap.setdefault("evidence_refs", [])
        record: dict = {"id": event["id"], "role": role, "source_type": "comment"}
        if criterion_ids:
            record["criterion_ids"] = criterion_ids
        evidence_refs.append(record)


@_register_mutation("comment_edited")
def _mut_comment_edited(snap: dict, event: dict) -> None:
    data = event.get("data", {})
    comment_id = data.get("comment_id")
    if "role" not in data or comment_id is None:
        return  # body-only edit — no evidence_refs changes
    new_role = data["role"]
    evidence_refs = snap.setdefault("evidence_refs", [])
    existing = next(
        (
            er
            for er in evidence_refs
            if er.get("source_type") == "comment" and er.get("id") == comment_id
        ),
        None,
    )
    if existing is None:
        if new_role is not None:
            evidence_refs.append({"id": comment_id, "role": new_role, "source_type": "comment"})
        return
    existing["role"] = new_role
    if new_role is None and not existing.get("criterion_ids"):
        evidence_refs.remove(existing)


@_register_mutation("comment_deleted")
def _mut_comment_deleted(snap: dict, event: dict) -> None:
    snap["comment_count"] = max(0, snap.get("comment_count", 0) - 1)
    comment_id = event.get("data", {}).get("comment_id")
    if comment_id and "evidence_refs" in snap:
        snap["evidence_refs"] = [
            er
            for er in snap["evidence_refs"]
            if not (er.get("source_type") == "comment" and er.get("id") == comment_id)
        ]


@_register_mutation("surface_bound")
def _mut_surface_bound(snap: dict, event: dict) -> None:
    data = event["data"]
    snap["c11_surface"] = data.get("surface")
    workspace = data.get("workspace")
    if workspace is not None:
        snap["c11_workspace"] = workspace
    else:
        snap.setdefault("c11_workspace", None)


@_register_mutation("surface_unbound")
def _mut_surface_unbound(snap: dict, event: dict) -> None:
    snap["c11_surface"] = None
    snap["c11_workspace"] = None


def get_artifact_evidence_refs(snapshot: dict) -> list[dict]:
    """Return copied full artifact evidence records with legacy fallback."""
    evidence_refs = snapshot.get("evidence_refs")
    if evidence_refs is not None:
        return [
            copy.deepcopy(ref) for ref in evidence_refs if ref.get("source_type") == "artifact"
        ]
    records: list[dict] = []
    for ref in snapshot.get("artifact_refs", []):
        if isinstance(ref, dict):
            record = copy.deepcopy(ref)
            record.setdefault("source_type", "artifact")
            record.setdefault("role", None)
        else:
            record = {"id": ref, "role": None, "source_type": "artifact"}
        records.append(record)
    return records


def get_comment_evidence_refs(snapshot: dict) -> list[dict]:
    """Return copied full comment evidence records with legacy fallback."""
    evidence_refs = snapshot.get("evidence_refs")
    if evidence_refs is not None:
        return [copy.deepcopy(ref) for ref in evidence_refs if ref.get("source_type") == "comment"]
    records: list[dict] = []
    for ref in snapshot.get("comment_role_refs", []):
        record = copy.deepcopy(ref)
        record.setdefault("source_type", "comment")
        record.setdefault("role", None)
        records.append(record)
    return records


def get_artifact_roles(snapshot: dict) -> dict[str, str | None]:
    """Return ``{artifact_id: role}`` from a snapshot's evidence refs.

    Reads from ``evidence_refs`` (source_type=="artifact") first.  Falls back
    to the legacy ``artifact_refs`` field for old snapshots that haven't been
    rebuilt yet.  Handles bare string IDs (old format) and enriched dicts.
    """
    return {ref["id"]: ref.get("role") for ref in get_artifact_evidence_refs(snapshot)}


def get_comment_role_refs(snapshot: dict) -> dict[str, str | None]:
    """Return ``{comment_id: role}`` from a snapshot's evidence refs.

    Reads from ``evidence_refs`` (source_type=="comment") first.  Falls back
    to the legacy ``comment_role_refs`` field for old snapshots.
    """
    return {ref["id"]: ref.get("role") for ref in get_comment_evidence_refs(snapshot)}


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
