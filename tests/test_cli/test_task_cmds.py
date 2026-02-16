"""Tests for Phase 2 task write commands: create, update, status, assign, comment."""

from __future__ import annotations

import json


# ---------------------------------------------------------------------------
# TestCreate
# ---------------------------------------------------------------------------


class TestCreate:
    """Tests for `lattice create`."""

    def test_happy_path(self, invoke):
        result = invoke("create", "My task", "--actor", "human:test")
        assert result.exit_code == 0
        assert "Created task" in result.output
        assert "My task" in result.output

    def test_defaults_from_config(self, invoke_json):
        data, code = invoke_json("create", "Default task", "--actor", "human:test")
        assert code == 0
        assert data["ok"] is True
        snap = data["data"]
        assert snap["status"] == "backlog"
        assert snap["priority"] == "medium"
        assert snap["type"] == "task"

    def test_custom_fields(self, invoke_json):
        data, code = invoke_json(
            "create",
            "Bug task",
            "--type",
            "bug",
            "--priority",
            "high",
            "--urgency",
            "immediate",
            "--status",
            "in_planning",
            "--description",
            "A bug",
            "--tags",
            "ui,backend",
            "--assigned-to",
            "agent:claude",
            "--actor",
            "human:test",
        )
        assert code == 0
        snap = data["data"]
        assert snap["type"] == "bug"
        assert snap["priority"] == "high"
        assert snap["urgency"] == "immediate"
        assert snap["status"] == "in_planning"
        assert snap["description"] == "A bug"
        assert snap["tags"] == ["ui", "backend"]
        assert snap["assigned_to"] == "agent:claude"

    def test_id_idempotent_success(self, invoke):
        tid = "task_00000000000000000000000000"
        r1 = invoke("create", "Idem task", "--id", tid, "--actor", "human:test")
        assert r1.exit_code == 0
        r2 = invoke("create", "Idem task", "--id", tid, "--actor", "human:test")
        assert r2.exit_code == 0
        assert "idempotent" in r2.output.lower()

    def test_id_conflict(self, invoke):
        tid = "task_00000000000000000000000001"
        invoke("create", "First", "--id", tid, "--actor", "human:test")
        result = invoke("create", "Different", "--id", tid, "--actor", "human:test")
        assert result.exit_code != 0
        assert "Conflict" in result.output or "conflict" in result.output.lower()

    def test_invalid_type(self, invoke):
        result = invoke("create", "Bad", "--type", "nonexistent", "--actor", "human:test")
        assert result.exit_code != 0

    def test_invalid_priority(self, invoke):
        result = invoke("create", "Bad", "--priority", "nope", "--actor", "human:test")
        assert result.exit_code != 0

    def test_invalid_status(self, invoke):
        result = invoke("create", "Bad", "--status", "nope", "--actor", "human:test")
        assert result.exit_code != 0

    def test_quiet_output(self, invoke):
        result = invoke("create", "Quiet task", "--actor", "human:test", "--quiet")
        assert result.exit_code == 0
        # Quiet mode should print only the task ID
        output = result.output.strip()
        assert output.startswith("task_")
        # Should be a single line
        assert "\n" not in output

    def test_json_envelope(self, invoke_json):
        data, code = invoke_json("create", "JSON task", "--actor", "human:test")
        assert code == 0
        assert data["ok"] is True
        assert "id" in data["data"]
        assert data["data"]["title"] == "JSON task"

    def test_json_error_envelope(self, invoke):
        result = invoke("create", "Bad", "--priority", "nope", "--actor", "human:test", "--json")
        assert result.exit_code != 0
        parsed = json.loads(result.output)
        assert parsed["ok"] is False
        assert "code" in parsed["error"]
        assert "message" in parsed["error"]


# ---------------------------------------------------------------------------
# TestUpdate
# ---------------------------------------------------------------------------


