"""Unit tests for lattice.core.next — pure selection logic."""

from __future__ import annotations

from lattice.core.next import (
    _actors_match,
    compute_claim_transitions,
    select_all_ready,
    select_next,
)


def _snap(
    task_id: str = "task_01",
    status: str = "backlog",
    priority: str = "medium",
    urgency: str = "normal",
    assigned_to: str | None = None,
    type: str = "task",
    **extra: object,
) -> dict:
    """Build a minimal snapshot dict for testing."""
    return {
        "id": task_id,
        "status": status,
        "priority": priority,
        "urgency": urgency,
        "assigned_to": assigned_to,
        "type": type,
        "title": f"Task {task_id}",
        **extra,
    }


class TestSelectNextEmpty:
    """Edge case: empty input."""

    def test_empty_list_returns_none(self) -> None:
        assert select_next([]) is None

    def test_empty_list_with_actor_returns_none(self) -> None:
        assert select_next([], actor="agent:claude") is None


class TestSelectNextPriorityOrdering:
    """Priority ordering: critical > high > medium > low."""

    def test_critical_beats_high(self) -> None:
        snaps = [
            _snap("task_high", priority="high"),
            _snap("task_crit", priority="critical"),
        ]
        result = select_next(snaps)
        assert result is not None
        assert result["id"] == "task_crit"

    def test_high_beats_medium(self) -> None:
        snaps = [
            _snap("task_med", priority="medium"),
            _snap("task_high", priority="high"),
        ]
        result = select_next(snaps)
        assert result is not None
        assert result["id"] == "task_high"

    def test_medium_beats_low(self) -> None:
        snaps = [
            _snap("task_low", priority="low"),
            _snap("task_med", priority="medium"),
        ]
        result = select_next(snaps)
        assert result is not None
        assert result["id"] == "task_med"

    def test_full_priority_order(self) -> None:
        snaps = [
            _snap("task_low", priority="low"),
            _snap("task_med", priority="medium"),
            _snap("task_high", priority="high"),
            _snap("task_crit", priority="critical"),
        ]
        result = select_next(snaps)
        assert result is not None
        assert result["id"] == "task_crit"


class TestSelectNextUrgencyBreaksTie:
    """Urgency breaks priority ties."""

    def test_immediate_beats_high(self) -> None:
        snaps = [
            _snap("task_high_urg", urgency="high"),
            _snap("task_imm_urg", urgency="immediate"),
        ]
        result = select_next(snaps)
        assert result is not None
        assert result["id"] == "task_imm_urg"

    def test_urgency_breaks_same_priority(self) -> None:
        snaps = [
            _snap("task_normal", priority="high", urgency="normal"),
            _snap("task_imm", priority="high", urgency="immediate"),
        ]
        result = select_next(snaps)
        assert result is not None
        assert result["id"] == "task_imm"


class TestSelectNextIdBreaksTie:
    """Oldest ULID wins when priority and urgency are equal."""

    def test_older_id_wins(self) -> None:
        snaps = [
            _snap("task_02BBB"),  # newer
            _snap("task_01AAA"),  # older
        ]
        result = select_next(snaps)
        assert result is not None
        assert result["id"] == "task_01AAA"


class TestSelectNextExclusions:
    """Excluded statuses are never selected."""

    def test_excludes_done(self) -> None:
        snaps = [_snap("task_done", status="done")]
        assert select_next(snaps) is None

    def test_excludes_cancelled(self) -> None:
        snaps = [_snap("task_cancel", status="cancelled")]
        assert select_next(snaps) is None

    def test_excludes_blocked(self) -> None:
        snaps = [_snap("task_block", status="blocked")]
        assert select_next(snaps) is None

    def test_excludes_needs_human(self) -> None:
        snaps = [_snap("task_human", status="needs_human")]
        assert select_next(snaps) is None

    def test_excludes_in_progress_without_actor(self) -> None:
        """in_progress is not in ready_statuses by default."""
        snaps = [_snap("task_ip", status="in_progress")]
        assert select_next(snaps) is None


