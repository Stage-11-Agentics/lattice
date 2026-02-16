"""Tests for core config module."""

from __future__ import annotations

import json

from lattice.core.config import (
    VALID_PRIORITIES,
    VALID_URGENCIES,
    default_config,
    get_wip_limit,
    load_config,
    serialize_config,
    validate_status,
    validate_task_type,
    validate_transition,
)


class TestDefaultConfig:
    """default_config() returns a well-formed configuration dict."""

    def test_has_schema_version(self) -> None:
        config = default_config()
        assert config["schema_version"] == 1

    def test_has_default_status(self) -> None:
        config = default_config()
        assert config["default_status"] == "backlog"

    def test_has_default_priority(self) -> None:
        config = default_config()
        assert config["default_priority"] == "medium"

    def test_has_task_types(self) -> None:
        config = default_config()
        assert config["task_types"] == ["task", "ticket", "epic", "bug", "spike", "chore"]

    def test_workflow_statuses(self) -> None:
        config = default_config()
        expected = [
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
        assert config["workflow"]["statuses"] == expected

    def test_workflow_transitions_keys(self) -> None:
        config = default_config()
        transitions = config["workflow"]["transitions"]
        expected_keys = {
            "backlog",
            "in_planning",
            "planned",
            "in_progress",
            "review",
            "done",
            "blocked",
            "needs_human",
            "cancelled",
        }
        assert set(transitions.keys()) == expected_keys

    def test_terminal_statuses_have_no_transitions(self) -> None:
        config = default_config()
        transitions = config["workflow"]["transitions"]
        assert transitions["done"] == []
        assert transitions["cancelled"] == []

    def test_wip_limits(self) -> None:
        config = default_config()
        wip = config["workflow"]["wip_limits"]
        assert wip == {"in_progress": 10, "review": 5}


class TestSerializeConfig:
    """serialize_config() produces deterministic canonical JSON."""

    def test_sorted_keys(self) -> None:
        config = default_config()
        serialized = serialize_config(config)
        parsed = json.loads(serialized)
        # Re-serialize with sort_keys to verify roundtrip
        reserialized = json.dumps(parsed, sort_keys=True, indent=2) + "\n"
        assert serialized == reserialized

    def test_trailing_newline(self) -> None:
        config = default_config()
        serialized = serialize_config(config)
        assert serialized.endswith("\n")
        assert not serialized.endswith("\n\n")

    def test_two_space_indent(self) -> None:
        config = default_config()
        serialized = serialize_config(config)
        # Second line should start with exactly 2 spaces (first key)
        lines = serialized.split("\n")
        assert lines[1].startswith("  ")
        assert not lines[1].startswith("    ")

    def test_roundtrip(self) -> None:
        config = default_config()
        serialized = serialize_config(config)
        parsed = json.loads(serialized)
        assert parsed == config


# ---------------------------------------------------------------------------
# load_config
# ---------------------------------------------------------------------------


class TestLoadConfig:
    """load_config() parses a JSON string into a config dict."""

    def test_roundtrip_with_default(self) -> None:
        config = default_config()
        raw = serialize_config(config)
        loaded = load_config(raw)
        assert loaded == config

    def test_returns_dict(self) -> None:
        raw = json.dumps({"schema_version": 1, "task_types": []})
        loaded = load_config(raw)
        assert isinstance(loaded, dict)

    def test_preserves_unknown_fields(self) -> None:
        raw = json.dumps({"schema_version": 1, "custom_key": "custom_val"})
        loaded = load_config(raw)
        assert loaded["custom_key"] == "custom_val"

    def test_invalid_json_raises(self) -> None:
        import pytest

        with pytest.raises(json.JSONDecodeError):
            load_config("{bad json")

    def test_empty_object(self) -> None:
        loaded = load_config("{}")
        assert loaded == {}


# ---------------------------------------------------------------------------
# validate_status
# ---------------------------------------------------------------------------


class TestValidateStatus:
    """validate_status() checks membership in workflow.statuses."""

    def test_valid_status(self) -> None:
        config = default_config()
        assert validate_status(config, "backlog") is True

    def test_all_default_statuses_valid(self) -> None:
        config = default_config()
        for status in config["workflow"]["statuses"]:
            assert validate_status(config, status) is True

    def test_unknown_status(self) -> None:
        config = default_config()
        assert validate_status(config, "nonexistent") is False

    def test_empty_string(self) -> None:
        config = default_config()
        assert validate_status(config, "") is False

    def test_missing_workflow_key(self) -> None:
        config: dict = {"schema_version": 1}
        assert validate_status(config, "backlog") is False

    def test_missing_statuses_key(self) -> None:
        config: dict = {"workflow": {}}
        assert validate_status(config, "backlog") is False


# ---------------------------------------------------------------------------
# validate_transition
# ---------------------------------------------------------------------------


class TestValidateTransition:
    """validate_transition() checks allowed workflow transitions."""

    def test_valid_transition_backlog_to_in_planning(self) -> None:
        config = default_config()
        assert validate_transition(config, "backlog", "in_planning") is True

    def test_valid_transition_backlog_to_cancelled(self) -> None:
        config = default_config()
        assert validate_transition(config, "backlog", "cancelled") is True

    def test_invalid_transition_backlog_to_done(self) -> None:
        config = default_config()
        assert validate_transition(config, "backlog", "done") is False

    def test_terminal_status_has_no_transitions(self) -> None:
        config = default_config()
        assert validate_transition(config, "done", "backlog") is False
        assert validate_transition(config, "cancelled", "backlog") is False

    def test_unknown_from_status(self) -> None:
        config = default_config()
        assert validate_transition(config, "nonexistent", "in_planning") is False

    def test_unknown_to_status(self) -> None:
        config = default_config()
        assert validate_transition(config, "backlog", "nonexistent") is False

    def test_missing_workflow_key(self) -> None:
        config: dict = {"schema_version": 1}
        assert validate_transition(config, "backlog", "in_planning") is False

    def test_all_declared_transitions_valid(self) -> None:
        config = default_config()
        transitions = config["workflow"]["transitions"]
        for from_s, to_list in transitions.items():
            for to_s in to_list:
                assert validate_transition(config, from_s, to_s) is True


# ---------------------------------------------------------------------------
# validate_task_type
# ---------------------------------------------------------------------------


class TestValidateTaskType:
    """validate_task_type() checks membership in config.task_types."""

    def test_valid_task_type(self) -> None:
        config = default_config()
        assert validate_task_type(config, "task") is True

    def test_all_default_types_valid(self) -> None:
        config = default_config()
        for tt in config["task_types"]:
            assert validate_task_type(config, tt) is True

    def test_ticket_type_valid(self) -> None:
        config = default_config()
        assert validate_task_type(config, "ticket") is True

    def test_unknown_type(self) -> None:
        config = default_config()
        assert validate_task_type(config, "feature") is False

    def test_empty_string(self) -> None:
        config = default_config()
        assert validate_task_type(config, "") is False

    def test_missing_task_types_key(self) -> None:
        config: dict = {"schema_version": 1}
        assert validate_task_type(config, "task") is False


# ---------------------------------------------------------------------------
# get_wip_limit
# ---------------------------------------------------------------------------


class TestGetWipLimit:
    """get_wip_limit() returns the WIP limit for a status or None."""

    def test_in_progress_limit(self) -> None:
        config = default_config()
        assert get_wip_limit(config, "in_progress") == 10

    def test_review_limit(self) -> None:
        config = default_config()
        assert get_wip_limit(config, "review") == 5

    def test_status_without_limit(self) -> None:
        config = default_config()
        assert get_wip_limit(config, "backlog") is None

    def test_unknown_status(self) -> None:
        config = default_config()
        assert get_wip_limit(config, "nonexistent") is None

    def test_missing_wip_limits_key(self) -> None:
        config: dict = {"workflow": {}}
        assert get_wip_limit(config, "in_progress") is None

    def test_missing_workflow_key(self) -> None:
        config: dict = {"schema_version": 1}
        assert get_wip_limit(config, "in_progress") is None


# ---------------------------------------------------------------------------
# VALID_PRIORITIES / VALID_URGENCIES
# ---------------------------------------------------------------------------


class TestValidPriorities:
    """VALID_PRIORITIES contains the correct enum values."""

    def test_is_tuple(self) -> None:
        assert isinstance(VALID_PRIORITIES, tuple)

    def test_values(self) -> None:
        assert VALID_PRIORITIES == ("critical", "high", "medium", "low")

    def test_length(self) -> None:
        assert len(VALID_PRIORITIES) == 4


class TestValidUrgencies:
    """VALID_URGENCIES contains the correct enum values."""

    def test_is_tuple(self) -> None:
        assert isinstance(VALID_URGENCIES, tuple)

    def test_values(self) -> None:
        assert VALID_URGENCIES == ("immediate", "high", "normal", "low")

    def test_length(self) -> None:
        assert len(VALID_URGENCIES) == 4
