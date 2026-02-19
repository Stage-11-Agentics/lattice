"""Hypothesis property-based tests for lattice core logic."""

from __future__ import annotations

import json

from hypothesis import given, settings, strategies as st

from lattice.core.events import create_event, serialize_event
from lattice.core.ids import generate_task_id
from lattice.core.tasks import _init_snapshot, apply_event_to_snapshot, serialize_snapshot

# ---------------------------------------------------------------------------
# Reusable strategies
# ---------------------------------------------------------------------------

valid_statuses = st.sampled_from(
    [
        "backlog",
        "in_planning",
        "planned",
        "in_progress",
        "review",
        "done",
        "blocked",
        "needs_human",
        "cancelled",
    ]
)
valid_priorities = st.sampled_from(["critical", "high", "medium", "low"])
valid_urgencies = st.sampled_from(["immediate", "high", "normal", "low"])
actor_ids = st.from_regex(r"(human|agent|team):[a-z0-9_]+", fullmatch=True)
valid_rel_types = st.sampled_from(
    [
        "blocks",
        "depends_on",
        "subtask_of",
        "related_to",
        "spawned_by",
        "duplicate_of",
        "supersedes",
    ]
)


def _make_task_created_event(
    title: str,
    status: str,
    priority: str,
    urgency: str,
    actor: str,
) -> dict:
    """Helper: build a task_created event with the given parameters."""
    task_id = generate_task_id()
    return create_event(
        type="task_created",
        task_id=task_id,
        actor=actor,
        data={
            "title": title,
            "status": status,
            "priority": priority,
            "urgency": urgency,
            "type": "task",
            "description": None,
            "tags": [],
            "assigned_to": None,
            "custom_fields": {},
        },
    )


def _required_snapshot_fields() -> set[str]:
    """Derive required snapshot keys from the canonical initializer."""
    event = _make_task_created_event(
        title="Schema baseline",
        status="backlog",
        priority="medium",
        urgency="normal",
        actor="human:test",
    )
    return set(_init_snapshot(event).keys())


REQUIRED_SNAPSHOT_FIELDS = _required_snapshot_fields()


# ---------------------------------------------------------------------------
# 1. Snapshot always valid after creation
# ---------------------------------------------------------------------------


@given(
    title=st.text(min_size=1, max_size=200),
    status=valid_statuses,
    priority=valid_priorities,
    urgency=valid_urgencies,
    actor=actor_ids,
)
@settings(max_examples=50)
def test_snapshot_always_valid_after_creation(
    title: str, status: str, priority: str, urgency: str, actor: str
) -> None:
    event = _make_task_created_event(title, status, priority, urgency, actor)
    snap = apply_event_to_snapshot(None, event)

    assert set(snap.keys()) == REQUIRED_SNAPSHOT_FIELDS
    assert snap["title"] == title
    assert snap["status"] == status
    assert snap["priority"] == priority
    assert snap["urgency"] == urgency
    assert snap["created_by"] == actor
    assert snap["id"] == event["task_id"]
    assert isinstance(snap["relationships_out"], list)
    assert isinstance(snap["evidence_refs"], list)
    assert isinstance(snap["custom_fields"], dict)


# ---------------------------------------------------------------------------
# 2. Rebuild determinism — applying the same event twice yields identical bytes
# ---------------------------------------------------------------------------


@given(
    title=st.text(min_size=1, max_size=100),
    status=valid_statuses,
    priority=valid_priorities,
    actor=actor_ids,
)
@settings(max_examples=50)
def test_rebuild_determinism(title: str, status: str, priority: str, actor: str) -> None:
    event = _make_task_created_event(title, status, priority, "normal", actor)
    snap1 = apply_event_to_snapshot(None, event)
    snap2 = apply_event_to_snapshot(None, event)

    assert serialize_snapshot(snap1) == serialize_snapshot(snap2)


# ---------------------------------------------------------------------------
# 3. Event serialization round-trip
# ---------------------------------------------------------------------------


@given(
    title=st.text(min_size=1, max_size=100),
    status=valid_statuses,
    actor=actor_ids,
)
@settings(max_examples=50)
def test_event_serialization_roundtrip(title: str, status: str, actor: str) -> None:
    event = _make_task_created_event(title, status, "medium", "normal", actor)
    serialized = serialize_event(event)

    # Must be a single line with trailing newline
    assert serialized.endswith("\n")
    assert "\n" not in serialized[:-1]

    parsed = json.loads(serialized)
    assert parsed == event