class TestSelectNextAssignment:
    """Assignment-based filtering."""

    def test_excludes_assigned_to_others(self) -> None:
        snaps = [
            _snap("task_other", assigned_to="agent:other"),
        ]
        result = select_next(snaps, actor="agent:claude")
        assert result is None

    def test_includes_assigned_to_self(self) -> None:
        snaps = [
            _snap("task_mine", assigned_to="agent:claude"),
        ]
        result = select_next(snaps, actor="agent:claude")
        assert result is not None
        assert result["id"] == "task_mine"

    def test_includes_unassigned(self) -> None:
        snaps = [
            _snap("task_free", assigned_to=None),
        ]
        result = select_next(snaps, actor="agent:claude")
        assert result is not None
        assert result["id"] == "task_free"

    def test_no_actor_excludes_assigned(self) -> None:
        """When no actor is specified, assigned tasks are excluded."""
        snaps = [
            _snap("task_assigned", assigned_to="agent:other"),
        ]
        result = select_next(snaps)
        assert result is None

    def test_no_actor_includes_unassigned(self) -> None:
        snaps = [
            _snap("task_free", assigned_to=None),
        ]
        result = select_next(snaps)
        assert result is not None


class TestSelectNextResume:
    """Resume-first logic: in_progress/in_planning tasks assigned to actor."""

    def test_resumes_in_progress_over_backlog(self) -> None:
        snaps = [
            _snap("task_backlog", status="backlog", priority="critical"),
            _snap("task_ip", status="in_progress", priority="low", assigned_to="agent:claude"),
        ]
        result = select_next(snaps, actor="agent:claude")
        assert result is not None
        assert result["id"] == "task_ip"

    def test_resumes_in_planning_over_backlog(self) -> None:
        snaps = [
            _snap("task_backlog", status="backlog", priority="critical"),
            _snap("task_plan", status="in_planning", priority="low", assigned_to="agent:claude"),
        ]
        result = select_next(snaps, actor="agent:claude")
        assert result is not None
        assert result["id"] == "task_plan"

    def test_does_not_resume_others_in_progress(self) -> None:
        """Don't resume in_progress tasks assigned to someone else."""
        snaps = [
            _snap("task_backlog", status="backlog"),
            _snap("task_ip", status="in_progress", assigned_to="agent:other"),
        ]
        result = select_next(snaps, actor="agent:claude")
        assert result is not None
        assert result["id"] == "task_backlog"

    def test_no_actor_skips_resume(self) -> None:
        """Resume logic only applies when actor is specified."""
        snaps = [
            _snap("task_backlog", status="backlog"),
            _snap("task_ip", status="in_progress", assigned_to="agent:claude"),
        ]
        result = select_next(snaps)
        assert result is not None
        assert result["id"] == "task_backlog"

    def test_resume_picks_highest_priority(self) -> None:
        snaps = [
            _snap("task_ip_low", status="in_progress", priority="low", assigned_to="agent:claude"),
            _snap(
                "task_ip_high",
                status="in_progress",
                priority="high",
                assigned_to="agent:claude",
            ),
        ]
        result = select_next(snaps, actor="agent:claude")
        assert result is not None
        assert result["id"] == "task_ip_high"


class TestSelectNextCustomStatuses:
    """Custom ready_statuses override."""

    def test_custom_ready_statuses(self) -> None:
        snaps = [
            _snap("task_backlog", status="backlog"),
            _snap("task_review", status="review"),
        ]
        result = select_next(snaps, ready_statuses=frozenset({"review"}))
        assert result is not None
        assert result["id"] == "task_review"

    def test_custom_statuses_excludes_default(self) -> None:
        snaps = [
            _snap("task_backlog", status="backlog"),
        ]
        result = select_next(snaps, ready_statuses=frozenset({"planned"}))
        assert result is None


class TestSelectNextPlanned:
    """Planned tasks are in the default ready pool."""

    def test_includes_planned(self) -> None:
        snaps = [_snap("task_planned", status="planned")]
        result = select_next(snaps)
        assert result is not None
        assert result["id"] == "task_planned"