class TestUpdate:
    """Tests for `lattice update`."""

    def test_single_field(self, create_task, invoke):
        task = create_task("Update me")
        task_id = task["id"]
        result = invoke("update", task_id, "title=New Title", "--actor", "human:test")
        assert result.exit_code == 0
        assert "title" in result.output

    def test_multiple_fields(self, create_task, invoke_json):
        task = create_task("Multi update")
        task_id = task["id"]
        data, code = invoke_json(
            "update",
            task_id,
            "title=Changed Title",
            "priority=high",
            "--actor",
            "human:test",
        )
        assert code == 0
        snap = data["data"]
        assert snap["title"] == "Changed Title"
        assert snap["priority"] == "high"

    def test_no_change_skip(self, create_task, invoke):
        task = create_task("Same title")
        task_id = task["id"]
        # Update to the same title should produce "No changes"
        result = invoke("update", task_id, "title=Same title", "--actor", "human:test")
        assert result.exit_code == 0
        assert "No changes" in result.output

    def test_field_value_split_on_first_equals(self, create_task, invoke_json):
        """Values can contain '=' signs — we split on the first '=' only."""
        task = create_task("Equals test")
        task_id = task["id"]
        data, code = invoke_json(
            "update",
            task_id,
            "description=key=value pair inside",
            "--actor",
            "human:test",
        )
        assert code == 0
        assert data["data"]["description"] == "key=value pair inside"

    def test_tags_comma_parsing(self, create_task, invoke_json):
        task = create_task("Tag test")
        task_id = task["id"]
        data, code = invoke_json(
            "update",
            task_id,
            "tags=frontend, backend, urgent",
            "--actor",
            "human:test",
        )
        assert code == 0
        assert data["data"]["tags"] == ["frontend", "backend", "urgent"]

    def test_custom_fields_dot_notation(self, create_task, invoke_json):
        task = create_task("CF test")
        task_id = task["id"]
        data, code = invoke_json(
            "update",
            task_id,
            "custom_fields.env=production",
            "--actor",
            "human:test",
        )
        assert code == 0
        assert data["data"]["custom_fields"]["env"] == "production"

    def test_reject_status_field(self, create_task, invoke):
        task = create_task("Status field test")
        task_id = task["id"]
        result = invoke("update", task_id, "status=done", "--actor", "human:test")
        assert result.exit_code != 0
        assert "lattice status" in result.output or "lattice status" in result.stderr

    def test_reject_assigned_to_field(self, create_task, invoke):
        task = create_task("Assign field test")
        task_id = task["id"]
        result = invoke("update", task_id, "assigned_to=human:bob", "--actor", "human:test")
        assert result.exit_code != 0
        assert "lattice assign" in result.output or "lattice assign" in result.stderr

    def test_invalid_priority(self, create_task, invoke):
        task = create_task("Bad priority update")
        task_id = task["id"]
        result = invoke("update", task_id, "priority=super", "--actor", "human:test")
        assert result.exit_code != 0

    def test_invalid_urgency(self, create_task, invoke):
        task = create_task("Bad urgency update")
        task_id = task["id"]
        result = invoke("update", task_id, "urgency=super", "--actor", "human:test")
        assert result.exit_code != 0

    def test_invalid_type(self, create_task, invoke):
        task = create_task("Bad type update")
        task_id = task["id"]
        result = invoke("update", task_id, "type=nonexistent", "--actor", "human:test")
        assert result.exit_code != 0

    def test_unknown_field(self, create_task, invoke):
        task = create_task("Unknown field")
        task_id = task["id"]
        result = invoke("update", task_id, "foobar=xyz", "--actor", "human:test")
        assert result.exit_code != 0

    def test_no_pairs_error(self, create_task, invoke):
        task = create_task("No pairs")
        task_id = task["id"]
        result = invoke("update", task_id, "--actor", "human:test")
        assert result.exit_code != 0

    def test_batch_events_share_timestamp(self, create_task, invoke_json, initialized_root):
        """All field_updated events from a single update share the same ts."""
        task = create_task("Batch ts test")
        task_id = task["id"]
        invoke_json(
            "update",
            task_id,
            "title=New",
            "priority=high",
            "--actor",
            "human:test",
        )
        events_path = initialized_root / ".lattice" / "events" / f"{task_id}.jsonl"
        lines = events_path.read_text().strip().split("\n")
        # First event is task_created, rest are field_updated
        field_events = [json.loads(line) for line in lines[1:]]
        assert len(field_events) == 2
        assert field_events[0]["ts"] == field_events[1]["ts"]


# ---------------------------------------------------------------------------
# TestStatus
# ---------------------------------------------------------------------------


