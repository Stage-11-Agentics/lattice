"""Tests for query commands: event, list, show."""

from __future__ import annotations

import json
from pathlib import Path

from lattice.core.ids import generate_event_id
from lattice.core.tasks import serialize_snapshot


# ---------------------------------------------------------------------------
# TestEvent
# ---------------------------------------------------------------------------


class TestEvent:
    """Tests for `lattice event <task_id> <event_type>`."""

    def test_custom_event_accepted(self, invoke, create_task):
        """x_ prefix custom events are accepted."""
        task = create_task("Test task")
        task_id = task["id"]

        result = invoke("event", task_id, "x_deployment", "--actor", "human:test")
        assert result.exit_code == 0
        assert "x_deployment" in result.output

    def test_builtin_type_rejected(self, invoke, create_task):
        """Built-in event types are rejected with a clear error."""
        task = create_task("Test task")
        task_id = task["id"]

        result = invoke("event", task_id, "status_changed", "--actor", "human:test")
        assert result.exit_code != 0
        assert "reserved" in result.stderr

    def test_builtin_type_rejected_task_created(self, invoke, create_task):
        """task_created is also rejected."""
        task = create_task("Test task")
        task_id = task["id"]

        result = invoke("event", task_id, "task_created", "--actor", "human:test")
        assert result.exit_code != 0
        assert "reserved" in result.stderr

    def test_invalid_custom_type_no_x_prefix(self, invoke, create_task):
        """Event type without x_ prefix is rejected."""
        task = create_task("Test task")
        task_id = task["id"]

        result = invoke("event", task_id, "my_custom", "--actor", "human:test")
        assert result.exit_code != 0
        assert "x_" in result.stderr

    def test_data_parsed_correctly(self, invoke, create_task, cli_env):
        """--data JSON string is parsed into event data."""
        task = create_task("Test task")
        task_id = task["id"]

        result = invoke(
            "event",
            task_id,
            "x_deploy",
            "--data",
            '{"env": "staging", "version": "1.0"}',
            "--actor",
            "human:test",
            "--json",
        )
        assert result.exit_code == 0
        parsed = json.loads(result.output)
        assert parsed["ok"] is True
        assert parsed["data"]["data"]["env"] == "staging"
        assert parsed["data"]["data"]["version"] == "1.0"

    def test_data_default_empty_dict(self, invoke, create_task):
        """Without --data, event data defaults to empty dict."""
        task = create_task("Test task")
        task_id = task["id"]

        result = invoke(
            "event",
            task_id,
            "x_ping",
            "--actor",
            "human:test",
            "--json",
        )
        assert result.exit_code == 0
        parsed = json.loads(result.output)
        assert parsed["data"]["data"] == {}

    def test_invalid_json_data_error(self, invoke, create_task):
        """Invalid JSON in --data produces VALIDATION_ERROR."""
        task = create_task("Test task")
        task_id = task["id"]

        result = invoke(
            "event",
            task_id,
            "x_test",
            "--data",
            "{not valid json}",
            "--actor",
            "human:test",
        )
        assert result.exit_code != 0
        assert "Invalid JSON" in result.stderr

    def test_invalid_json_data_error_json_mode(self, invoke, create_task):
        """Invalid JSON in --data with --json outputs structured error."""
        task = create_task("Test task")
        task_id = task["id"]

        result = invoke(
            "event",
            task_id,
            "x_test",
            "--data",
            "not json",
            "--actor",
            "human:test",
            "--json",
        )
        assert result.exit_code != 0
        parsed = json.loads(result.output)
        assert parsed["ok"] is False
        assert parsed["error"]["code"] == "VALIDATION_ERROR"

    def test_event_in_per_task_log_only(self, invoke, create_task, cli_env):
        """Custom events go to per-task log but NOT to _lifecycle.jsonl."""
        task = create_task("Test task")
        task_id = task["id"]

        result = invoke("event", task_id, "x_custom", "--actor", "human:test")
        assert result.exit_code == 0

        # Check per-task log has the custom event
        root = Path(cli_env["LATTICE_ROOT"])
        event_log = root / ".lattice" / "events" / f"{task_id}.jsonl"
        lines = event_log.read_text().strip().splitlines()
        custom_events = [
            json.loads(line) for line in lines if json.loads(line)["type"] == "x_custom"
        ]
        assert len(custom_events) == 1

        # Check lifecycle log does NOT have the custom event
        lifecycle_log = root / ".lattice" / "events" / "_lifecycle.jsonl"
        lifecycle_lines = lifecycle_log.read_text().strip().splitlines()
        for line in lifecycle_lines:
            ev = json.loads(line)
            assert ev["type"] != "x_custom", "Custom event should not be in lifecycle log"

    def test_event_id_accepted(self, invoke, create_task):
        """--id with valid ev_ prefix is accepted."""
        task = create_task("Test task")
        task_id = task["id"]
        ev_id = generate_event_id()

        result = invoke(
            "event",
            task_id,
            "x_test",
            "--id",
            ev_id,
            "--actor",
            "human:test",
            "--json",
        )
        assert result.exit_code == 0
        parsed = json.loads(result.output)
        assert parsed["data"]["id"] == ev_id

    def test_invalid_event_id_rejected(self, invoke, create_task):
        """--id with invalid format is rejected."""
        task = create_task("Test task")
        task_id = task["id"]

        result = invoke(
            "event",
            task_id,
            "x_test",
            "--id",
            "bad_id",
            "--actor",
            "human:test",
        )
        assert result.exit_code != 0
        assert "Invalid event ID" in result.stderr

    def test_snapshot_updated(self, invoke, create_task, cli_env):
        """Custom event updates snapshot's last_event_id and updated_at."""
        task = create_task("Test task")
        task_id = task["id"]
        original_event_id = task["last_event_id"]

        invoke("event", task_id, "x_custom", "--actor", "human:test")

        root = Path(cli_env["LATTICE_ROOT"])
        snap = json.loads((root / ".lattice" / "tasks" / f"{task_id}.json").read_text())
        assert snap["last_event_id"] != original_event_id

    def test_quiet_output(self, invoke, create_task):
        """--quiet prints only the event ID."""
        task = create_task("Test task")
        task_id = task["id"]

        result = invoke(
            "event",
            task_id,
            "x_test",
            "--actor",
            "human:test",
            "--quiet",
        )
        assert result.exit_code == 0
        # Output should be just an event ID
        ev_id = result.output.strip()
        assert ev_id.startswith("ev_")

    def test_task_not_found(self, invoke):
        """Logging to a non-existent task fails with NOT_FOUND."""
        result = invoke(
            "event",
            "task_01ZZZZZZZZZZZZZZZZZZZZZZZZ",
            "x_test",
            "--actor",
            "human:test",
        )
        assert result.exit_code != 0
        assert "not found" in result.stderr