class TestSelectAllReady:
    """select_all_ready returns full sorted list for display."""

    def test_returns_all_ready_sorted(self) -> None:
        snaps = [
            _snap("task_low", priority="low"),
            _snap("task_crit", priority="critical"),
            _snap("task_med", priority="medium"),
        ]
        result = select_all_ready(snaps)
        assert len(result) == 3
        assert result[0]["id"] == "task_crit"
        assert result[1]["id"] == "task_med"
        assert result[2]["id"] == "task_low"

    def test_excludes_non_ready_statuses(self) -> None:
        snaps = [
            _snap("task_backlog", status="backlog"),
            _snap("task_done", status="done"),
            _snap("task_ip", status="in_progress"),
        ]
        result = select_all_ready(snaps)
        assert len(result) == 1
        assert result[0]["id"] == "task_backlog"

    def test_includes_assigned_tasks(self) -> None:
        """Unlike select_next, select_all_ready does not filter by assignment."""
        snaps = [
            _snap("task_assigned", assigned_to="agent:other"),
            _snap("task_free"),
        ]
        result = select_all_ready(snaps)
        assert len(result) == 2

    def test_empty_returns_empty(self) -> None:
        assert select_all_ready([]) == []

    def test_needs_human_excluded(self) -> None:
        snaps = [
            _snap("task_nh", status="needs_human"),
            _snap("task_bl", status="backlog"),
        ]
        result = select_all_ready(snaps)
        assert len(result) == 1
        assert result[0]["id"] == "task_bl"

    def test_epics_excluded(self) -> None:
        """Epics should never appear in the ready list."""
        snaps = [
            _snap("task_epic", status="backlog", type="epic"),
            _snap("task_normal", status="backlog"),
        ]
        result = select_all_ready(snaps)
        assert len(result) == 1
        assert result[0]["id"] == "task_normal"


class TestSelectNextEpicExclusion:
    """Epics are containers, not actionable work — always excluded from next."""

    def test_epic_excluded_from_next(self) -> None:
        snaps = [
            _snap("task_epic", status="backlog", type="epic"),
            _snap("task_normal", status="backlog", priority="low"),
        ]
        result = select_next(snaps)
        assert result is not None
        assert result["id"] == "task_normal"

    def test_only_epics_returns_none(self) -> None:
        snaps = [_snap("task_epic", status="backlog", type="epic")]
        result = select_next(snaps)
        assert result is None

    def test_epic_excluded_from_resume(self) -> None:
        snaps = [
            _snap("task_epic", status="in_progress", type="epic", assigned_to="agent:claude"),
            _snap("task_normal", status="backlog"),
        ]
        result = select_next(snaps, actor="agent:claude")
        assert result is not None
        assert result["id"] == "task_normal"


class TestComputeClaimTransitions:
    """BFS transition path computation."""

    def test_same_status_returns_empty(self) -> None:
        transitions = {"in_progress": ["review"]}
        assert compute_claim_transitions("in_progress", "in_progress", transitions) == []

    def test_direct_transition(self) -> None:
        transitions = {"planned": ["in_progress"]}
        result = compute_claim_transitions("planned", "in_progress", transitions)
        assert result == ["in_progress"]

    def test_two_hop_transition(self) -> None:
        """backlog -> planned -> in_progress requires 2 hops."""
        transitions = {
            "backlog": ["in_planning", "planned", "cancelled"],
            "planned": ["in_progress", "review"],
        }
        result = compute_claim_transitions("backlog", "in_progress", transitions)
        assert result == ["planned", "in_progress"]

    def test_three_hop_transition(self) -> None:
        """backlog -> in_planning -> planned -> in_progress requires 3 hops."""
        transitions = {
            "backlog": ["in_planning"],
            "in_planning": ["planned"],
            "planned": ["in_progress"],
        }
        result = compute_claim_transitions("backlog", "in_progress", transitions)
        assert result == ["in_planning", "planned", "in_progress"]

    def test_no_path_returns_none(self) -> None:
        transitions = {
            "done": [],
            "cancelled": [],
        }
        assert compute_claim_transitions("done", "in_progress", transitions) is None

    def test_exceeds_max_depth_returns_none(self) -> None:
        """More than 3 hops is not allowed."""
        transitions = {
            "a": ["b"],
            "b": ["c"],
            "c": ["d"],
            "d": ["target"],
        }
        assert compute_claim_transitions("a", "target", transitions) is None

    def test_shortest_path_preferred(self) -> None:
        """When multiple paths exist, BFS finds the shortest."""
        transitions = {
            "backlog": ["in_planning", "planned"],
            "in_planning": ["planned"],
            "planned": ["in_progress"],
        }
        # Direct: backlog -> planned -> in_progress (2 hops)
        # Longer: backlog -> in_planning -> planned -> in_progress (3 hops)
        result = compute_claim_transitions("backlog", "in_progress", transitions)
        assert result == ["planned", "in_progress"]

    def test_unknown_status_returns_none(self) -> None:
        transitions = {"backlog": ["planned"]}
        assert compute_claim_transitions("nonexistent", "in_progress", transitions) is None

    def test_with_full_default_config(self) -> None:
        """Verify against the actual default workflow transitions."""
        transitions = {
            "backlog": ["in_planning", "planned", "cancelled"],
            "in_planning": ["planned", "needs_human", "cancelled"],
            "planned": ["in_progress", "review", "blocked", "needs_human", "cancelled"],
            "in_progress": ["review", "blocked", "needs_human", "cancelled"],
            "review": ["done", "in_progress", "needs_human", "cancelled"],
            "done": [],
            "blocked": ["in_planning", "planned", "in_progress", "cancelled"],
            "needs_human": ["in_planning", "planned", "in_progress", "review", "cancelled"],
            "cancelled": [],
        }
        # backlog -> planned -> in_progress (2 hops)
        result = compute_claim_transitions("backlog", "in_progress", transitions)
        assert result == ["planned", "in_progress"]

        # planned -> in_progress (1 hop)
        result = compute_claim_transitions("planned", "in_progress", transitions)
        assert result == ["in_progress"]

        # done -> in_progress (no path)
        assert compute_claim_transitions("done", "in_progress", transitions) is None

        # cancelled -> in_progress (no path)
        assert compute_claim_transitions("cancelled", "in_progress", transitions) is None


