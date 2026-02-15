"""End-to-end integration workflow tests and actor resolution tests."""

from __future__ import annotations

import json
from pathlib import Path


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _read_events(lattice_dir: Path, task_id: str, archived: bool = False) -> list[dict]:
    """Read all events from a task's JSONL file."""
    if archived:
        path = lattice_dir / "archive" / "events" / f"{task_id}.jsonl"
    else:
        path = lattice_dir / "events" / f"{task_id}.jsonl"
    events = []
    if path.exists():
        for line in path.read_text().splitlines():
            stripped = line.strip()
            if stripped:
                events.append(json.loads(stripped))
    return events


# ---------------------------------------------------------------------------
# Full lifecycle workflow
# ---------------------------------------------------------------------------


class TestFullLifecycleWorkflow:
    """Test a complete task lifecycle from creation through archival."""

    def test_full_lifecycle_workflow(self, invoke, create_task, initialized_root):
        """Create -> ready -> in_progress -> assign -> comment -> link -> attach -> review -> done -> archive.

        Verify that the full event log captures every operation in order.
        """
        lattice_dir = initialized_root / ".lattice"

        # 1. Create task
        task = create_task("Lifecycle test task")
        task_id = task["id"]

        # 2. Status: backlog -> ready
        r = invoke("status", task_id, "ready", "--actor", "human:test")
        assert r.exit_code == 0

        # 3. Status: ready -> in_progress
        r = invoke("status", task_id, "in_progress", "--actor", "human:test")
        assert r.exit_code == 0

        # 4. Assign
        r = invoke("assign", task_id, "agent:bot", "--actor", "human:test")
        assert r.exit_code == 0

        # 5. Comment
        r = invoke("comment", task_id, "Work in progress", "--actor", "human:test")
        assert r.exit_code == 0

        # 6. Create a second task and link
        task_b = create_task("Blocked by lifecycle")
        task_b_id = task_b["id"]

        r = invoke("link", task_id, "blocks", task_b_id, "--actor", "human:test")
        assert r.exit_code == 0

        # 7. Attach a URL
        r = invoke(
            "attach",
            task_id,
            "https://example.com/docs",
            "--actor",
            "human:test",
        )
        assert r.exit_code == 0

        # 8. Status: in_progress -> review
        r = invoke("status", task_id, "review", "--actor", "human:test")
        assert r.exit_code == 0

        # 9. Status: review -> done
        r = invoke("status", task_id, "done", "--actor", "human:test")
        assert r.exit_code == 0

        # 10. Archive
        r = invoke("archive", task_id, "--actor", "human:test")
        assert r.exit_code == 0

        # Verify events moved to archive
        events = _read_events(lattice_dir, task_id, archived=True)
        assert len(events) > 0

        # Verify event types in order
        event_types = [e["type"] for e in events]
        expected_types = [
            "task_created",
            "status_changed",
            "status_changed",
            "assignment_changed",
            "comment_added",
            "relationship_added",
            "artifact_attached",
            "status_changed",
            "status_changed",
            "task_archived",
        ]
        assert event_types == expected_types

        # Verify archived snapshot exists, active snapshot removed
        assert not (lattice_dir / "tasks" / f"{task_id}.json").exists()
        assert (lattice_dir / "archive" / "tasks" / f"{task_id}.json").exists()


# ---------------------------------------------------------------------------
# Multi-actor attribution
# ---------------------------------------------------------------------------


