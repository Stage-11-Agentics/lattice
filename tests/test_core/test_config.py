"""Tests for core config module."""

from __future__ import annotations

import json

import pytest

from lattice.core.config import (
    VALID_PRIORITIES,
    VALID_URGENCIES,
    default_config,
    get_configured_roles,
    get_wip_limit,
    load_config,
    serialize_config,
    validate_completion_policy,
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
        assert config["task_types"] == ["task", "epic", "bug", "spike", "chore"]

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

    def test_terminal_statuses_have_no_explicit_transitions(self) -> None:
        config = default_config()
        transitions = config["workflow"]["transitions"]
        assert transitions["done"] == []
        assert transitions["cancelled"] == []

    def test_has_universal_targets(self) -> None:
        config = default_config()
        assert "universal_targets" in config["workflow"]
        assert "needs_human" in config["workflow"]["universal_targets"]
        assert "cancelled" in config["workflow"]["universal_targets"]

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

    def test_terminal_status_has_no_explicit_transitions(self) -> None:
        config = default_config()
        # done/cancelled have no explicit transitions, but universal targets
        # (needs_human, cancelled) are still reachable
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

    def test_universal_target_reachable_from_any_status(self) -> None:
        config = default_config()
        all_statuses = config["workflow"]["statuses"]
        universal = config["workflow"]["universal_targets"]
        for target in universal:
            for from_s in all_statuses:
                assert validate_transition(config, from_s, target) is True, (
                    f"Universal target {target!r} should be reachable from {from_s!r}"
                )

    def test_universal_target_backlog_to_needs_human(self) -> None:
        config = default_config()
        assert validate_transition(config, "backlog", "needs_human") is True

    def test_universal_target_done_to_needs_human(self) -> None:
        config = default_config()
        assert validate_transition(config, "done", "needs_human") is True

    def test_universal_target_done_to_cancelled(self) -> None:
        config = default_config()
        assert validate_transition(config, "done", "cancelled") is True

    def test_no_universal_targets_falls_back_to_explicit(self) -> None:
        config: dict = {
            "workflow": {
                "transitions": {"backlog": ["in_planning"]},
            },
        }
        assert validate_transition(config, "backlog", "in_planning") is True
        assert validate_transition(config, "backlog", "needs_human") is False


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


# ---------------------------------------------------------------------------
# validate_completion_policy
# ---------------------------------------------------------------------------


def _snap_with_evidence(evidence_refs: list, assigned_to: str | None = None) -> dict:
    """Build a minimal snapshot dict for policy testing."""
    return {
        "id": "task_01EXAMPLE0000000000000000",
        "evidence_refs": evidence_refs,
        "assigned_to": assigned_to,
    }


class TestValidateCompletionPolicy:
    """validate_completion_policy() checks evidence gating rules."""

    def test_no_policy_always_passes(self) -> None:
        config = default_config()
        snap = _snap_with_evidence([])
        ok, failures = validate_completion_policy(config, snap, "done")
        assert ok is True
        assert failures == []

    def test_missing_required_role_blocked(self) -> None:
        config = default_config()
        config["workflow"]["completion_policies"] = {
            "done": {"require_roles": ["review"]},
        }
        snap = _snap_with_evidence([])
        ok, failures = validate_completion_policy(config, snap, "done")
        assert ok is False
        assert any("review" in f for f in failures)

    def test_has_required_role_via_artifact(self) -> None:
        config = default_config()
        config["workflow"]["completion_policies"] = {
            "done": {"require_roles": ["review"]},
        }
        snap = _snap_with_evidence([
            {"id": "art_A", "role": "review", "source_type": "artifact"},
        ])
        ok, failures = validate_completion_policy(config, snap, "done")
        assert ok is True
        assert failures == []

    def test_has_required_role_via_comment(self) -> None:
        config = default_config()
        config["workflow"]["completion_policies"] = {
            "done": {"require_roles": ["review"]},
        }
        snap = _snap_with_evidence([
            {"id": "ev_C", "role": "review", "source_type": "comment"},
        ])
        ok, failures = validate_completion_policy(config, snap, "done")
        assert ok is True
        assert failures == []

    def test_multiple_required_roles_partial_blocked(self) -> None:
        config = default_config()
        config["workflow"]["completion_policies"] = {
            "done": {"require_roles": ["review", "security"]},
        }
        snap = _snap_with_evidence([
            {"id": "art_A", "role": "review", "source_type": "artifact"},
        ])
        ok, failures = validate_completion_policy(config, snap, "done")
        assert ok is False
        assert any("security" in f for f in failures)
        assert not any("review" in f for f in failures)

    def test_multiple_required_roles_all_present_passes(self) -> None:
        config = default_config()
        config["workflow"]["completion_policies"] = {
            "done": {"require_roles": ["review", "security"]},
        }
        snap = _snap_with_evidence([
            {"id": "art_A", "role": "review", "source_type": "artifact"},
            {"id": "ev_B", "role": "security", "source_type": "comment"},
        ])
        ok, failures = validate_completion_policy(config, snap, "done")
        assert ok is True

    def test_require_assigned_blocked_when_unassigned(self) -> None:
        config = default_config()
        config["workflow"]["completion_policies"] = {
            "done": {"require_assigned": True},
        }
        snap = _snap_with_evidence([], assigned_to=None)
        ok, failures = validate_completion_policy(config, snap, "done")
        assert ok is False
        assert any("assigned" in f.lower() for f in failures)

    def test_require_assigned_passes_when_assigned(self) -> None:
        config = default_config()
        config["workflow"]["completion_policies"] = {
            "done": {"require_assigned": True},
        }
        snap = _snap_with_evidence([], assigned_to="agent:claude")
        ok, failures = validate_completion_policy(config, snap, "done")
        assert ok is True

    def test_universal_target_bypasses_policy(self) -> None:
        config = default_config()
        config["workflow"]["completion_policies"] = {
            "needs_human": {"require_roles": ["review"]},
        }
        snap = _snap_with_evidence([])
        ok, failures = validate_completion_policy(config, snap, "needs_human")
        assert ok is True

    def test_cancelled_bypasses_policy(self) -> None:
        config = default_config()
        config["workflow"]["completion_policies"] = {
            "cancelled": {"require_roles": ["review"]},
        }
        snap = _snap_with_evidence([])
        ok, failures = validate_completion_policy(config, snap, "cancelled")
        assert ok is True

    def test_policy_for_different_status_not_applied(self) -> None:
        config = default_config()
        config["workflow"]["completion_policies"] = {
            "done": {"require_roles": ["review"]},
        }
        snap = _snap_with_evidence([])
        ok, failures = validate_completion_policy(config, snap, "review")
        assert ok is True

    def test_no_role_evidence_fails(self) -> None:
        """Evidence refs without a role should not satisfy require_roles."""
        config = default_config()
        config["workflow"]["completion_policies"] = {
            "done": {"require_roles": ["review"]},
        }
        snap = _snap_with_evidence([
            {"id": "art_A", "role": None, "source_type": "artifact"},
        ])
        ok, failures = validate_completion_policy(config, snap, "done")
        assert ok is False


# ---------------------------------------------------------------------------
# get_configured_roles (LAT-137, LAT-151)
# ---------------------------------------------------------------------------


class TestGetConfiguredRoles:
    def test_default_config_includes_explicit_roles(self) -> None:
        """Default config defines workflow.roles: ['review']."""
        config = default_config()
        assert get_configured_roles(config) == {"review"}

    def test_no_roles_no_policies_returns_empty(self) -> None:
        """Config with neither workflow.roles nor completion policies → empty set."""
        config = default_config()
        config["workflow"].pop("roles", None)
        assert get_configured_roles(config) == set()

    def test_explicit_roles_without_policies(self) -> None:
        """workflow.roles alone (no completion policies) is sufficient."""
        config = default_config()
        config["workflow"]["roles"] = ["review", "qa"]
        assert get_configured_roles(config) == {"review", "qa"}

    def test_explicit_roles_merged_with_policy_roles(self) -> None:
        """Union of workflow.roles and completion policy require_roles."""
        config = default_config()
        config["workflow"]["roles"] = ["qa"]
        config["workflow"]["completion_policies"] = {
            "done": {"require_roles": ["review"]},
        }
        assert get_configured_roles(config) == {"review", "qa"}

    def test_single_policy_single_role(self) -> None:
        config = default_config()
        config["workflow"].pop("roles", None)
        config["workflow"]["completion_policies"] = {
            "done": {"require_roles": ["review"]},
        }
        assert get_configured_roles(config) == {"review"}

    def test_multiple_policies_multiple_roles(self) -> None:
        config = default_config()
        config["workflow"].pop("roles", None)
        config["workflow"]["completion_policies"] = {
            "done": {"require_roles": ["review", "sign_off"]},
            "review": {"require_roles": ["triage"]},
        }
        assert get_configured_roles(config) == {"review", "sign_off", "triage"}

    def test_empty_require_roles_with_no_explicit(self) -> None:
        config = default_config()
        config["workflow"].pop("roles", None)
        config["workflow"]["completion_policies"] = {
            "done": {"require_roles": []},
        }
        assert get_configured_roles(config) == set()

    def test_policy_without_require_roles_with_no_explicit(self) -> None:
        config = default_config()
        config["workflow"].pop("roles", None)
        config["workflow"]["completion_policies"] = {
            "done": {"require_assigned": True},
        }
        assert get_configured_roles(config) == set()


# ---------------------------------------------------------------------------
# Workflow invariants (parametrized)
# ---------------------------------------------------------------------------

_DEFAULT_CONFIG = default_config()
_ALL_STATUSES = _DEFAULT_CONFIG["workflow"]["statuses"]
_TRANSITIONS = _DEFAULT_CONFIG["workflow"]["transitions"]
_UNIVERSAL_TARGETS = _DEFAULT_CONFIG["workflow"]["universal_targets"]
_TERMINAL_STATUSES = {"done", "cancelled"}


class TestWorkflowInvariants:
    """Parametrized tests ensuring the default workflow is internally consistent."""

    @pytest.mark.parametrize("status", _ALL_STATUSES)
    def test_every_status_has_transition_entry(self, status: str) -> None:
        """Every defined status has an entry in the transitions dict."""
        assert status in _TRANSITIONS, f"Status '{status}' missing from transitions"

    @pytest.mark.parametrize("status", _ALL_STATUSES)
    def test_every_status_is_reachable_or_initial(self, status: str) -> None:
        """Every status is either the default, a universal target, or reachable
        from at least one other status via explicit transitions."""
        if status == _DEFAULT_CONFIG["default_status"]:
            return  # initial status is reachable by creation
        if status in _UNIVERSAL_TARGETS:
            return  # universal targets are reachable from any status
        # Check if any other status can transition to this one
        reachable_from = [
            src for src, targets in _TRANSITIONS.items() if status in targets
        ]
        assert reachable_from, f"Status '{status}' is unreachable — no status transitions to it"

    @pytest.mark.parametrize("status", _ALL_STATUSES)
    def test_non_terminal_statuses_have_outbound_transitions(self, status: str) -> None:
        """Non-terminal statuses must have at least one outbound transition
        (explicit or via universal targets)."""
        if status in _TERMINAL_STATUSES:
            return  # terminal statuses are allowed to have empty transitions
        explicit = _TRANSITIONS.get(status, [])
        assert explicit or _UNIVERSAL_TARGETS, (
            f"Status '{status}' has no outbound transitions and no universal targets"
        )

    @pytest.mark.parametrize("status", _ALL_STATUSES)
    def test_transition_targets_are_valid_statuses(self, status: str) -> None:
        """Every transition target must be a defined status."""
        for target in _TRANSITIONS.get(status, []):
            assert target in _ALL_STATUSES, (
                f"Transition from '{status}' targets undefined status '{target}'"
            )