# ---------------------------------------------------------------------------
# Structured actor matching
# ---------------------------------------------------------------------------


class TestActorsMatch:
    """_actors_match handles both legacy strings and structured dicts."""

    def test_both_none(self) -> None:
        assert _actors_match(None, None)

    def test_one_none(self) -> None:
        assert not _actors_match("agent:claude", None)
        assert not _actors_match(None, "agent:claude")

    def test_legacy_strings_match(self) -> None:
        assert _actors_match("agent:claude", "agent:claude")

    def test_legacy_strings_no_match(self) -> None:
        assert not _actors_match("agent:claude", "agent:codex")

    def test_structured_dicts_match(self) -> None:
        a = {"name": "Argus-3", "base_name": "Argus", "serial": 3}
        b = {"name": "Argus-3", "base_name": "Argus", "serial": 3}
        assert _actors_match(a, b)

    def test_structured_dicts_no_match(self) -> None:
        a = {"name": "Argus-3", "base_name": "Argus", "serial": 3}
        b = {"name": "Beacon-1", "base_name": "Beacon", "serial": 1}
        assert not _actors_match(a, b)

    def test_mixed_no_match(self) -> None:
        """Legacy string vs structured dict with different names."""
        assert not _actors_match("agent:claude", {"name": "Argus-3"})


class TestSelectNextStructuredActor:
    """Resume and assignment with structured actor dicts."""

    def test_resume_with_structured_actor(self) -> None:
        actor_dict = {"name": "Argus-3", "base_name": "Argus", "serial": 3}
        snaps = [
            _snap("task_backlog", status="backlog", priority="critical"),
            _snap("task_ip", status="in_progress", priority="low", assigned_to=actor_dict),
        ]
        result = select_next(snaps, actor=actor_dict)
        assert result is not None
        assert result["id"] == "task_ip"

    def test_does_not_resume_different_structured_actor(self) -> None:
        assigned = {"name": "Argus-3", "base_name": "Argus", "serial": 3}
        requesting = {"name": "Beacon-1", "base_name": "Beacon", "serial": 1}
        snaps = [
            _snap("task_backlog", status="backlog"),
            _snap("task_ip", status="in_progress", assigned_to=assigned),
        ]
        result = select_next(snaps, actor=requesting)
        assert result is not None
        assert result["id"] == "task_backlog"

    def test_excludes_tasks_assigned_to_other_structured_actor(self) -> None:
        other = {"name": "Beacon-1", "base_name": "Beacon", "serial": 1}
        me = {"name": "Argus-3", "base_name": "Argus", "serial": 3}
        snaps = [_snap("task_other", assigned_to=other)]
        assert select_next(snaps, actor=me) is None
