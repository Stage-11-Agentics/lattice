"""CLI integration tests for `lattice next`."""

from __future__ import annotations

import json


class TestNextBasic:
    """Basic next command behavior."""

    def test_no_tasks_returns_empty(self, invoke) -> None:
        result = invoke("next")
        assert result.exit_code == 0
        assert "No tasks available" in result.output

    def test_no_tasks_json_returns_null(self, invoke) -> None:
        result = invoke("next", "--json")
        assert result.exit_code == 0
        parsed = json.loads(result.output)
        assert parsed["ok"] is True
        assert parsed["data"] is None

    def test_no_tasks_quiet_returns_empty(self, invoke) -> None:
        result = invoke("next", "--quiet")
        assert result.exit_code == 0
        assert result.output.strip() == ""

    def test_picks_single_backlog_task(self, create_task, invoke) -> None:
        create_task("My backlog task")
        result = invoke("next")
        assert result.exit_code == 0
        assert "My backlog task" in result.output

    def test_picks_single_task_json(self, create_task, invoke) -> None:
        create_task("JSON task")
        result = invoke("next", "--json")
        assert result.exit_code == 0
        parsed = json.loads(result.output)
        assert parsed["ok"] is True
        assert parsed["data"]["title"] == "JSON task"

    def test_picks_single_task_quiet(self, create_task, invoke) -> None:
        create_task("Quiet task")
        result = invoke("next", "--quiet")
        assert result.exit_code == 0
        # Should print just the task ID
        output = result.output.strip()
        assert output  # Non-empty


class TestNextPriority:
    """Priority-based selection via CLI."""

    def test_critical_beats_medium(self, create_task, invoke) -> None:
        create_task("Medium task")  # default priority is medium
        create_task("Critical task", "--priority", "critical")

        result = invoke("next", "--json")
        parsed = json.loads(result.output)
        assert parsed["data"]["title"] == "Critical task"

    def test_high_beats_low(self, create_task, invoke) -> None:
        create_task("Low task", "--priority", "low")
        create_task("High task", "--priority", "high")

        result = invoke("next", "--json")
        parsed = json.loads(result.output)
        assert parsed["data"]["title"] == "High task"


class TestNextExclusions:
    """Tasks in terminal/blocked states are excluded."""

    def test_excludes_done_task(self, create_task, invoke) -> None:
        task = create_task("Done task")
        task_id = task["id"]
        invoke("status", task_id, "in_planning", "--actor", "human:test")
        invoke("status", task_id, "planned", "--actor", "human:test")
        invoke("status", task_id, "in_progress", "--actor", "human:test")
        invoke("status", task_id, "review", "--actor", "human:test")
        invoke("status", task_id, "done", "--actor", "human:test")

        result = invoke("next", "--json")
        parsed = json.loads(result.output)
        assert parsed["data"] is None

    def test_excludes_cancelled_task(self, create_task, invoke) -> None:
        task = create_task("Cancelled task")
        task_id = task["id"]
        invoke("status", task_id, "cancelled", "--actor", "human:test")

        result = invoke("next", "--json")
        parsed = json.loads(result.output)
        assert parsed["data"] is None


class TestNextAssignment:
    """Assignment-based filtering."""

    def test_excludes_assigned_to_others(self, create_task, invoke) -> None:
        task = create_task("Assigned task")
        task_id = task["id"]
        invoke("assign", task_id, "agent:other", "--actor", "human:test")

        result = invoke("next", "--actor", "agent:claude", "--json")
        parsed = json.loads(result.output)
        assert parsed["data"] is None

    def test_includes_assigned_to_self(self, create_task, invoke) -> None:
        task = create_task("My task")
        task_id = task["id"]
        invoke("assign", task_id, "agent:claude", "--actor", "human:test")

        result = invoke("next", "--actor", "agent:claude", "--json")
        parsed = json.loads(result.output)
        assert parsed["data"]["title"] == "My task"


class TestNextResume:
    """Resume-first logic via CLI."""

    def test_resumes_in_progress_over_backlog(self, create_task, invoke) -> None:
        # Create a backlog task with critical priority
        create_task("Critical backlog", "--priority", "critical")

        # Create a task and move it to in_progress, assign to actor
        task2 = create_task("In progress task", "--priority", "low")
        task2_id = task2["id"]
        invoke("assign", task2_id, "agent:claude", "--actor", "human:test")
        invoke("status", task2_id, "in_planning", "--actor", "human:test")
        invoke("status", task2_id, "planned", "--actor", "human:test")
        invoke("status", task2_id, "in_progress", "--actor", "human:test")

        result = invoke("next", "--actor", "agent:claude", "--json")
        parsed = json.loads(result.output)
        assert parsed["data"]["title"] == "In progress task"


