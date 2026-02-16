"""Unit tests for lattice.core.next — pure selection logic."""

from __future__ import annotations

from lattice.core.next import select_next


def _snap(
    task_id: str = "task_01",
    status: str = "backlog",
    priority: str = "medium",
    urgency: str = "normal",
    assigned_to: str | None = None,
    **extra: object,
) -> dict:
    """Build a minimal snapshot dict for testing."""
    return {
        "id": task_id,
        "status": status,
        "priority": priority,
        "urgency": urgency,
        "assigned_to": assigned_to,
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
            _snap(
                "task_plan", status="in_planning", priority="low", assigned_to="agent:claude"
            ),
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