# ---------------------------------------------------------------------------
# TestList
# ---------------------------------------------------------------------------


class TestList:
    """Tests for `lattice list`."""

    def test_list_all_returns_all(self, invoke, create_task):
        """No filters returns all tasks."""
        create_task("Task A")
        create_task("Task B")

        result = invoke("list")
        assert result.exit_code == 0
        assert "Task A" in result.output
        assert "Task B" in result.output

    def test_list_empty(self, invoke):
        """Empty project returns no output."""
        result = invoke("list")
        assert result.exit_code == 0
        assert result.output.strip() == ""

    def test_list_empty_json(self, invoke):
        """Empty project with --json returns empty array."""
        result = invoke("list", "--json")
        assert result.exit_code == 0
        parsed = json.loads(result.output)
        assert parsed["ok"] is True
        assert parsed["data"] == []

    def test_filter_by_status(self, invoke, create_task):
        """--status filters tasks by exact status match."""
        create_task("Backlog task")
        task_b = create_task("Planning task")
        task_b_id = task_b["id"]
        invoke("status", task_b_id, "in_planning", "--actor", "human:test")

        result = invoke("list", "--status", "in_planning")
        assert result.exit_code == 0
        assert "Planning task" in result.output
        assert "Backlog task" not in result.output

    def test_filter_by_assigned(self, invoke, create_task):
        """--assigned filters tasks by assigned_to field."""
        task_a = create_task("Task A")
        create_task("Task B")
        invoke("assign", task_a["id"], "agent:claude", "--actor", "human:test")

        result = invoke("list", "--assigned", "agent:claude")
        assert result.exit_code == 0
        assert "Task A" in result.output
        assert "Task B" not in result.output

    def test_filter_by_tag(self, invoke, create_task):
        """--tag filters tasks that contain the specified tag."""
        create_task("Tagged task", "--tags", "frontend,urgent")
        create_task("No tag task")

        result = invoke("list", "--tag", "frontend")
        assert result.exit_code == 0
        assert "Tagged task" in result.output
        assert "No tag task" not in result.output

    def test_filter_by_type(self, invoke, create_task):
        """--type filters tasks by exact type match."""
        create_task("Bug task", "--type", "bug")
        create_task("Normal task")

        result = invoke("list", "--type", "bug")
        assert result.exit_code == 0
        assert "Bug task" in result.output
        assert "Normal task" not in result.output

    def test_combined_filters_and(self, invoke, create_task):
        """Multiple filters are combined with AND logic."""
        create_task("Bug in backlog", "--type", "bug")
        task_b = create_task("Bug planning", "--type", "bug")
        invoke("status", task_b["id"], "in_planning", "--actor", "human:test")
        create_task("Task in backlog")

        result = invoke("list", "--type", "bug", "--status", "backlog")
        assert result.exit_code == 0
        assert "Bug in backlog" in result.output
        assert "Bug planning" not in result.output
        assert "Task in backlog" not in result.output

    def test_no_matches(self, invoke, create_task):
        """Invalid status filter produces a warning and empty results."""
        create_task("Task A")

        result = invoke("list", "--status", "nonexistent")
        assert result.exit_code == 0
        assert "Task A" not in result.output
        assert "not a configured status" in result.output

    def test_no_matches_valid_status(self, invoke, create_task):
        """Valid status filter with no matching tasks produces empty output."""
        create_task("Task A")

        result = invoke("list", "--status", "done")
        assert result.exit_code == 0
        assert result.output.strip() == ""

    def test_compact_human_output_format(self, invoke, create_task):
        """Human output shows compact one-line-per-task format."""
        task = create_task("My test task", "--type", "bug", "--priority", "high")

        result = invoke("list")
        assert result.exit_code == 0
        output = result.output.strip()
        # Output line should contain: id, status, priority, type, title, assigned
        assert task["id"] in output
        assert "backlog" in output
        assert "high" in output
        assert "bug" in output
        assert '"My test task"' in output
        assert "unassigned" in output

    def test_json_output(self, invoke, create_task):
        """--json outputs full task array."""
        create_task("Task A")
        create_task("Task B")

        result = invoke("list", "--json")
        assert result.exit_code == 0
        parsed = json.loads(result.output)
        assert parsed["ok"] is True
        assert len(parsed["data"]) == 2
        # Full snapshots have many fields
        assert "relationships_out" in parsed["data"][0]

    def test_json_compact_output(self, invoke, create_task):
        """--json --compact outputs compact task views."""
        create_task("Task A")

        result = invoke("list", "--json", "--compact")
        assert result.exit_code == 0
        parsed = json.loads(result.output)
        assert parsed["ok"] is True
        task_data = parsed["data"][0]
        # Compact view has specific fields
        assert "id" in task_data
        assert "title" in task_data
        assert "relationships_out_count" in task_data
        # But not full snapshot fields
        assert "relationships_out" not in task_data
        assert "description" not in task_data

    def test_sorted_by_id(self, invoke, create_task):
        """Tasks are sorted by ID (ULID = chronological)."""
        t1 = create_task("First")
        create_task("Second")
        t3 = create_task("Third")

        result = invoke("list", "--json")
        parsed = json.loads(result.output)
        ids = [t["id"] for t in parsed["data"]]
        assert ids == sorted(ids)
        assert ids[0] == t1["id"]
        assert ids[-1] == t3["id"]

    def test_unassigned_display(self, invoke, create_task):
        """Tasks with no assignment show 'unassigned'."""
        create_task("Unassigned task")

        result = invoke("list")
        assert "unassigned" in result.output

    def test_quiet_outputs_ids_only(self, invoke, create_task):
        """--quiet prints one task ID per line."""
        t1 = create_task("Task A")
        t2 = create_task("Task B")

        result = invoke("list", "--quiet")
        assert result.exit_code == 0
        lines = result.output.strip().splitlines()
        assert len(lines) == 2
        ids = {line.strip() for line in lines}
        assert t1["id"] in ids
        assert t2["id"] in ids

    def test_quiet_with_filter(self, invoke, create_task):
        """--quiet with --status filter outputs only matching IDs."""
        create_task("Backlog task")
        t2 = create_task("Planning task")
        invoke("status", t2["id"], "in_planning", "--actor", "human:test")

        result = invoke("list", "--quiet", "--status", "in_planning")
        assert result.exit_code == 0
        lines = result.output.strip().splitlines()
        assert len(lines) == 1
        assert lines[0].strip() == t2["id"]

    def test_quiet_empty(self, invoke):
        """--quiet with no tasks produces empty output."""
        result = invoke("list", "--quiet")
        assert result.exit_code == 0
        assert result.output.strip() == ""

    def test_include_archived_shows_archived_tasks(self, invoke, create_task):
        """--include-archived includes archived tasks in output."""
        create_task("Active task")
        t2 = create_task("Archived task")
        invoke("archive", t2["id"], "--actor", "human:test")

        # Without --include-archived, only active task shown
        result = invoke("list")
        assert "Active task" in result.output
        assert "Archived task" not in result.output

        # With --include-archived, both shown
        result = invoke("list", "--include-archived")
        assert "Active task" in result.output
        assert "Archived task" in result.output
        assert "[A]" in result.output  # archived marker

    def test_include_archived_json(self, invoke, create_task):
        """--include-archived --json marks archived tasks with archived=true."""
        t1 = create_task("Active")
        t2 = create_task("Archived")
        invoke("archive", t2["id"], "--actor", "human:test")

        result = invoke("list", "--include-archived", "--json")
        assert result.exit_code == 0
        parsed = json.loads(result.output)
        assert parsed["ok"] is True
        assert len(parsed["data"]) == 2

        active = [t for t in parsed["data"] if t["id"] == t1["id"]][0]
        archived = [t for t in parsed["data"] if t["id"] == t2["id"]][0]
        assert "archived" not in active or active.get("archived") is not True
        assert archived.get("archived") is True

    def test_include_archived_with_filter(self, invoke, create_task):
        """--include-archived respects other filters."""
        create_task("Active bug", "--type", "bug")
        t2 = create_task("Archived bug", "--type", "bug")
        t3 = create_task("Archived task", "--type", "task")
        invoke("archive", t2["id"], "--actor", "human:test")
        invoke("archive", t3["id"], "--actor", "human:test")

        result = invoke("list", "--include-archived", "--type", "bug")
        assert "Active bug" in result.output
        assert "Archived bug" in result.output
        assert "Archived task" not in result.output


