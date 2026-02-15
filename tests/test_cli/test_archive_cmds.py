"""Tests for the `lattice archive` command."""

from __future__ import annotations

import json


class TestArchive:
    """Tests for `lattice archive`."""

    def test_archive_basic(self, create_task, invoke, initialized_root):
        """Archive moves snapshot and events from active to archive dirs."""
        task = create_task("Archive me")
        task_id = task["id"]

        result = invoke("archive", task_id, "--actor", "human:test")
        assert result.exit_code == 0
        assert "Archived" in result.output

        lattice = initialized_root / ".lattice"

        # Snapshot moved to archive
        assert not (lattice / "tasks" / f"{task_id}.json").exists()
        assert (lattice / "archive" / "tasks" / f"{task_id}.json").exists()

        # Events moved to archive
        assert not (lattice / "events" / f"{task_id}.jsonl").exists()
        assert (lattice / "archive" / "events" / f"{task_id}.jsonl").exists()

    def test_archive_event_in_log(self, create_task, invoke, initialized_root):
        """The archived event log should contain a task_archived event as the last event."""
        task = create_task("Event log check")
        task_id = task["id"]

        invoke("archive", task_id, "--actor", "human:test")

        lattice = initialized_root / ".lattice"
        event_path = lattice / "archive" / "events" / f"{task_id}.jsonl"
        lines = event_path.read_text().strip().split("\n")
        last_event = json.loads(lines[-1])
        assert last_event["type"] == "task_archived"
        assert last_event["task_id"] == task_id
        assert last_event["actor"] == "human:test"

    def test_archive_in_lifecycle_log(self, create_task, invoke, initialized_root):
        """task_archived should appear in _lifecycle.jsonl."""
        task = create_task("Lifecycle log check")
        task_id = task["id"]

        invoke("archive", task_id, "--actor", "human:test")

        lattice = initialized_root / ".lattice"
        lifecycle_path = lattice / "events" / "_lifecycle.jsonl"
        content = lifecycle_path.read_text().strip()
        events = [json.loads(line) for line in content.split("\n")]
        archived_events = [
            e for e in events if e["type"] == "task_archived" and e["task_id"] == task_id
        ]
        assert len(archived_events) == 1

    def test_archive_with_notes(self, create_task, invoke, initialized_root):
        """Notes file should be moved to archive/notes/ when present."""
        task = create_task("Notes test")
        task_id = task["id"]

        lattice = initialized_root / ".lattice"
        notes_path = lattice / "notes" / f"{task_id}.md"
        notes_path.write_text("# Some notes\n\nThese are task notes.\n")

        result = invoke("archive", task_id, "--actor", "human:test")
        assert result.exit_code == 0

        assert not notes_path.exists()
        archived_notes = lattice / "archive" / "notes" / f"{task_id}.md"
        assert archived_notes.exists()
        assert "Some notes" in archived_notes.read_text()

    def test_archive_without_notes(self, create_task, invoke, initialized_root):
        """Archiving a task without notes should not error."""
        task = create_task("No notes")
        task_id = task["id"]

        lattice = initialized_root / ".lattice"
        # Remove the auto-scaffolded notes file to test the no-notes path
        notes_path = lattice / "notes" / f"{task_id}.md"
        if notes_path.exists():
            notes_path.unlink()
        assert not notes_path.exists()

        result = invoke("archive", task_id, "--actor", "human:test")
        assert result.exit_code == 0

    def test_archive_rejects_invalid_task_id(self, invoke):
        """Archiving with a malformed task_id should fail with INVALID_ID."""
        result = invoke("archive", "../../etc/passwd", "--actor", "human:test")
        assert result.exit_code == 1
        assert "INVALID_ID" in result.stderr or "Invalid task ID" in result.stderr

    def test_archive_rejects_invalid_task_id_json(self, invoke):
        """Archive with --json should return INVALID_ID for malformed IDs."""
        result = invoke("archive", "../../etc/passwd", "--actor", "human:test", "--json")
        assert result.exit_code == 1
        parsed = json.loads(result.output)
        assert parsed["ok"] is False
        assert parsed["error"]["code"] == "INVALID_ID"

    def test_archive_not_found(self, invoke):
        """Archiving a non-existent task should fail with NOT_FOUND."""
        fake_id = "task_00000000000000000000000099"
        result = invoke("archive", fake_id, "--actor", "human:test", "--json")
        assert result.exit_code != 0
        parsed = json.loads(result.output)
        assert parsed["ok"] is False
        assert parsed["error"]["code"] == "NOT_FOUND"

    def test_archive_already_archived(self, create_task, invoke):
        """Archiving an already-archived task should fail with CONFLICT."""
        task = create_task("Double archive")
        task_id = task["id"]

        r1 = invoke("archive", task_id, "--actor", "human:test")
        assert r1.exit_code == 0

        r2 = invoke("archive", task_id, "--actor", "human:test", "--json")
        assert r2.exit_code != 0
        parsed = json.loads(r2.output)
        assert parsed["ok"] is False
        assert parsed["error"]["code"] == "CONFLICT"
        assert "already archived" in parsed["error"]["message"]

    def test_archive_not_in_list(self, create_task, invoke, invoke_json):
        """Archived tasks should not appear in `lattice list`."""
        task1 = create_task("Keep me")
        task2 = create_task("Archive me")
        task2_id = task2["id"]

        invoke("archive", task2_id, "--actor", "human:test")

        data, code = invoke_json("list")
        assert code == 0
        task_ids = [t["id"] for t in data["data"]]
        assert task1["id"] in task_ids
        assert task2_id not in task_ids

    def test_archive_show_finds_archived(self, create_task, invoke, invoke_json):
        """lattice show should find archived tasks."""
        task = create_task("Show archived")
        task_id = task["id"]

        invoke("archive", task_id, "--actor", "human:test")

        data, code = invoke_json("show", task_id)
        assert code == 0
        assert data["ok"] is True
        assert data["data"]["id"] == task_id
        assert data["data"]["archived"] is True

    def test_archive_json_output(self, create_task, invoke_json):
        """Archive with --json should return ok:true envelope."""
        task = create_task("JSON archive")
        task_id = task["id"]

        data, code = invoke_json("archive", task_id, "--actor", "human:test")
        assert code == 0
        assert data["ok"] is True
        assert data["data"]["type"] == "task_archived"
        assert data["data"]["task_id"] == task_id

    def test_archive_quiet_output(self, create_task, invoke):
        """Archive with --quiet should print only the task_id."""
        task = create_task("Quiet archive")
        task_id = task["id"]

        result = invoke("archive", task_id, "--actor", "human:test", "--quiet")
        assert result.exit_code == 0
        assert result.output.strip() == task_id

    def test_archive_artifacts_not_moved(self, create_task, invoke, initialized_root):
        """Artifact files should remain in artifacts/ after archiving."""
        task = create_task("Artifact test")
        task_id = task["id"]

        lattice = initialized_root / ".lattice"

        # Create a dummy source file and attach it
        src_file = initialized_root / "test_artifact.txt"
        src_file.write_text("artifact content")

        # Attach the artifact
        attach_result = invoke(
            "attach",
            task_id,
            str(src_file),
            "--title",
            "Test artifact",
            "--actor",
            "human:test",
            "--json",
        )
        assert attach_result.exit_code == 0
        attach_data = json.loads(attach_result.output)
        art_id = attach_data["data"]["id"]

        # Verify artifact files exist before archive
        assert (lattice / "artifacts" / "meta" / f"{art_id}.json").exists()
        payload_files = list((lattice / "artifacts" / "payload").glob(f"{art_id}.*"))
        assert len(payload_files) >= 1

        # Archive the task
        result = invoke("archive", task_id, "--actor", "human:test")
        assert result.exit_code == 0

        # Artifact files should still be in artifacts/ (not moved)
        assert (lattice / "artifacts" / "meta" / f"{art_id}.json").exists()
        payload_files_after = list((lattice / "artifacts" / "payload").glob(f"{art_id}.*"))
        assert len(payload_files_after) >= 1


