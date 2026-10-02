"""CLI integration tests for `lattice next`."""

from __future__ import annotations

import json
from pathlib import Path


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

    def test_excludes_done_task(self, create_task, invoke, fill_plan) -> None:
        task = create_task("Done task")
        task_id = task["id"]
        invoke("status", task_id, "in_planning", "--actor", "human:test")
        fill_plan(task_id, "Done task")
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

    def test_resumes_in_progress_over_backlog(self, create_task, invoke, fill_plan) -> None:
        # Create a backlog task with critical priority
        create_task("Critical backlog", "--priority", "critical")

        # Create a task and move it to in_progress, assign to actor
        task2 = create_task("In progress task", "--priority", "low")
        task2_id = task2["id"]
        invoke("assign", task2_id, "agent:claude", "--actor", "human:test")
        invoke("status", task2_id, "in_planning", "--actor", "human:test")
        fill_plan(task2_id, "In progress task")
        invoke("status", task2_id, "planned", "--actor", "human:test")
        invoke("status", task2_id, "in_progress", "--actor", "human:test")

        result = invoke("next", "--actor", "agent:claude", "--json")
        parsed = json.loads(result.output)
        assert parsed["data"]["title"] == "In progress task"


class TestNextStatusOverride:
    """Custom --status flag."""

    def test_status_override_review(self, create_task, invoke, fill_plan) -> None:
        # Create a task in review
        task = create_task("Review task")
        task_id = task["id"]
        invoke("status", task_id, "in_planning", "--actor", "human:test")
        fill_plan(task_id, "Review task")
        invoke("status", task_id, "planned", "--actor", "human:test")
        invoke("status", task_id, "in_progress", "--actor", "human:test")
        invoke("status", task_id, "review", "--actor", "human:test")

        # Default statuses won't find it
        result = invoke("next", "--json")
        parsed = json.loads(result.output)
        assert parsed["data"] is None

        # But with --status override, it will (pass --actor since task is auto-assigned)
        result = invoke("next", "--status", "review", "--actor", "human:test", "--json")
        parsed = json.loads(result.output)
        assert parsed["data"]["title"] == "Review task"