# ---------------------------------------------------------------------------
# TestShow
# ---------------------------------------------------------------------------


class TestShow:
    """Tests for `lattice show <task_id>`."""

    def test_basic_show(self, invoke, create_task):
        """Show displays task info in human format."""
        task = create_task(
            "My bug", "--type", "bug", "--priority", "high", "--description", "Something is broken"
        )
        task_id = task["id"]

        result = invoke("show", task_id)
        assert result.exit_code == 0
        assert task_id in result.output
        assert '"My bug"' in result.output
        assert "Status: backlog" in result.output
        assert "Priority: high" in result.output
        assert "Type: bug" in result.output
        assert "Description:" in result.output
        assert "Something is broken" in result.output

    def test_events_shown_latest_first(self, invoke, create_task):
        """Events are displayed in reverse chronological order."""
        task = create_task("Test task")
        task_id = task["id"]
        invoke("comment", task_id, "First comment", "--actor", "human:test")
        invoke("comment", task_id, "Second comment", "--actor", "human:test")

        result = invoke("show", task_id)
        assert result.exit_code == 0
        assert "Events (latest first):" in result.output

        # Second comment should appear before First comment
        output = result.output
        pos_second = output.find("Second comment")
        pos_first = output.find("First comment")
        assert pos_second < pos_first, "Latest events should be shown first"

    def test_not_found_error(self, invoke):
        """Non-existent task shows NOT_FOUND error."""
        result = invoke("show", "task_01ZZZZZZZZZZZZZZZZZZZZZZZZ")
        assert result.exit_code != 0
        assert "not found" in result.stderr

    def test_not_found_json(self, invoke):
        """Non-existent task with --json shows structured error."""
        result = invoke("show", "task_01ZZZZZZZZZZZZZZZZZZZZZZZZ", "--json")
        assert result.exit_code != 0
        parsed = json.loads(result.output)
        assert parsed["ok"] is False
        assert parsed["error"]["code"] == "NOT_FOUND"

    def test_json_output(self, invoke, create_task):
        """--json outputs full structured data."""
        task = create_task("Test task")
        task_id = task["id"]

        result = invoke("show", task_id, "--json")
        assert result.exit_code == 0
        parsed = json.loads(result.output)
        assert parsed["ok"] is True
        data = parsed["data"]
        assert data["id"] == task_id
        assert data["title"] == "Test task"
        assert "events" in data
        assert isinstance(data["events"], list)

    def test_compact_output(self, invoke, create_task):
        """--compact shows only compact fields."""
        task = create_task("Test task")
        task_id = task["id"]

        result = invoke("show", task_id, "--compact")
        assert result.exit_code == 0
        # Compact human output should have status, priority, type
        assert "Status:" in result.output
        # But not events or description sections
        assert "Events" not in result.output

    def test_compact_json_output(self, invoke, create_task):
        """--compact --json shows compact snapshot fields."""
        task = create_task("Test task")
        task_id = task["id"]

        result = invoke("show", task_id, "--compact", "--json")
        assert result.exit_code == 0
        parsed = json.loads(result.output)
        data = parsed["data"]
        assert "id" in data
        assert "relationships_out_count" in data
        # No events in compact mode
        assert "events" not in data

    def test_archived_task_found(self, invoke, create_task, cli_env):
        """Archived tasks are found in archive/ directory."""
        task = create_task("Will be archived")
        task_id = task["id"]

        # Manually move task to archive to simulate archival
        root = Path(cli_env["LATTICE_ROOT"])
        lattice_dir = root / ".lattice"

        # Move snapshot
        src_snap = lattice_dir / "tasks" / f"{task_id}.json"
        dst_snap = lattice_dir / "archive" / "tasks" / f"{task_id}.json"
        dst_snap.write_text(src_snap.read_text())
        src_snap.unlink()

        # Move event log
        src_events = lattice_dir / "events" / f"{task_id}.jsonl"
        dst_events = lattice_dir / "archive" / "events" / f"{task_id}.jsonl"
        dst_events.write_text(src_events.read_text())
        src_events.unlink()

        result = invoke("show", task_id)
        assert result.exit_code == 0
        assert "ARCHIVED" in result.output

    def test_archived_task_json(self, invoke, create_task, cli_env):
        """Archived task with --json includes archived flag."""
        task = create_task("Will be archived")
        task_id = task["id"]

        root = Path(cli_env["LATTICE_ROOT"])
        lattice_dir = root / ".lattice"

        src_snap = lattice_dir / "tasks" / f"{task_id}.json"
        dst_snap = lattice_dir / "archive" / "tasks" / f"{task_id}.json"
        dst_snap.write_text(src_snap.read_text())
        src_snap.unlink()

        result = invoke("show", task_id, "--json")
        assert result.exit_code == 0
        parsed = json.loads(result.output)
        assert parsed["data"]["archived"] is True

    def test_notes_path_shown_when_exists(self, invoke, create_task, cli_env):
        """Notes path is displayed when the notes file exists."""
        task = create_task("Task with notes")
        task_id = task["id"]

        # Create a notes file manually
        root = Path(cli_env["LATTICE_ROOT"])
        notes_path = root / ".lattice" / "notes" / f"{task_id}.md"
        notes_path.write_text("# Task Notes\nSome notes here.\n")

        result = invoke("show", task_id)
        assert result.exit_code == 0
        assert f"Notes: notes/{task_id}.md" in result.output

    def test_notes_path_in_json(self, invoke, create_task, cli_env):
        """Notes path appears in JSON output when notes file exists."""
        task = create_task("Task with notes")
        task_id = task["id"]

        root = Path(cli_env["LATTICE_ROOT"])
        notes_path = root / ".lattice" / "notes" / f"{task_id}.md"
        notes_path.write_text("Notes content.\n")

        result = invoke("show", task_id, "--json")
        assert result.exit_code == 0
        parsed = json.loads(result.output)
        assert parsed["data"]["notes_path"] == f"notes/{task_id}.md"

    def test_no_notes_path_when_absent(self, invoke, create_task, cli_env):
        """Notes section is not shown when no notes file exists."""
        task = create_task("Task without notes")
        task_id = task["id"]

        # Remove the auto-scaffolded notes file to test the no-notes path
        root = Path(cli_env["LATTICE_ROOT"])
        notes_path = root / ".lattice" / "notes" / f"{task_id}.md"
        if notes_path.exists():
            notes_path.unlink()

        result = invoke("show", task_id)
        assert result.exit_code == 0
        assert "Notes:" not in result.output

    def test_plan_scaffolded_on_create(self, invoke, create_task, cli_env):
        """Plan file is scaffolded on create, notes file is NOT."""
        task = create_task("Plan scaffold test")
        task_id = task["id"]

        root = Path(cli_env["LATTICE_ROOT"])
        plan_path = root / ".lattice" / "plans" / f"{task_id}.md"
        notes_path = root / ".lattice" / "notes" / f"{task_id}.md"

        assert plan_path.exists(), "Plan file should be scaffolded on create"
        assert not notes_path.exists(), "Notes file should NOT be scaffolded on create"

        content = plan_path.read_text()
        assert "## Summary" in content
        assert "## Technical Plan" in content
        assert "## Acceptance Criteria" in content

    def test_plan_path_shown_when_exists(self, invoke, create_task, cli_env):
        """Plan path is displayed when the plan file exists."""
        task = create_task("Task with plan")
        task_id = task["id"]

        result = invoke("show", task_id)
        assert result.exit_code == 0
        assert f"Plan: plans/{task_id}.md" in result.output

    def test_plan_path_in_json(self, invoke, create_task, cli_env):
        """Plan path appears in JSON output when plan file exists."""
        task = create_task("Task with plan")
        task_id = task["id"]

        result = invoke("show", task_id, "--json")
        assert result.exit_code == 0
        parsed = json.loads(result.output)
        assert parsed["data"]["plan_path"] == f"plans/{task_id}.md"

    def test_plan_command_shows_content(self, invoke, create_task, cli_env):
        """lattice plan <task> shows plan file content."""
        task = create_task("Task for plan cmd")
        task_id = task["id"]

        result = invoke("plan", task_id)
        assert result.exit_code == 0
        assert "## Summary" in result.output
        assert "## Technical Plan" in result.output

    def test_plan_command_json(self, invoke, create_task, cli_env):
        """lattice plan <task> --json returns plan content."""
        task = create_task("Task for plan json")
        task_id = task["id"]

        result = invoke("plan", task_id, "--json")
        assert result.exit_code == 0
        parsed = json.loads(result.output)
        assert parsed["ok"] is True
        assert "content" in parsed["data"]
        assert "## Summary" in parsed["data"]["content"]

    def test_plan_command_not_found(self, invoke, create_task, cli_env):
        """lattice plan for task with no plan file reports not found."""
        task = create_task("Task no plan")
        task_id = task["id"]

        # Remove the auto-scaffolded plan file
        root = Path(cli_env["LATTICE_ROOT"])
        plan_path = root / ".lattice" / "plans" / f"{task_id}.md"
        if plan_path.exists():
            plan_path.unlink()

        result = invoke("plan", task_id)
        assert result.exit_code != 0
        assert "No plan file" in result.output

    def test_relationships_displayed(self, invoke, create_task):
        """Outgoing relationships are shown with target title."""
        task_a = create_task("Blocker task")
        task_b = create_task("Blocked task")

        invoke(
            "link",
            task_a["id"],
            "blocks",
            task_b["id"],
            "--actor",
            "human:test",
        )

        result = invoke("show", task_a["id"])
        assert result.exit_code == 0
        assert "Relationships (outgoing):" in result.output
        assert "blocks" in result.output
        assert task_b["id"] in result.output
        assert "Blocked task" in result.output

    def test_artifact_refs_displayed(self, invoke, create_task, cli_env):
        """Artifact refs are displayed."""
        task = create_task("Task with artifact")
        task_id = task["id"]

        # Manually add an artifact ref to the snapshot
        root = Path(cli_env["LATTICE_ROOT"])
        lattice_dir = root / ".lattice"
        snap_path = lattice_dir / "tasks" / f"{task_id}.json"
        snap = json.loads(snap_path.read_text())
        art_id = "art_01J0000000000000000000000"
        snap["artifact_refs"] = [art_id]
        snap_path.write_text(serialize_snapshot(snap))

        result = invoke("show", task_id)
        assert result.exit_code == 0
        assert "Artifacts:" in result.output
        assert art_id in result.output

    def test_full_flag_shows_event_data(self, invoke, create_task):
        """--full includes complete event data in output."""
        task = create_task("Test task")
        task_id = task["id"]
        invoke("comment", task_id, "Test comment body", "--actor", "human:test")

        result = invoke("show", task_id, "--full")
        assert result.exit_code == 0
        # Full mode shows JSON data for events
        assert '"body"' in result.output
        assert "Test comment body" in result.output

    def test_show_assigned_task(self, invoke, create_task):
        """Show displays assigned actor."""
        task = create_task("Assigned task")
        invoke("assign", task["id"], "agent:claude", "--actor", "human:test")

        result = invoke("show", task["id"])
        assert result.exit_code == 0
        assert "Assigned: agent:claude" in result.output

    def test_show_unassigned_task(self, invoke, create_task):
        """Unassigned tasks show 'unassigned' for the assigned field."""
        task = create_task("Unassigned task")

        result = invoke("show", task["id"])
        assert result.exit_code == 0
        assert "Assigned: unassigned" in result.output

    def test_show_created_by(self, invoke, create_task):
        """Show displays the created_by actor."""
        task = create_task("Test task")

        result = invoke("show", task["id"])
        assert result.exit_code == 0
        assert "Created by: human:test" in result.output

    def test_show_task_created_event(self, invoke, create_task):
        """Show displays the task_created event."""
        task = create_task("Test task")

        result = invoke("show", task["id"])
        assert result.exit_code == 0
        assert "task_created" in result.output

    def test_show_multiple_event_types(self, invoke, create_task):
        """Show displays multiple event types."""
        task = create_task("Test task")
        task_id = task["id"]
        invoke("status", task_id, "in_planning", "--actor", "human:test")
        invoke("comment", task_id, "A comment", "--actor", "agent:claude")

        result = invoke("show", task_id)
        assert result.exit_code == 0
        assert "task_created" in result.output
        assert "status_changed" in result.output
        assert "comment_added" in result.output

    def test_json_output_includes_events(self, invoke, create_task):
        """JSON output includes events array."""
        task = create_task("Test task")
        task_id = task["id"]
        invoke("comment", task_id, "A comment", "--actor", "human:test")

        result = invoke("show", task_id, "--json")
        parsed = json.loads(result.output)
        assert "events" in parsed["data"]
        assert len(parsed["data"]["events"]) == 2  # task_created + comment_added

    def test_json_output_includes_relationships_enriched(self, invoke, create_task):
        """JSON output includes enriched relationships."""
        task_a = create_task("Source")
        task_b = create_task("Target")
        invoke("link", task_a["id"], "blocks", task_b["id"], "--actor", "human:test")

        result = invoke("show", task_a["id"], "--json")
        parsed = json.loads(result.output)
        rels = parsed["data"]["relationships_enriched"]
        assert len(rels) == 1
        assert rels[0]["target_task_id"] == task_b["id"]
        assert rels[0]["target_title"] == "Target"

    def test_show_renders_provenance_line(self, invoke, create_task):
        """Show output includes provenance line when event has provenance."""
        task = create_task("Provenance show test")
        task_id = task["id"]
        invoke(
            "comment",
            task_id,
            "Prov comment",
            "--actor",
            "agent:claude",
            "--triggered-by",
            "ev_TRIGGER123",
            "--on-behalf-of",
            "human:atin",
            "--reason",
            "Sprint planning",
        )

        result = invoke("show", task_id)
        assert result.exit_code == 0
        assert "triggered by: ev_TRIGGER123" in result.output
        assert "on behalf of: human:atin" in result.output
        assert "reason: Sprint planning" in result.output

    def test_show_no_provenance_line_when_absent(self, invoke, create_task):
        """Show output does NOT include provenance line when events lack provenance."""
        task = create_task("No provenance show test")
        task_id = task["id"]
        invoke("comment", task_id, "Plain comment", "--actor", "human:test")

        result = invoke("show", task_id)
        assert result.exit_code == 0
        assert "triggered by:" not in result.output
        assert "on behalf of:" not in result.output
        # The word "reason:" could match other things, so check the indent pattern
        for line in result.output.splitlines():
            if line.startswith("    ") and "reason:" in line:
                raise AssertionError("Unexpected provenance line found")