# ---------------------------------------------------------------------------
# 4. Status reflects last status_changed event
# ---------------------------------------------------------------------------


@given(
    initial_status=valid_statuses,
    status_sequence=st.lists(valid_statuses, min_size=1, max_size=10),
    actor=actor_ids,
)
@settings(max_examples=50)
def test_status_reflects_last_status_changed(
    initial_status: str, status_sequence: list[str], actor: str
) -> None:
    event = _make_task_created_event("Status test", initial_status, "medium", "normal", actor)
    snap = apply_event_to_snapshot(None, event)

    for new_status in status_sequence:
        change_event = create_event(
            type="status_changed",
            task_id=event["task_id"],
            actor=actor,
            data={"from": snap["status"], "to": new_status},
        )
        snap = apply_event_to_snapshot(snap, change_event)

    assert snap["status"] == status_sequence[-1]


# ---------------------------------------------------------------------------
# 5. Add relationship then remove same → empty
# ---------------------------------------------------------------------------


@given(
    rel_type=valid_rel_types,
    actor=actor_ids,
)
@settings(max_examples=50)
def test_relationship_add_remove_equals_empty(rel_type: str, actor: str) -> None:
    event = _make_task_created_event("Rel test", "backlog", "medium", "normal", actor)
    snap = apply_event_to_snapshot(None, event)

    target_id = generate_task_id()

    add_event = create_event(
        type="relationship_added",
        task_id=event["task_id"],
        actor=actor,
        data={"type": rel_type, "target_task_id": target_id},
    )
    snap = apply_event_to_snapshot(snap, add_event)
    assert len(snap["relationships_out"]) == 1

    remove_event = create_event(
        type="relationship_removed",
        task_id=event["task_id"],
        actor=actor,
        data={"type": rel_type, "target_task_id": target_id},
    )
    snap = apply_event_to_snapshot(snap, remove_event)
    assert snap["relationships_out"] == []


# ---------------------------------------------------------------------------
# 6. Custom fields are independent — setting X doesn't affect Y
# ---------------------------------------------------------------------------


@given(
    key_a=st.from_regex(r"[a-z][a-z0-9_]{0,19}", fullmatch=True),
    key_b=st.from_regex(r"[a-z][a-z0-9_]{0,19}", fullmatch=True),
    val_a=st.text(min_size=1, max_size=50),
    val_b=st.text(min_size=1, max_size=50),
    actor=actor_ids,
)
@settings(max_examples=50)
def test_custom_fields_independence(
    key_a: str, key_b: str, val_a: str, val_b: str, actor: str
) -> None:
    # Ensure distinct keys so we can verify independence.
    if key_a == key_b:
        return

    event = _make_task_created_event("CF test", "backlog", "medium", "normal", actor)
    snap = apply_event_to_snapshot(None, event)

    ev_a = create_event(
        type="field_updated",
        task_id=event["task_id"],
        actor=actor,
        data={"field": f"custom_fields.{key_a}", "from": None, "to": val_a},
    )
    snap = apply_event_to_snapshot(snap, ev_a)

    ev_b = create_event(
        type="field_updated",
        task_id=event["task_id"],
        actor=actor,
        data={"field": f"custom_fields.{key_b}", "from": None, "to": val_b},
    )
    snap = apply_event_to_snapshot(snap, ev_b)

    assert snap["custom_fields"][key_a] == val_a
    assert snap["custom_fields"][key_b] == val_b


# ---------------------------------------------------------------------------
# 7. last_event_id always equals the final event's id
# ---------------------------------------------------------------------------


@given(
    n_comments=st.integers(min_value=1, max_value=10),
    actor=actor_ids,
)
@settings(max_examples=50)
def test_last_event_id_always_final(n_comments: int, actor: str) -> None:
    event = _make_task_created_event("Last-event test", "backlog", "medium", "normal", actor)
    snap = apply_event_to_snapshot(None, event)

    last_event_id = event["id"]
    for i in range(n_comments):
        comment_event = create_event(
            type="comment_added",
            task_id=event["task_id"],
            actor=actor,
            data={"body": f"Comment {i}"},
        )
        snap = apply_event_to_snapshot(snap, comment_event)
        last_event_id = comment_event["id"]

    assert snap["last_event_id"] == last_event_id