class TestStatus:
    """Tests for `lattice status`."""

    def test_valid_transition(self, create_task, invoke):
        task = create_task("Status test")
        task_id = task["id"]
        # backlog -> in_planning is a valid transition
        result = invoke("status", task_id, "in_planning", "--actor", "human:test")
        assert result.exit_code == 0
        assert "in_planning" in result.output

    def test_invalid_transition_without_force(self, create_task, invoke):
        task = create_task("Invalid trans")
        task_id = task["id"]
        # backlog -> done is not a valid transition
        result = invoke("status", task_id, "done", "--actor", "human:test")
        assert result.exit_code != 0
        assert "Invalid transition" in result.output or "INVALID_TRANSITION" in result.output

    def test_force_without_reason(self, create_task, invoke):
        task = create_task("Force no reason")
        task_id = task["id"]
        result = invoke("status", task_id, "done", "--force", "--actor", "human:test")
        assert result.exit_code != 0
        assert "--reason" in result.output or "--reason" in result.stderr

    def test_force_with_reason(self, create_task, invoke_json):
        task = create_task("Force with reason")
        task_id = task["id"]
        data, code = invoke_json(
            "status",
            task_id,
            "done",
            "--force",
            "--reason",
            "Skip for release",
            "--actor",
            "human:test",
        )
        assert code == 0
        assert data["data"]["status"] == "done"

    def test_already_at_status(self, create_task, invoke):
        task = create_task("Same status")
        task_id = task["id"]
        # Task starts at backlog
        result = invoke("status", task_id, "backlog", "--actor", "human:test")
        assert result.exit_code == 0
        assert "Already at status" in result.output

    def test_json_output(self, create_task, invoke_json):
        task = create_task("JSON status")
        task_id = task["id"]
        data, code = invoke_json("status", task_id, "in_planning", "--actor", "human:test")
        assert code == 0
        assert data["ok"] is True
        assert data["data"]["status"] == "in_planning"

    def test_invalid_status_name(self, create_task, invoke):
        task = create_task("Bad status name")
        task_id = task["id"]
        result = invoke("status", task_id, "nonexistent", "--actor", "human:test")
        assert result.exit_code != 0

    def test_status_event_in_jsonl(self, create_task, invoke, initialized_root):
        task = create_task("Event check")
        task_id = task["id"]
        invoke("status", task_id, "in_planning", "--actor", "human:test")
        events_path = initialized_root / ".lattice" / "events" / f"{task_id}.jsonl"
        lines = events_path.read_text().strip().split("\n")
        status_event = json.loads(lines[-1])
        assert status_event["type"] == "status_changed"
        assert status_event["data"]["from"] == "backlog"
        assert status_event["data"]["to"] == "in_planning"


# ---------------------------------------------------------------------------
# TestAssign
# ---------------------------------------------------------------------------


class TestAssign:
    """Tests for `lattice assign`."""

    def test_valid_actor(self, create_task, invoke):
        task = create_task("Assign test")
        task_id = task["id"]
        result = invoke("assign", task_id, "agent:claude", "--actor", "human:test")
        assert result.exit_code == 0
        assert "agent:claude" in result.output

    def test_invalid_format(self, create_task, invoke):
        task = create_task("Bad assign")
        task_id = task["id"]
        result = invoke("assign", task_id, "not_valid", "--actor", "human:test")
        assert result.exit_code != 0

    def test_no_change_skip(self, create_task, invoke):
        task = create_task("Assign no change", "--assigned-to", "agent:claude")
        task_id = task["id"]
        result = invoke("assign", task_id, "agent:claude", "--actor", "human:test")
        assert result.exit_code == 0
        assert "Already assigned" in result.output

    def test_json_output(self, create_task, invoke_json):
        task = create_task("JSON assign")
        task_id = task["id"]
        data, code = invoke_json("assign", task_id, "human:bob", "--actor", "human:test")
        assert code == 0
        assert data["ok"] is True
        assert data["data"]["assigned_to"] == "human:bob"

    def test_reassign(self, create_task, invoke_json):
        task = create_task("Reassign test", "--assigned-to", "agent:old")
        task_id = task["id"]
        data, code = invoke_json("assign", task_id, "agent:new", "--actor", "human:test")
        assert code == 0
        assert data["data"]["assigned_to"] == "agent:new"

    def test_assign_event_data(self, create_task, invoke, initialized_root):
        task = create_task("Event assign")
        task_id = task["id"]
        invoke("assign", task_id, "team:frontend", "--actor", "human:test")
        events_path = initialized_root / ".lattice" / "events" / f"{task_id}.jsonl"
        lines = events_path.read_text().strip().split("\n")
        event = json.loads(lines[-1])
        assert event["type"] == "assignment_changed"
        assert event["data"]["from"] is None
        assert event["data"]["to"] == "team:frontend"