class TestNextClaim:
    """--claim atomically assigns and respects the explicit planning gate."""

    def test_claim_requires_actor(self, invoke) -> None:
        result = invoke("next", "--claim")
        assert result.exit_code != 0

    def test_claim_assigns_and_stops_in_planning(self, create_task, invoke, fill_plan) -> None:
        task = create_task("Claimable task")
        task_id = task["id"]
        fill_plan(task_id, "Claimable task")

        result = invoke("next", "--actor", "agent:claude", "--claim", "--json")
        assert result.exit_code == 0
        parsed = json.loads(result.output)
        assert parsed["ok"] is True
        assert parsed["data"]["assigned_to"] == "agent:claude"
        assert parsed["data"]["status"] == "in_planning"
        label = parsed["data"].get("short_id") or task_id
        assert parsed["data"]["next_steps"]["command"] == f"lattice status {label} planned"

        # Verify the task was actually updated on disk
        show_result = invoke("show", task_id, "--json")
        show_parsed = json.loads(show_result.output)
        assert show_parsed["data"]["assigned_to"] == "agent:claude"
        assert show_parsed["data"]["status"] == "in_planning"

    def test_claim_no_task_available(self, invoke) -> None:
        result = invoke("next", "--actor", "agent:claude", "--claim", "--json")
        assert result.exit_code == 0
        parsed = json.loads(result.output)
        assert parsed["data"] is None

    def test_claim_invalid_actor_format(self, invoke) -> None:
        result = invoke("next", "--actor", "badformat", "--claim")
        assert result.exit_code != 0

    def test_claim_json_includes_plan_content_when_non_scaffold(
        self, create_task, invoke, cli_env
    ) -> None:
        task = create_task("Plan content task")
        task_id = task["id"]
        plan_path = Path(cli_env["LATTICE_ROOT"]) / ".lattice" / "plans" / f"{task_id}.md"
        plan_path.write_text(
            f"# {task_id}\n\n"
            "## Summary\n\n"
            "Useful summary.\n\n"
            "## Technical Plan\n\n"
            "- Implement behavior\n\n"
            "## Acceptance Criteria\n\n"
            "- Includes plan content in next JSON\n"
        )

        result = invoke("next", "--actor", "agent:claude", "--claim", "--json")
        assert result.exit_code == 0
        parsed = json.loads(result.output)
        assert parsed["data"]["id"] == task_id
        assert parsed["data"]["plan_content"] is not None
        assert "Implement behavior" in parsed["data"]["plan_content"]
        assert parsed["data"]["status"] == "in_planning"
        label = parsed["data"].get("short_id") or task_id
        assert parsed["data"]["next_steps"] == {
            "action": "move_to_planned",
            "command": f"lattice status {label} planned",
            "plan_path": f"plans/{task_id}.md",
            "then": "planned",
        }

    def test_claim_with_missing_plan_stops_and_names_steps(
        self, create_task, invoke, cli_env
    ) -> None:
        task = create_task("No plan file task")
        task_id = task["id"]
        plan_path = Path(cli_env["LATTICE_ROOT"]) / ".lattice" / "plans" / f"{task_id}.md"
        if plan_path.exists():
            plan_path.unlink()

        result = invoke("next", "--actor", "agent:claude", "--claim", "--json")
        assert result.exit_code == 0
        parsed = json.loads(result.output)
        assert parsed["data"]["status"] == "in_planning"
        assert parsed["data"]["plan_content"] is None
        label = parsed["data"].get("short_id") or task_id
        assert parsed["data"]["next_steps"] == {
            "action": "write_plan",
            "command": f"lattice status {label} planned",
            "plan_path": f"plans/{task_id}.md",
            "then": "planned",
        }

    def test_claim_with_scaffold_plan_prints_assignment_and_plan_gate(
        self, create_task, invoke
    ) -> None:
        task = create_task("Scaffold plan task")
        label = task.get("short_id") or task["id"]
        # The plan is auto-scaffolded on create — the current hint stays verbatim.
        result = invoke("next", "--actor", "agent:claude", "--claim")
        assert result.exit_code == 0
        assert result.output.startswith(f"{label}  in_planning")
        assert "Assigned to agent:claude." in result.output
        assert "Next: write the plan in plans/" in result.output
        assert (
            f"Next: run 'lattice status {label} planned' after writing the plan." in result.output
        )

        quiet = invoke("next", "--actor", "agent:claude", "--claim", "--quiet")
        assert quiet.output.strip() == label

    def test_claim_with_existing_plan_prints_planned_command(
        self, create_task, invoke, fill_plan
    ) -> None:
        task = create_task("Existing substantive plan")
        task_id = task["id"]
        label = task.get("short_id") or task_id
        fill_plan(task_id, "Existing substantive plan")

        result = invoke("next", "--actor", "agent:claude", "--claim")

        assert result.exit_code == 0
        assert result.output.startswith(f"{label}  in_planning")
        assert "Assigned to agent:claude." in result.output
        assert (
            f"Next: run 'lattice status {label} planned' to enter planned and follow its review hint."
            in result.output
        )
        assert "Next: write the plan in plans/" not in result.output

    def test_described_scaffold_is_null_only_for_claim_json(self, create_task, invoke) -> None:
        create_task("Description scaffold", "--description", "Details from the task.")
        plain = invoke("next", "--json")
        assert json.loads(plain.output)["data"]["plan_content"] is not None
        assert "Details from the task." in json.loads(plain.output)["data"]["plan_content"]

        claimed = invoke("next", "--actor", "agent:claude", "--claim", "--json")
        data = json.loads(claimed.output)["data"]
        assert data["status"] == "in_planning"
        assert data["plan_content"] is None
        assert data["next_steps"]["action"] == "write_plan"

    def test_claim_plan_gate_refusal_keeps_no_write_suffix(self, create_task, invoke) -> None:
        task = create_task("Planned scaffold")
        task_id = task["id"]
        invoke("status", task_id, "in_planning", "--actor", "human:test")
        invoke("status", task_id, "planned", "--actor", "human:test")
        invoke("assign", task_id, "none", "--actor", "human:test")
        before = json.loads(invoke("show", task_id, "--full", "--json").output)["data"]["events"]

        refused = invoke(
            "next", "--status", "planned", "--actor", "agent:claude", "--claim", "--json"
        )
        parsed = json.loads(refused.output)
        assert parsed["error"]["code"] == "PLAN_REQUIRED"
        assert parsed["error"]["message"].endswith("No assignment or status change was made.")
        after = json.loads(invoke("show", task_id, "--full", "--json").output)["data"]["events"]
        assert after == before


