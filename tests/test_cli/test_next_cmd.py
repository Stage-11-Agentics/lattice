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

        # But with --status override, it will
        result = invoke("next", "--status", "review", "--json")
        parsed = json.loads(result.output)
        assert parsed["data"]["title"] == "Review task"


class TestNextClaim:
    """--claim flag atomically assigns and moves to in_progress."""

    def test_claim_requires_actor(self, invoke) -> None:
        result = invoke("next", "--claim")
        assert result.exit_code != 0

    def test_claim_assigns_and_starts(self, create_task, invoke, fill_plan) -> None:
        task = create_task("Claimable task")
        task_id = task["id"]
        fill_plan(task_id, "Claimable task")

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

    def test_claim_blocked_when_plan_missing(self, create_task, invoke, cli_env) -> None:
        task = create_task("No plan file task")
        task_id = task["id"]
        plan_path = Path(cli_env["LATTICE_ROOT"]) / ".lattice" / "plans" / f"{task_id}.md"
        if plan_path.exists():
            plan_path.unlink()

        result = invoke("next", "--actor", "agent:claude", "--claim", "--json")
        assert result.exit_code != 0
        parsed = json.loads(result.output)
        assert parsed["error"]["code"] == "PLAN_REQUIRED"

    def test_claim_blocked_when_plan_is_scaffold(self, create_task, invoke) -> None:
        create_task("Scaffold plan task")
        # Plan is auto-scaffolded on create — just title, no real content

        result = invoke("next", "--actor", "agent:claude", "--claim", "--json")
        assert result.exit_code != 0
        parsed = json.loads(result.output)
        assert parsed["error"]["code"] == "PLAN_REQUIRED"


class TestNextClaimTransitions:
    """--claim emits valid intermediate transitions."""

    def test_claim_planned_task_direct(self, create_task, invoke, fill_plan) -> None:
        """Claiming a planned task should transition planned -> in_progress (1 hop)."""
        task = create_task("Planned task")
        task_id = task["id"]
        invoke("status", task_id, "in_planning", "--actor", "human:test")
        fill_plan(task_id, "Planned task")
        invoke("status", task_id, "planned", "--actor", "human:test")

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
        """Claiming a backlog task should emit backlog -> planned -> in_progress."""
        task = create_task("Backlog task")
        task_id = task["id"]
        fill_plan(task_id, "Backlog task")

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
        assert parsed["data"]["status"] == "in_progress"
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
        assert parsed["data"]["status"] == "in_progress"

        # Create another higher-priority task
        create_task("Higher priority", "--priority", "critical")

        # next with same session should resume the in_progress task, not pick new one
        result = invoke("next", "--name", "Beacon-1", "--json")
        assert result.exit_code == 0
        parsed = json.loads(result.output)
        assert parsed["data"]["id"] == task_id


class TestNextClaimConcurrency:
    """Concurrent claim guard — reject when another agent already claimed.

    The guard fires inside the file lock, after re-reading the snapshot.
    In a real race, both agents call select_next() and pick the same task
    (both see it unassigned), then serialize through the lock.  The first
    writes assignment + status; the second re-reads and should see the
    first's claim.

    To simulate this without threads, we:
    1. Let agent:alpha claim the task normally (select → lock → write).
    2. Manually mutate the snapshot back to 'backlog' + unassigned so
       select_next() picks it again for agent:bravo.
    3. agent:bravo calls next --claim, select_next sees the faked
       snapshot (unassigned/backlog), but the lock path re-reads the
       REAL snapshot (assigned to alpha, in_progress) and rejects.
    """

    def test_guard_rejects_when_snapshot_shows_other_owner(
        self, create_task, invoke, fill_plan, cli_env, monkeypatch
    ) -> None:
        """Patch read_snapshot to return a claimed snapshot inside the lock.

        select_next uses load_all_snapshots (not read_snapshot), so the only
        read_snapshot call for our task_id is the re-read inside the lock.
        We patch that single call to simulate another agent having claimed
        the task between selection and lock acquisition.
        """
        task = create_task("Race task")
        task_id = task["id"]
        fill_plan(task_id, "Race task")

        import lattice.cli.query_cmds as qmod

        original_read = qmod.read_snapshot

        def patched_read(lattice_dir, tid):
            snap = original_read(lattice_dir, tid)
            if tid == task_id and snap is not None:
                # Simulate: alpha claimed between select and lock
                snap = dict(snap)
                snap["assigned_to"] = "agent:alpha"
                snap["status"] = "in_progress"
            return snap

        monkeypatch.setattr(qmod, "read_snapshot", patched_read)

        result = invoke("next", "--actor", "agent:bravo", "--claim", "--json")
        assert result.exit_code != 0
        parsed = json.loads(result.output)
        assert parsed["ok"] is False
        assert parsed["error"]["code"] == "ALREADY_CLAIMED"
        assert "alpha" in parsed["error"]["message"]

    def test_guard_allows_reclaim_by_same_actor(
        self, create_task, invoke, fill_plan, cli_env, monkeypatch
    ) -> None:
        """If the snapshot shows the SAME actor, claim should proceed (no false reject)."""
        task = create_task("Own task")
        task_id = task["id"]
        fill_plan(task_id, "Own task")

        import lattice.cli.query_cmds as qmod

        original_read = qmod.read_snapshot

        def patched_read(lattice_dir, tid):
            snap = original_read(lattice_dir, tid)
            if tid == task_id and snap is not None:
                snap = dict(snap)
                snap["assigned_to"] = "agent:claude"
                snap["status"] = "in_progress"
            return snap

        monkeypatch.setattr(qmod, "read_snapshot", patched_read)

        result = invoke("next", "--actor", "agent:claude", "--claim", "--json")
        assert result.exit_code == 0
        parsed = json.loads(result.output)
        assert parsed["data"]["id"] == task_id

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
        assert parsed["data"]["status"] == "in_progress"

        # Second claim by same actor (resume path)
        result = invoke("next", "--actor", "agent:claude", "--claim", "--json")
        assert result.exit_code == 0
        parsed = json.loads(result.output)
        assert parsed["data"]["id"] == task_id
        assert parsed["data"]["status"] == "in_progress"

    def test_guard_human_readable_error(self, create_task, invoke, fill_plan, monkeypatch) -> None:
        """Non-JSON mode should also show the ALREADY_CLAIMED error."""
        task = create_task("Contested HR task")
        task_id = task["id"]
        fill_plan(task_id, "Contested HR task")

        import lattice.cli.query_cmds as qmod

        original_read = qmod.read_snapshot

        def patched_read(lattice_dir, tid):
            snap = original_read(lattice_dir, tid)
            if tid == task_id and snap is not None:
                snap = dict(snap)
                snap["assigned_to"] = "agent:alpha"
                snap["status"] = "in_progress"
            return snap

        monkeypatch.setattr(qmod, "read_snapshot", patched_read)

        result = invoke("next", "--actor", "agent:bravo", "--claim")
        assert result.exit_code != 0
        assert "already claimed" in result.output.lower()