# ---------------------------------------------------------------------------
# TestComment
# ---------------------------------------------------------------------------


class TestComment:
    """Tests for `lattice comment`."""

    def test_event_appended(self, create_task, invoke, initialized_root):
        task = create_task("Comment test")
        task_id = task["id"]
        invoke("comment", task_id, "Hello world", "--actor", "human:test")
        events_path = initialized_root / ".lattice" / "events" / f"{task_id}.jsonl"
        lines = events_path.read_text().strip().split("\n")
        last_event = json.loads(lines[-1])
        assert last_event["type"] == "comment_added"
        assert last_event["data"]["body"] == "Hello world"

    def test_snapshot_updated(self, create_task, invoke_json):
        task = create_task("Comment snap")
        task_id = task["id"]
        old_event_id = task["last_event_id"]
        data, code = invoke_json("comment", task_id, "A comment", "--actor", "human:test")
        assert code == 0
        snap = data["data"]
        # last_event_id should have changed
        assert snap["last_event_id"] != old_event_id
        # Title and status should not change
        assert snap["title"] == "Comment snap"
        assert snap["status"] == "backlog"

    def test_comment_body_in_event(self, create_task, invoke, initialized_root):
        task = create_task("Body check")
        task_id = task["id"]
        long_text = "This is a longer comment with special chars: =, --force, 'quotes'"
        invoke("comment", task_id, long_text, "--actor", "human:test")
        events_path = initialized_root / ".lattice" / "events" / f"{task_id}.jsonl"
        lines = events_path.read_text().strip().split("\n")
        event = json.loads(lines[-1])
        assert event["data"]["body"] == long_text

    def test_json_output(self, create_task, invoke_json):
        task = create_task("JSON comment")
        task_id = task["id"]
        data, code = invoke_json("comment", task_id, "JSON comment text", "--actor", "human:test")
        assert code == 0
        assert data["ok"] is True

    def test_quiet_output(self, create_task, invoke):
        task = create_task("Quiet comment")
        task_id = task["id"]
        result = invoke("comment", task_id, "Quiet text", "--actor", "human:test", "--quiet")
        assert result.exit_code == 0
        assert result.output.strip() == "ok"


# ---------------------------------------------------------------------------
# CrossCutting
# ---------------------------------------------------------------------------