class TestNextClaimTransitions:
    """--claim emits valid intermediate transitions."""

    def test_claim_planned_task_direct(self, create_task, invoke, fill_plan) -> None:
        """Claiming a planned task should transition planned -> in_progress (1 hop)."""
        task = create_task("Planned task")
        task_id = task["id"]
        invoke("status", task_id, "in_planning", "--actor", "human:test")
        fill_plan(task_id, "Planned task")
        invoke("status", task_id, "planned", "--actor", "human:test")
        # Unassign so a different actor can claim it
        invoke("assign", task_id, "none", "--actor", "human:test")

        result = invoke(
            "next", "--actor", "agent:claude", "--status", "planned", "--claim", "--json"
        )
        assert result.exit_code == 0
        parsed = json.loads(result.output)
        assert parsed["ok"] is True
        assert parsed["data"]["status"] == "in_progress"
        assert parsed["data"]["assigned_to"] == "agent:claude"

    def test_claim_backlog_emits_intermediate_transitions(
        self, create_task, invoke, fill_plan
    ) -> None:
        """A plan-review workflow must stop before its explicit planned gate."""
        task = create_task("Backlog task")
        task_id = task["id"]
        fill_plan(task_id, "Backlog task")

        result = invoke("next", "--actor", "agent:claude", "--claim", "--json")
        assert result.exit_code == 0
        parsed = json.loads(result.output)
        assert parsed["data"]["status"] == "in_planning"

        # Verify events show intermediate transitions
        show_result = invoke("show", task_id, "--full", "--json")
        show_parsed = json.loads(show_result.output)
        events = show_parsed["data"].get("events", [])
        status_events = [e for e in events if e["type"] == "status_changed"]
        assert status_events[-1]["data"] == {"from": "backlog", "to": "in_planning"}

    def test_claim_already_in_progress_is_noop(self, create_task, invoke, fill_plan) -> None:
        """If resume-first returns an in_progress task, --claim should not error."""
        task = create_task("Active task")
        task_id = task["id"]
        invoke("assign", task_id, "agent:claude", "--actor", "human:test")
        invoke("status", task_id, "in_planning", "--actor", "human:test")
        fill_plan(task_id, "Active task")
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


class TestNextWithSessionName:
    """--name flag resolves session identity for next/claim."""

    def test_claim_with_name(self, create_task, invoke, fill_plan) -> None:
        """--name resolves to structured actor for claim."""
        # Start a session first
        result = invoke(
            "session",
            "start",
            "--name",
            "Argus",
            "--model",
            "claude-opus-4",
            "--framework",
            "claude-code",
        )
        assert result.exit_code == 0

        task = create_task("Session claimable task")
        fill_plan(task["id"], "Session claimable task")
        result = invoke("next", "--name", "Argus-1", "--claim", "--json")
        assert result.exit_code == 0
        parsed = json.loads(result.output)
        assert parsed["ok"] is True
        assert parsed["data"]["status"] == "in_planning"
        label = task.get("short_id") or task["id"]
        assert parsed["data"]["next_steps"]["command"] == f"lattice status {label} planned"
        assert "--name" not in parsed["data"]["next_steps"]["command"]
        # assigned_to should be a structured dict with name
        assigned = parsed["data"]["assigned_to"]
        assert isinstance(assigned, dict)
        assert assigned["name"] == "Argus-1"

    def test_claim_requires_name_or_actor(self, invoke) -> None:
        """--claim without --name or --actor should error."""
        result = invoke("next", "--claim", "--json")
        assert result.exit_code != 0
        parsed = json.loads(result.output)
        assert parsed["error"]["code"] == "VALIDATION_ERROR"

    def test_name_not_found(self, invoke) -> None:
        """--name with nonexistent session should error."""
        result = invoke("next", "--name", "Ghost-1", "--json")
        assert result.exit_code != 0
        parsed = json.loads(result.output)
        assert parsed["error"]["code"] == "SESSION_NOT_FOUND"

    def test_resume_with_name(self, create_task, invoke, fill_plan) -> None:
        """Resume-first logic works with structured actor from --name."""
        # Start session
        invoke(
            "session",
            "start",
            "--name",
            "Beacon",
            "--model",
            "gpt-4.1",
            "--framework",
            "codex-cli",
        )

        # Create and claim a task using session identity
        task = create_task("Resume target")
        fill_plan(task["id"], "Resume target")
        claim_result = invoke("next", "--name", "Beacon-1", "--claim", "--json")
        assert claim_result.exit_code == 0
        parsed = json.loads(claim_result.output)
        task_id = parsed["data"]["id"]
        assert parsed["data"]["status"] == "in_planning"

        # Create another higher-priority task
        create_task("Higher priority", "--priority", "critical")

        # next with same session should resume the in_progress task, not pick new one
        result = invoke("next", "--name", "Beacon-1", "--json")
        assert result.exit_code == 0
        parsed = json.loads(result.output)
        assert parsed["data"]["id"] == task_id