class TestMultiActorAttribution:
    """Verify that each operation records the correct actor in the event log."""

    def test_multi_actor_attribution(self, invoke, initialized_root):
        """Multiple actors operate on the same task; verify each event has correct actor."""
        lattice_dir = initialized_root / ".lattice"

        # Agent creates the task
        r = invoke("create", "Multi-actor task", "--actor", "agent:creator", "--json")
        assert r.exit_code == 0
        task_id = json.loads(r.output)["data"]["id"]

        # Human updates a field
        r = invoke(
            "update",
            task_id,
            "priority=high",
            "--actor",
            "human:reviewer",
        )
        assert r.exit_code == 0

        # Different agent comments
        r = invoke(
            "comment",
            task_id,
            "Automated analysis complete",
            "--actor",
            "agent:analyzer",
        )
        assert r.exit_code == 0

        # Read event log and verify actors
        events = _read_events(lattice_dir, task_id)
        assert len(events) == 3

        assert events[0]["actor"] == "agent:creator"
        assert events[0]["type"] == "task_created"

        assert events[1]["actor"] == "human:reviewer"
        assert events[1]["type"] == "field_updated"

        assert events[2]["actor"] == "agent:analyzer"
        assert events[2]["type"] == "comment_added"


# ---------------------------------------------------------------------------
# Doctor after lifecycle
# ---------------------------------------------------------------------------


class TestDoctorAfterLifecycle:
    """Verify that doctor reports zero issues after a full lifecycle."""

    def test_doctor_passes_after_full_lifecycle(self, invoke, create_task):
        """Create a task, transition it, archive it, then run doctor. Zero findings expected."""
        task = create_task("Doctor test task")
        task_id = task["id"]

        # Transition through states
        invoke("status", task_id, "ready", "--actor", "human:test")
        invoke("status", task_id, "in_progress", "--actor", "human:test")
        invoke("status", task_id, "review", "--actor", "human:test")
        invoke("status", task_id, "done", "--actor", "human:test")
        invoke("archive", task_id, "--actor", "human:test")

        # Run doctor
        r = invoke("doctor", "--json")
        assert r.exit_code == 0
        data = json.loads(r.output)
        assert data["ok"] is True
        assert data["data"]["summary"]["warnings"] == 0
        assert data["data"]["summary"]["errors"] == 0
        assert data["data"]["findings"] == []


# ---------------------------------------------------------------------------
# Force transition tests
# ---------------------------------------------------------------------------


class TestForceTransition:
    """Test forced status transitions (skipping valid transition checks)."""

    def test_force_transition_without_force_fails(self, invoke, create_task):
        """Attempting backlog -> done without --force should fail with INVALID_TRANSITION."""
        task = create_task("Force test task")
        task_id = task["id"]

        r = invoke("status", task_id, "done", "--actor", "human:test", "--json")
        # The output_error raises SystemExit(1) which Click translates to exit_code=1
        assert r.exit_code == 1
        data = json.loads(r.output)
        assert data["ok"] is False
        assert data["error"]["code"] == "INVALID_TRANSITION"

    def test_force_transition_with_force_succeeds(self, invoke, create_task):
        """backlog -> done with --force --reason should succeed."""
        task = create_task("Force success task")
        task_id = task["id"]

        r = invoke(
            "status",
            task_id,
            "done",
            "--force",
            "--reason",
            "Testing forced transition",
            "--actor",
            "human:test",
            "--json",
        )
        assert r.exit_code == 0
        data = json.loads(r.output)
        assert data["ok"] is True
        assert data["data"]["status"] == "done"

    def test_force_without_reason_fails(self, invoke, create_task):
        """--force without --reason should fail with VALIDATION_ERROR."""
        task = create_task("Force no reason task")
        task_id = task["id"]

        r = invoke(
            "status",
            task_id,
            "done",
            "--force",
            "--actor",
            "human:test",
            "--json",
        )
        assert r.exit_code == 1
        data = json.loads(r.output)
        assert data["ok"] is False
        assert data["error"]["code"] == "VALIDATION_ERROR"
        assert "reason" in data["error"]["message"].lower()


# ---------------------------------------------------------------------------
# Actor flag is required
# ---------------------------------------------------------------------------


class TestActorRequired:
    """Verify that omitting --actor causes a CLI error."""

    def test_actor_flag_is_required(self, invoke):
        """create without --actor should fail (Click's required=True enforcement)."""
        r = invoke("create", "No actor task", "--json")
        assert r.exit_code != 0


# ---------------------------------------------------------------------------
# Agent meta propagation
# ---------------------------------------------------------------------------


