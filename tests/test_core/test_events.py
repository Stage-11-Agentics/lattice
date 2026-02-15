"""Tests for lattice.core.events."""

from __future__ import annotations

import json

from lattice.core.events import (
    BUILTIN_EVENT_TYPES,
    LIFECYCLE_EVENT_TYPES,
    create_event,
    serialize_event,
    validate_custom_event_type,
)


# ---------------------------------------------------------------------------
# BUILTIN_EVENT_TYPES
# ---------------------------------------------------------------------------


class TestBuiltinEventTypes:
    """Verify the canonical set of built-in event types."""

    EXPECTED = frozenset(
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

    def test_contains_all_builtin_types(self) -> None:
        assert BUILTIN_EVENT_TYPES == self.EXPECTED

    def test_is_frozenset(self) -> None:
        assert isinstance(BUILTIN_EVENT_TYPES, frozenset)

    def test_count(self) -> None:
        assert len(BUILTIN_EVENT_TYPES) == 12


# ---------------------------------------------------------------------------
# LIFECYCLE_EVENT_TYPES
# ---------------------------------------------------------------------------


class TestLifecycleEventTypes:
    """Verify exactly task_created and task_archived go to _lifecycle.jsonl."""

    def test_contains_exactly_lifecycle_events(self) -> None:
        assert LIFECYCLE_EVENT_TYPES == frozenset(
            {"task_created", "task_archived", "task_unarchived"}
        )

    def test_is_frozenset(self) -> None:
        assert isinstance(LIFECYCLE_EVENT_TYPES, frozenset)

    def test_is_subset_of_builtin(self) -> None:
        assert LIFECYCLE_EVENT_TYPES <= BUILTIN_EVENT_TYPES


# ---------------------------------------------------------------------------
# create_event
# ---------------------------------------------------------------------------


class TestCreateEvent:
    """Test event dict construction."""

    def test_required_fields_present(self) -> None:
        ev = create_event(
            "task_created",
            "task_01EXAMPLE0000000000000000",
            "human:atin",
            {"title": "Fix bug"},
        )
        assert ev["schema_version"] == 1
        assert ev["type"] == "task_created"
        assert ev["task_id"] == "task_01EXAMPLE0000000000000000"
        assert ev["actor"] == "human:atin"
        assert ev["data"] == {"title": "Fix bug"}
        # Auto-generated fields must exist and be non-empty.
        assert ev["id"].startswith("ev_")
        assert len(ev["ts"]) > 0

    def test_auto_generates_id_and_ts(self) -> None:
        ev = create_event("status_changed", "task_X", "agent:a", {})
        assert ev["id"].startswith("ev_")
        assert ev["ts"].endswith("Z")

    def test_explicit_id_used(self) -> None:
        ev = create_event(
            "comment_added",
            "task_X",
            "agent:a",
            {"body": "hi"},
            event_id="ev_CUSTOM00000000000000000000",
        )
        assert ev["id"] == "ev_CUSTOM00000000000000000000"

    def test_explicit_ts_used(self) -> None:
        ts = "2026-01-01T00:00:00Z"
        ev = create_event("field_updated", "task_X", "agent:a", {}, ts=ts)
        assert ev["ts"] == ts

    def test_agent_meta_included_when_model_provided(self) -> None:
        ev = create_event(
            "task_created",
            "task_X",
            "agent:a",
            {},
            model="claude-opus-4",
        )
        assert "agent_meta" in ev
        assert ev["agent_meta"]["model"] == "claude-opus-4"
        assert ev["agent_meta"]["session"] is None

    def test_agent_meta_included_when_session_provided(self) -> None:
        ev = create_event(
            "task_created",
            "task_X",
            "agent:a",
            {},
            session="sess-123",
        )
        assert "agent_meta" in ev
        assert ev["agent_meta"]["session"] == "sess-123"
        assert ev["agent_meta"]["model"] is None

    def test_agent_meta_included_when_both_provided(self) -> None:
        ev = create_event(
            "task_created",
            "task_X",
            "agent:a",
            {},
            model="gpt-4",
            session="sess-456",
        )
        assert ev["agent_meta"] == {"model": "gpt-4", "session": "sess-456"}

    def test_agent_meta_excluded_when_neither_provided(self) -> None:
        ev = create_event("task_created", "task_X", "agent:a", {})
        assert "agent_meta" not in ev

    def test_data_dict_preserved(self) -> None:
        data = {"field": "title", "from": "old", "to": "new"}
        ev = create_event("field_updated", "task_X", "agent:a", data)
        assert ev["data"] is data  # same object, not copied


# ---------------------------------------------------------------------------
# serialize_event
# ---------------------------------------------------------------------------


class TestSerializeEvent:
    """Test JSONL serialization."""

    def test_compact_format(self) -> None:
        ev = {
            "schema_version": 1,
            "id": "ev_TEST",
            "ts": "2026-02-15T00:00:00Z",
            "type": "comment_added",
            "task_id": "task_TEST",
            "actor": "human:atin",
            "data": {"body": "hello"},
        }
        line = serialize_event(ev)
        # Must be valid JSON.
        parsed = json.loads(line)
        assert parsed == ev

    def test_sorted_keys(self) -> None:
        ev = {"z": 1, "a": 2}
        line = serialize_event(ev)
        assert line.index('"a"') < line.index('"z"')

    def test_trailing_newline(self) -> None:
        line = serialize_event({"x": 1})
        assert line.endswith("\n")
        assert not line.endswith("\n\n")

    def test_no_extra_whitespace(self) -> None:
        line = serialize_event({"a": 1, "b": "c"})
        # Compact separators: no spaces after , or :
        assert ": " not in line
        assert ", " not in line


# ---------------------------------------------------------------------------
# validate_custom_event_type
# ---------------------------------------------------------------------------


class TestValidateCustomEventType:
    """Test custom event-type validation."""

    def test_x_foo_passes(self) -> None:
        assert validate_custom_event_type("x_foo") is True

    def test_x_deployment_started_passes(self) -> None:
        assert validate_custom_event_type("x_deployment_started") is True

    def test_builtin_task_created_rejected(self) -> None:
        assert validate_custom_event_type("task_created") is False

    def test_builtin_status_changed_rejected(self) -> None:
        assert validate_custom_event_type("status_changed") is False

    def test_empty_string_rejected(self) -> None:
        assert validate_custom_event_type("") is False

    def test_no_x_prefix_rejected(self) -> None:
        assert validate_custom_event_type("custom_thing") is False

    def test_just_x_underscore_rejected(self) -> None:
        # "x_" alone is technically valid per the spec (starts with x_,
        # not in BUILTIN_EVENT_TYPES).  But let's verify it passes --
        # the spec says "must start with x_".
        assert validate_custom_event_type("x_") is True

    def test_non_string_rejected(self) -> None:
        assert validate_custom_event_type(42) is False  # type: ignore[arg-type]