class TestCrossCutting:
    """Cross-cutting tests for event integrity, global log, and JSON envelope."""

    def test_event_exists_in_jsonl(self, create_task, initialized_root):
        """After create, the event log should contain a task_created event."""
        task = create_task("Event exists")
        task_id = task["id"]
        events_path = initialized_root / ".lattice" / "events" / f"{task_id}.jsonl"
        assert events_path.exists()
        lines = events_path.read_text().strip().split("\n")
        assert len(lines) >= 1
        event = json.loads(lines[0])
        assert event["type"] == "task_created"
        assert event["task_id"] == task_id

    def test_lifecycle_log_gets_task_created(self, create_task, initialized_root):
        """task_created events should appear in _lifecycle.jsonl."""
        task = create_task("Lifecycle log test")
        task_id = task["id"]
        lifecycle_path = initialized_root / ".lattice" / "events" / "_lifecycle.jsonl"
        content = lifecycle_path.read_text().strip()
        assert content  # not empty
        events = [json.loads(line) for line in content.split("\n")]
        created_events = [e for e in events if e["type"] == "task_created"]
        assert any(e["task_id"] == task_id for e in created_events)

    def test_lifecycle_log_excludes_non_lifecycle_events(
        self, create_task, invoke, initialized_root
    ):
        """status_changed, field_updated, assignment_changed, comment_added
        should NOT appear in _lifecycle.jsonl."""
        task = create_task("Lifecycle exclusion")
        task_id = task["id"]

        # Perform several non-lifecycle operations
        invoke("status", task_id, "in_planning", "--actor", "human:test")
        invoke("update", task_id, "title=Changed", "--actor", "human:test")
        invoke("assign", task_id, "agent:claude", "--actor", "human:test")
        invoke("comment", task_id, "A note", "--actor", "human:test")

        lifecycle_path = initialized_root / ".lattice" / "events" / "_lifecycle.jsonl"
        content = lifecycle_path.read_text().strip()
        events = [json.loads(line) for line in content.split("\n")]
        for event in events:
            assert event["type"] in {"task_created", "task_archived"}, (
                f"Unexpected event type in lifecycle log: {event['type']}"
            )

    def test_json_error_envelope_format(self, invoke):
        """Error with --json should produce a structured envelope on stdout."""
        result = invoke("create", "Bad", "--priority", "nope", "--actor", "human:test", "--json")
        assert result.exit_code != 0
        parsed = json.loads(result.output)
        assert parsed["ok"] is False
        assert isinstance(parsed["error"], dict)
        assert parsed["error"]["code"] == "VALIDATION_ERROR"
        assert isinstance(parsed["error"]["message"], str)

    def test_snapshot_on_disk_matches_json_output(self, create_task, initialized_root):
        """The snapshot file on disk should be consistent with what --json returns."""
        task = create_task("Disk check")
        task_id = task["id"]
        snap_path = initialized_root / ".lattice" / "tasks" / f"{task_id}.json"
        disk_snapshot = json.loads(snap_path.read_text())
        assert disk_snapshot["id"] == task_id
        assert disk_snapshot["title"] == "Disk check"

    def test_task_not_found(self, invoke):
        """Operating on a non-existent task should fail with NOT_FOUND."""
        fake_id = "task_00000000000000000000000099"
        result = invoke("status", fake_id, "in_planning", "--actor", "human:test", "--json")
        assert result.exit_code != 0
        parsed = json.loads(result.output)
        assert parsed["ok"] is False
        assert parsed["error"]["code"] == "NOT_FOUND"

    def test_multiple_operations_sequence(self, create_task, invoke_json):
        """Create -> update -> status -> assign -> comment: full lifecycle."""
        task = create_task("Lifecycle test")
        task_id = task["id"]

        # Update title
        data, code = invoke_json(
            "update", task_id, "title=Lifecycle updated", "--actor", "human:test"
        )
        assert code == 0
        assert data["data"]["title"] == "Lifecycle updated"

        # Status change
        data, code = invoke_json("status", task_id, "in_planning", "--actor", "human:test")
        assert code == 0
        assert data["data"]["status"] == "in_planning"

        # Assign
        data, code = invoke_json("assign", task_id, "agent:claude", "--actor", "human:test")
        assert code == 0
        assert data["data"]["assigned_to"] == "agent:claude"

        # Comment
        data, code = invoke_json("comment", task_id, "All done", "--actor", "human:test")
        assert code == 0
        assert data["data"]["title"] == "Lifecycle updated"
        assert data["data"]["status"] == "in_planning"
        assert data["data"]["assigned_to"] == "agent:claude"

    def test_event_count_after_lifecycle(self, create_task, invoke, initialized_root):
        """All events should be recorded in the per-task JSONL."""
        task = create_task("Event count")
        task_id = task["id"]
        invoke("status", task_id, "in_planning", "--actor", "human:test")
        invoke("update", task_id, "priority=high", "--actor", "human:test")
        invoke("assign", task_id, "human:bob", "--actor", "human:test")
        invoke("comment", task_id, "Note", "--actor", "human:test")

        events_path = initialized_root / ".lattice" / "events" / f"{task_id}.jsonl"
        lines = events_path.read_text().strip().split("\n")
        # 1 create + 1 status + 1 update + 1 assign + 1 comment = 5
        assert len(lines) == 5
        types = [json.loads(line)["type"] for line in lines]
        assert types == [
            "task_created",
            "status_changed",
            "field_updated",
            "assignment_changed",
            "comment_added",
        ]


# ---------------------------------------------------------------------------
# TestProvenance
# ---------------------------------------------------------------------------