class TestUnarchive:
    """Tests for `lattice unarchive`."""

    def test_unarchive_basic(self, create_task, invoke, initialized_root):
        """Unarchive moves snapshot and events from archive back to active dirs."""
        task = create_task("Unarchive me")
        task_id = task["id"]

        invoke("archive", task_id, "--actor", "human:test")

        result = invoke("unarchive", task_id, "--actor", "human:test")
        assert result.exit_code == 0
        assert "Unarchived" in result.output

        lattice = initialized_root / ".lattice"

        # Snapshot back in active
        assert (lattice / "tasks" / f"{task_id}.json").exists()
        assert not (lattice / "archive" / "tasks" / f"{task_id}.json").exists()

        # Events back in active
        assert (lattice / "events" / f"{task_id}.jsonl").exists()
        assert not (lattice / "archive" / "events" / f"{task_id}.jsonl").exists()

    def test_unarchive_event_in_log(self, create_task, invoke, initialized_root):
        """The event log should contain a task_unarchived event as the last event."""
        task = create_task("Event log check")
        task_id = task["id"]

        invoke("archive", task_id, "--actor", "human:test")
        invoke("unarchive", task_id, "--actor", "human:test")

        lattice = initialized_root / ".lattice"
        event_path = lattice / "events" / f"{task_id}.jsonl"
        lines = event_path.read_text().strip().split("\n")
        last_event = json.loads(lines[-1])
        assert last_event["type"] == "task_unarchived"
        assert last_event["task_id"] == task_id
        assert last_event["actor"] == "human:test"

    def test_unarchive_in_lifecycle_log(self, create_task, invoke, initialized_root):
        """task_unarchived should appear in _lifecycle.jsonl."""
        task = create_task("Lifecycle log check")
        task_id = task["id"]

        invoke("archive", task_id, "--actor", "human:test")
        invoke("unarchive", task_id, "--actor", "human:test")

        lattice = initialized_root / ".lattice"
        lifecycle_path = lattice / "events" / "_lifecycle.jsonl"
        content = lifecycle_path.read_text().strip()
        events = [json.loads(line) for line in content.split("\n")]
        unarchived_events = [
            e for e in events if e["type"] == "task_unarchived" and e["task_id"] == task_id
        ]
        assert len(unarchived_events) == 1

    def test_unarchive_with_notes(self, create_task, invoke, initialized_root):
        """Notes file should be moved back from archive/notes/ when present."""
        task = create_task("Notes test")
        task_id = task["id"]

        lattice = initialized_root / ".lattice"
        notes_path = lattice / "notes" / f"{task_id}.md"
        notes_path.write_text("# Some notes\n\nThese are task notes.\n")

        invoke("archive", task_id, "--actor", "human:test")

        # Verify notes are in archive
        assert not notes_path.exists()
        assert (lattice / "archive" / "notes" / f"{task_id}.md").exists()

        result = invoke("unarchive", task_id, "--actor", "human:test")
        assert result.exit_code == 0

        # Notes should be back in active
        assert notes_path.exists()
        assert "Some notes" in notes_path.read_text()
        assert not (lattice / "archive" / "notes" / f"{task_id}.md").exists()

    def test_unarchive_not_found(self, invoke):
        """Unarchiving a non-existent task should fail with NOT_FOUND."""
        fake_id = "task_00000000000000000000000099"
        result = invoke("unarchive", fake_id, "--actor", "human:test", "--json")
        assert result.exit_code != 0
        parsed = json.loads(result.output)
        assert parsed["ok"] is False
        assert parsed["error"]["code"] == "NOT_FOUND"

    def test_unarchive_already_active(self, create_task, invoke):
        """Unarchiving an already-active task should fail with CONFLICT."""
        task = create_task("Already active")
        task_id = task["id"]

        result = invoke("unarchive", task_id, "--actor", "human:test", "--json")
        assert result.exit_code != 0
        parsed = json.loads(result.output)
        assert parsed["ok"] is False
        assert parsed["error"]["code"] == "CONFLICT"
        assert "already active" in parsed["error"]["message"]

    def test_unarchive_in_list(self, create_task, invoke, invoke_json):
        """Unarchived tasks should reappear in `lattice list`."""
        task = create_task("List me again")
        task_id = task["id"]

        invoke("archive", task_id, "--actor", "human:test")

        # Verify not in list
        data, code = invoke_json("list")
        assert code == 0
        task_ids = [t["id"] for t in data["data"]]
        assert task_id not in task_ids

        invoke("unarchive", task_id, "--actor", "human:test")

        # Verify back in list
        data, code = invoke_json("list")
        assert code == 0
        task_ids = [t["id"] for t in data["data"]]
        assert task_id in task_ids

    def test_unarchive_json_output(self, create_task, invoke, invoke_json):
        """Unarchive with --json should return ok:true envelope."""
        task = create_task("JSON unarchive")
        task_id = task["id"]

        invoke("archive", task_id, "--actor", "human:test")

        data, code = invoke_json("unarchive", task_id, "--actor", "human:test")
        assert code == 0
        assert data["ok"] is True
        assert data["data"]["type"] == "task_unarchived"
        assert data["data"]["task_id"] == task_id

    def test_unarchive_quiet_output(self, create_task, invoke):
        """Unarchive with --quiet should print only the task_id."""
        task = create_task("Quiet unarchive")
        task_id = task["id"]

        invoke("archive", task_id, "--actor", "human:test")

        result = invoke("unarchive", task_id, "--actor", "human:test", "--quiet")
        assert result.exit_code == 0
        assert result.output.strip() == task_id

    def test_unarchive_rejects_invalid_task_id(self, invoke):
        """Unarchiving with a malformed task_id should fail with INVALID_ID."""
        result = invoke("unarchive", "../../etc/passwd", "--actor", "human:test")
        assert result.exit_code == 1
        assert "INVALID_ID" in result.stderr or "Invalid task ID" in result.stderr