class TestAgentMetaPropagation:
    """Verify that --model and --session produce agent_meta in events."""

    def test_agent_meta_propagation(self, invoke, initialized_root):
        """--model and --session should appear in the event's agent_meta."""
        lattice_dir = initialized_root / ".lattice"

        r = invoke(
            "create",
            "Agent meta test",
            "--actor",
            "agent:bot",
            "--model",
            "claude-opus",
            "--session",
            "sess-abc123",
            "--json",
        )
        assert r.exit_code == 0
        task_id = json.loads(r.output)["data"]["id"]

        # Read event log
        events = _read_events(lattice_dir, task_id)
        assert len(events) == 1

        event = events[0]
        assert "agent_meta" in event
        assert event["agent_meta"]["model"] == "claude-opus"
        assert event["agent_meta"]["session"] == "sess-abc123"


# ---------------------------------------------------------------------------
# Custom event recording
# ---------------------------------------------------------------------------


class TestCustomEventRecording:
    """Test the `lattice event` command for custom x_ events."""

    def test_custom_event_recording(self, invoke, create_task, initialized_root):
        """x_deploy event with --data should be recorded correctly."""
        lattice_dir = initialized_root / ".lattice"

        task = create_task("Custom event task")
        task_id = task["id"]

        r = invoke(
            "event",
            task_id,
            "x_deploy",
            "--data",
            '{"env":"prod","version":"1.2.3"}',
            "--actor",
            "human:test",
            "--json",
        )
        assert r.exit_code == 0
        data = json.loads(r.output)
        assert data["ok"] is True
        assert data["data"]["type"] == "x_deploy"

        # Verify in event log
        events = _read_events(lattice_dir, task_id)
        custom_events = [e for e in events if e["type"] == "x_deploy"]
        assert len(custom_events) == 1
        assert custom_events[0]["data"]["env"] == "prod"
        assert custom_events[0]["data"]["version"] == "1.2.3"

    def test_custom_event_reserved_type_rejected(self, invoke, create_task):
        """Using a built-in event type (e.g., status_changed) should be rejected."""
        task = create_task("Reserved type task")
        task_id = task["id"]

        r = invoke(
            "event",
            task_id,
            "status_changed",
            "--actor",
            "human:test",
            "--json",
        )
        assert r.exit_code == 1
        data = json.loads(r.output)
        assert data["ok"] is False
        assert data["error"]["code"] == "VALIDATION_ERROR"
        assert "reserved" in data["error"]["message"].lower()


# ---------------------------------------------------------------------------
# Show after multiple operations
# ---------------------------------------------------------------------------


class TestShowAfterMultipleOperations:
    """Verify that `lattice show --json` includes the full event history."""

    def test_show_after_multiple_operations(self, invoke, create_task):
        """Perform several operations, then show --json. Verify events array."""
        task = create_task("Show test task")
        task_id = task["id"]

        # Perform operations
        invoke("status", task_id, "ready", "--actor", "human:test")
        invoke("status", task_id, "in_progress", "--actor", "human:test")
        invoke("assign", task_id, "agent:worker", "--actor", "human:test")
        invoke("comment", task_id, "Making progress", "--actor", "human:test")
        invoke(
            "update",
            task_id,
            "priority=high",
            "--actor",
            "human:test",
        )

        # Show
        r = invoke("show", task_id, "--json")
        assert r.exit_code == 0
        data = json.loads(r.output)
        assert data["ok"] is True

        show_data = data["data"]
        events = show_data["events"]

        # 1 create + 2 status + 1 assign + 1 comment + 1 update = 6
        assert len(events) == 6

        event_types = [e["type"] for e in events]
        assert event_types == [
            "task_created",
            "status_changed",
            "status_changed",
            "assignment_changed",
            "comment_added",
            "field_updated",
        ]

        # Verify final snapshot state
        assert show_data["status"] == "in_progress"
        assert show_data["assigned_to"] == "agent:worker"
        assert show_data["priority"] == "high"