class TestProvenance:
    """Tests for deep attribution (provenance) flags on write commands."""

    def test_create_with_provenance_flags(self, invoke_json, cli_env):
        from pathlib import Path

        data, code = invoke_json(
            "create",
            "Provenance test",
            "--actor",
            "human:test",
            "--triggered-by",
            "ev_EXAMPLE",
            "--on-behalf-of",
            "human:atin",
            "--reason",
            "Testing provenance",
        )
        assert code == 0
        task_id = data["data"]["id"]

        # Read event log
        lattice_dir = Path(cli_env["LATTICE_ROOT"]) / ".lattice"
        events_file = lattice_dir / "events" / f"{task_id}.jsonl"
        events = [
            json.loads(line) for line in events_file.read_text().splitlines() if line.strip()
        ]
        create_event = events[0]

        assert "provenance" in create_event
        assert create_event["provenance"]["triggered_by"] == "ev_EXAMPLE"
        assert create_event["provenance"]["on_behalf_of"] == "human:atin"
        assert create_event["provenance"]["reason"] == "Testing provenance"

    def test_on_behalf_of_bad_format_errors(self, invoke):
        result = invoke(
            "create",
            "Bad on-behalf-of",
            "--actor",
            "human:test",
            "--on-behalf-of",
            "not_valid_actor",
        )
        assert result.exit_code != 0
        assert "Invalid actor" in result.output or "Invalid actor" in (result.stderr or "")

    def test_status_force_reason_in_both(self, create_task, invoke, cli_env):
        """--force --reason puts reason in data AND provenance."""
        from pathlib import Path

        task = create_task("Force reason test")
        task_id = task["id"]
        result = invoke(
            "status",
            task_id,
            "done",
            "--force",
            "--reason",
            "Skip for release",
            "--actor",
            "human:test",
        )
        assert result.exit_code == 0

        lattice_dir = Path(cli_env["LATTICE_ROOT"]) / ".lattice"
        events_file = lattice_dir / "events" / f"{task_id}.jsonl"
        events = [
            json.loads(line) for line in events_file.read_text().splitlines() if line.strip()
        ]
        status_event = events[-1]

        assert status_event["type"] == "status_changed"
        # Backward compat: reason in data
        assert status_event["data"]["reason"] == "Skip for release"
        assert status_event["data"]["force"] is True
        # Also in provenance
        assert "provenance" in status_event
        assert status_event["provenance"]["reason"] == "Skip for release"

    def test_status_reason_no_force(self, create_task, invoke, cli_env):
        """--reason without --force puts reason only in provenance, not data."""
        from pathlib import Path

        task = create_task("Reason no force")
        task_id = task["id"]
        # backlog -> in_planning is a valid transition, no --force needed
        result = invoke(
            "status",
            task_id,
            "in_planning",
            "--reason",
            "Sprint planning",
            "--actor",
            "human:test",
        )
        assert result.exit_code == 0

        lattice_dir = Path(cli_env["LATTICE_ROOT"]) / ".lattice"
        events_file = lattice_dir / "events" / f"{task_id}.jsonl"
        events = [
            json.loads(line) for line in events_file.read_text().splitlines() if line.strip()
        ]
        status_event = events[-1]

        assert status_event["type"] == "status_changed"
        # No force => reason NOT in data
        assert "reason" not in status_event["data"]
        assert "force" not in status_event["data"]
        # But IS in provenance
        assert "provenance" in status_event
        assert status_event["provenance"]["reason"] == "Sprint planning"

    def test_all_three_flags_on_same_command(self, create_task, invoke, cli_env):
        """Combined --triggered-by, --on-behalf-of, --reason on a single command."""
        from pathlib import Path

        task = create_task("All flags")
        task_id = task["id"]
        result = invoke(
            "comment",
            task_id,
            "A comment",
            "--actor",
            "agent:claude",
            "--triggered-by",
            "ev_PARENT123",
            "--on-behalf-of",
            "human:atin",
            "--reason",
            "Delegated task",
        )
        assert result.exit_code == 0

        lattice_dir = Path(cli_env["LATTICE_ROOT"]) / ".lattice"
        events_file = lattice_dir / "events" / f"{task_id}.jsonl"
        events = [
            json.loads(line) for line in events_file.read_text().splitlines() if line.strip()
        ]
        comment_event = events[-1]

        assert comment_event["type"] == "comment_added"
        assert "provenance" in comment_event
        assert comment_event["provenance"]["triggered_by"] == "ev_PARENT123"
        assert comment_event["provenance"]["on_behalf_of"] == "human:atin"
        assert comment_event["provenance"]["reason"] == "Delegated task"