class TestNextStatusOverride:
    """Custom --status flag."""

    def test_status_override_review(self, create_task, invoke) -> None:
        # Create a task in review
        task = create_task("Review task")
        task_id = task["id"]
        invoke("status", task_id, "in_planning", "--actor", "human:test")
        invoke("status", task_id, "planned", "--actor", "human:test")
        invoke("status", task_id, "in_progress", "--actor", "human:test")
        invoke("status", task_id, "review", "--actor", "human:test")

        # Default statuses won't find it
        result = invoke("next", "--json")
        parsed = json.loads(result.output)
        assert parsed["data"] is None

        # But with --status override, it will
        result = invoke("next", "--status", "review", "--json")
        parsed = json.loads(result.output)
        assert parsed["data"]["title"] == "Review task"


class TestNextClaim:
    """--claim flag atomically assigns and moves to in_progress."""

    def test_claim_requires_actor(self, invoke) -> None:
        result = invoke("next", "--claim")
        assert result.exit_code != 0

    def test_claim_assigns_and_starts(self, create_task, invoke) -> None:
        task = create_task("Claimable task")
        task_id = task["id"]

        result = invoke("next", "--actor", "agent:claude", "--claim", "--json")
        assert result.exit_code == 0
        parsed = json.loads(result.output)
        assert parsed["ok"] is True
        assert parsed["data"]["assigned_to"] == "agent:claude"
        assert parsed["data"]["status"] == "in_progress"

        # Verify the task was actually updated on disk
        show_result = invoke("show", task_id, "--json")
        show_parsed = json.loads(show_result.output)
        assert show_parsed["data"]["assigned_to"] == "agent:claude"
        assert show_parsed["data"]["status"] == "in_progress"

    def test_claim_no_task_available(self, invoke) -> None:
        result = invoke("next", "--actor", "agent:claude", "--claim", "--json")
        assert result.exit_code == 0
        parsed = json.loads(result.output)
        assert parsed["data"] is None

    def test_claim_invalid_actor_format(self, invoke) -> None:
        result = invoke("next", "--actor", "badformat", "--claim")
        assert result.exit_code != 0


class TestNextClaimTransitions:
    """--claim emits valid intermediate transitions."""

    def test_claim_planned_task_direct(self, create_task, invoke) -> None:
        """Claiming a planned task should transition planned -> in_progress (1 hop)."""
        task = create_task("Planned task")
        task_id = task["id"]
        invoke("status", task_id, "in_planning", "--actor", "human:test")
        invoke("status", task_id, "planned", "--actor", "human:test")

        result = invoke(
            "next", "--actor", "agent:claude", "--status", "planned", "--claim", "--json"
        )
        assert result.exit_code == 0
        parsed = json.loads(result.output)
        assert parsed["ok"] is True
        assert parsed["data"]["status"] == "in_progress"
        assert parsed["data"]["assigned_to"] == "agent:claude"

    def test_claim_backlog_emits_intermediate_transitions(self, create_task, invoke) -> None:
        """Claiming a backlog task should emit backlog -> planned -> in_progress."""
        task = create_task("Backlog task")
        task_id = task["id"]

        result = invoke("next", "--actor", "agent:claude", "--claim", "--json")
        assert result.exit_code == 0
        parsed = json.loads(result.output)
        assert parsed["data"]["status"] == "in_progress"

        # Verify events show intermediate transitions
        show_result = invoke("show", task_id, "--full", "--json")
        show_parsed = json.loads(show_result.output)
        events = show_parsed["data"].get("events", [])
        status_events = [e for e in events if e["type"] == "status_changed"]
        # Should have at least 2 status changes: backlog->planned, planned->in_progress
        assert len(status_events) >= 2
        assert status_events[-2]["data"]["from"] == "backlog"
        assert status_events[-2]["data"]["to"] == "planned"
        assert status_events[-1]["data"]["from"] == "planned"
        assert status_events[-1]["data"]["to"] == "in_progress"

    def test_claim_already_in_progress_is_noop(self, create_task, invoke) -> None:
        """If resume-first returns an in_progress task, --claim should not error."""
        task = create_task("Active task")
        task_id = task["id"]
        invoke("assign", task_id, "agent:claude", "--actor", "human:test")
        invoke("status", task_id, "in_planning", "--actor", "human:test")
        invoke("status", task_id, "planned", "--actor", "human:test")
        invoke("status", task_id, "in_progress", "--actor", "human:test")

        result = invoke("next", "--actor", "agent:claude", "--claim", "--json")
        assert result.exit_code == 0
        parsed = json.loads(result.output)
        assert parsed["data"]["status"] == "in_progress"
        assert parsed["data"]["assigned_to"] == "agent:claude"

    def test_claim_requires_actor_json_mode(self, invoke) -> None:
        """--claim without --actor should error even in JSON mode."""
        result = invoke("next", "--claim", "--json")
        assert result.exit_code != 0


class TestNextActorValidation:
    """Actor format validation."""

    def test_invalid_actor_format(self, invoke) -> None:
        result = invoke("next", "--actor", "noprefix")
        assert result.exit_code != 0
