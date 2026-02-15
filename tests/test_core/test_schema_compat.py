"""Schema forward-compatibility tests.

Validates that Lattice preserves unknown fields through serialization
roundtrips and handles unknown event types gracefully, ensuring forward
compatibility as the schema evolves.
"""

from __future__ import annotations

import json

from lattice.core.events import create_event, serialize_event
from lattice.core.tasks import apply_event_to_snapshot, serialize_snapshot


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_task_created_event(task_id: str = "task_AAAA") -> dict:
    """Return a minimal task_created event."""
    return create_event(
        type="task_created",
        task_id=task_id,
        actor="human:test",
        data={
            "title": "Schema test task",
            "status": "backlog",
            "priority": "medium",
            "type": "task",
        },
    )


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_snapshot_with_unknown_fields_survives_roundtrip():
    """Unknown fields in a snapshot must be preserved through serialization."""
    event = _make_task_created_event()
    snapshot = apply_event_to_snapshot(None, event)

    # Inject an unknown field (simulating a future schema addition)
    snapshot["future_field"] = "some_future_value"
    snapshot["another_future_list"] = [1, 2, 3]

    serialized = serialize_snapshot(snapshot)
    roundtripped = json.loads(serialized)

    assert roundtripped["future_field"] == "some_future_value"
    assert roundtripped["another_future_list"] == [1, 2, 3]
    # Original fields still intact
    assert roundtripped["id"] == event["task_id"]
    assert roundtripped["schema_version"] == 1


def test_event_with_unknown_fields_survives_roundtrip():
    """Unknown fields in an event must be preserved through serialization."""
    event = _make_task_created_event()

    # Inject an unknown field (simulating a future schema addition)
    event["future_event_field"] = {"nested": True, "version": 42}

    serialized = serialize_event(event)
    roundtripped = json.loads(serialized)

    assert roundtripped["future_event_field"] == {"nested": True, "version": 42}
    # Original fields still intact
    assert roundtripped["type"] == "task_created"
    assert roundtripped["schema_version"] == 1


def test_unknown_event_type_does_not_crash(capsys):
    """An unknown built-in event type should warn on stderr but not raise."""
    event = _make_task_created_event(task_id="task_BBBB")
    snapshot = apply_event_to_snapshot(None, event)

    unknown_event = create_event(
        type="some_future_type",
        task_id="task_BBBB",
        actor="human:test",
        data={"info": "from the future"},
    )

    result = apply_event_to_snapshot(snapshot, unknown_event)

    captured = capsys.readouterr()
    assert "unknown event type" in captured.err.lower()
    assert "some_future_type" in captured.err

    # Bookkeeping fields must still be updated
    assert result["last_event_id"] == unknown_event["id"]
    assert result["updated_at"] == unknown_event["ts"]
    # Existing fields unchanged
    assert result["title"] == "Schema test task"
    assert result["status"] == "backlog"


def test_schema_version_preserved_through_rebuild():
    """schema_version must remain 1 after replaying multiple events."""
    created = _make_task_created_event(task_id="task_CCCC")
    snapshot = apply_event_to_snapshot(None, created)
    assert snapshot["schema_version"] == 1

    # Apply several mutations
    status_event = create_event(
        type="status_changed",
        task_id="task_CCCC",
        actor="human:test",
        data={"from": "backlog", "to": "in_progress"},
    )
    snapshot = apply_event_to_snapshot(snapshot, status_event)

    comment_event = create_event(
        type="comment_added",
        task_id="task_CCCC",
        actor="human:test",
        data={"body": "Working on this now."},
    )
    snapshot = apply_event_to_snapshot(snapshot, comment_event)

    field_event = create_event(
        type="field_updated",
        task_id="task_CCCC",
        actor="human:test",
        data={"field": "title", "from": "Schema test task", "to": "Updated title"},
    )
    snapshot = apply_event_to_snapshot(snapshot, field_event)

    assert snapshot["schema_version"] == 1


def test_custom_event_type_handled_silently(capsys):
    """Custom x_* event types should not produce any warning on stderr."""
    created = _make_task_created_event(task_id="task_DDDD")
    snapshot = apply_event_to_snapshot(None, created)

    custom_event = create_event(
        type="x_deployment",
        task_id="task_DDDD",
        actor="agent:deployer",
        data={"environment": "staging", "sha": "abc123"},
    )

    result = apply_event_to_snapshot(snapshot, custom_event)

    captured = capsys.readouterr()
    # No warning should be printed for x_* types
    assert captured.err == ""

    # Bookkeeping updated
    assert result["last_event_id"] == custom_event["id"]
    assert result["updated_at"] == custom_event["ts"]
    # Existing fields unchanged (custom events don't mutate snapshot fields)
    assert result["title"] == "Schema test task"
    assert result["status"] == "backlog"