class TestNextClaimConcurrency:
    """The mutation uses the task snapshot refreshed after selection."""

    def test_guard_ignores_non_authoritative_snapshot_patch(
        self, create_task, invoke, fill_plan, monkeypatch
    ) -> None:
        """A real assignment/status change after selection wins under the task lock."""
        task = create_task("Race task")
        task_id = task["id"]
        fill_plan(task_id, "Race task")

        import lattice.ops.board_next_claim as claim_module

        original_select = claim_module.select_next
        event_ids_after_race: list[str] = []

        def select_then_change(snapshots, **kwargs):  # noqa: ANN003, ANN202
            selected = original_select(snapshots, **kwargs)
            if selected is not None:
                assigned = invoke("assign", task_id, "agent:alpha", "--actor", "human:test")
                changed = invoke("status", task_id, "in_planning", "--actor", "human:test")
                assert assigned.exit_code == changed.exit_code == 0
                full = json.loads(invoke("show", task_id, "--full", "--json").output)["data"]
                event_ids_after_race.extend(event["id"] for event in full["events"])
            return selected

        monkeypatch.setattr(claim_module, "select_next", select_then_change)

        result = invoke("next", "--actor", "agent:bravo", "--claim", "--json")
        assert result.exit_code != 0
        parsed = json.loads(result.output)
        assert parsed["error"]["code"] == "ALREADY_CLAIMED"
        full = json.loads(invoke("show", task_id, "--full", "--json").output)["data"]
        assert [event["id"] for event in full["events"]] == event_ids_after_race
        assert full["status"] == "in_planning" and full["assigned_to"] == "agent:alpha"

    def test_guard_allows_reclaim_by_same_actor(self, create_task, invoke, fill_plan) -> None:
        """An already-owned in-planning task is returned without a status change."""
        task = create_task("Own task")
        task_id = task["id"]
        result = invoke("next", "--actor", "agent:claude", "--claim", "--json")
        assert result.exit_code == 0
        parsed = json.loads(result.output)
        assert parsed["data"]["id"] == task_id
        assert parsed["data"]["status"] == "in_planning"

    def test_claim_rejects_when_in_progress_by_other(self, create_task, invoke, fill_plan) -> None:
        """If task is already in_progress by another agent, bravo picks the next task."""
        task = create_task("Active task")
        task_id = task["id"]
        fill_plan(task_id, "Active task")

        # agent:alpha claims
        invoke("next", "--actor", "agent:alpha", "--claim")

        # Create a second task for bravo to pick up
        task2 = create_task("Second task")
        task2_id = task2["id"]
        fill_plan(task2_id, "Second task")

        # bravo should get the second task, not the first
        result = invoke("next", "--actor", "agent:bravo", "--claim", "--json")
        assert result.exit_code == 0
        parsed = json.loads(result.output)
        assert parsed["data"]["id"] == task2_id
        assert parsed["data"]["assigned_to"] == "agent:bravo"

    def test_claim_succeeds_when_assigned_to_self(self, create_task, invoke, fill_plan) -> None:
        """Re-claiming your own task should work (no regression)."""
        task = create_task("My task")
        task_id = task["id"]
        fill_plan(task_id, "My task")

        # First claim
        result = invoke("next", "--actor", "agent:claude", "--claim", "--json")
        assert result.exit_code == 0
        parsed = json.loads(result.output)
        assert parsed["data"]["id"] == task_id
        assert parsed["data"]["status"] == "in_planning"

        # Second claim by same actor (resume path)
        result = invoke("next", "--actor", "agent:claude", "--claim", "--json")
        assert result.exit_code == 0
        parsed = json.loads(result.output)
        assert parsed["data"]["id"] == task_id
        assert parsed["data"]["status"] == "in_planning"

    def test_old_server_returned_status_does_not_get_a_planning_hint(
        self, create_task, invoke, cli_env, monkeypatch
    ) -> None:
        """The renderer trusts returned task status rather than local workflow config."""
        task = create_task("Old server result")
        snapshot = {**task, "status": "in_progress", "assigned_to": "agent:claude"}

        from types import SimpleNamespace

        import lattice.cli.query_cmds as query_module

        class FakeBoard:
            lattice_dir = Path(cli_env["LATTICE_ROOT"]) / ".lattice"

            def execute(self, *_args, **_kwargs):  # noqa: ANN002, ANN003, ANN202
                return SimpleNamespace(value=snapshot)

        monkeypatch.setattr(query_module, "board_or_exit", lambda *_args, **_kwargs: FakeBoard())
        result = invoke("next", "--actor", "agent:claude", "--claim")
        assert result.exit_code == 0
        assert "in_progress" in result.output
        assert "Assigned to" not in result.output
        assert "status " not in result.output
