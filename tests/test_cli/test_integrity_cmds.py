"""Tests for integrity commands: doctor, rebuild."""

from __future__ import annotations

import json


# ---------------------------------------------------------------------------
# Doctor tests
# ---------------------------------------------------------------------------


class TestDoctor:
    """Tests for `lattice doctor`."""

    def test_doctor_clean_state(self, create_task, invoke):
        """Init project, create a few tasks. Doctor should report all clean."""
        create_task("Task one")
        create_task("Task two")
        create_task("Task three")

        result = invoke("doctor")
        assert result.exit_code == 0
        assert "No issues found" in result.output

    def test_doctor_truncated_jsonl(self, create_task, invoke, initialized_root):
        """Manually append truncated line to event file. Doctor should detect."""
        task = create_task("Truncate test")
        task_id = task["id"]

        # Append a truncated (invalid) line
        event_path = initialized_root / ".lattice" / "events" / f"{task_id}.jsonl"
        with open(event_path, "a") as f:
            f.write('{"incomplete": true, "no_close\n')

        result = invoke("doctor")
        assert result.exit_code == 0  # warnings only, not errors
        assert "Truncated final line" in result.output

    def test_doctor_fix_truncated(self, create_task, invoke, initialized_root):
        """With --fix, truncated final line should be removed."""
        task = create_task("Fix truncate test")
        task_id = task["id"]

        event_path = initialized_root / ".lattice" / "events" / f"{task_id}.jsonl"
        original_content = event_path.read_text()
        original_line_count = len(original_content.strip().split("\n"))

        # Append truncated line
        with open(event_path, "a") as f:
            f.write('{"incomplete": true\n')

        result = invoke("doctor", "--fix")
        assert result.exit_code == 0
        assert "fixed" in result.output

        # Verify the truncated line was removed
        fixed_content = event_path.read_text()
        fixed_lines = [ln for ln in fixed_content.strip().split("\n") if ln.strip()]
        assert len(fixed_lines) == original_line_count

        # Each remaining line should be valid JSON
        for line in fixed_lines:
            json.loads(line)

    def test_doctor_snapshot_drift(self, create_task, invoke, initialized_root):
        """Manually modify last_event_id in snapshot. Doctor should detect drift."""
        task = create_task("Drift test")
        task_id = task["id"]

        snap_path = initialized_root / ".lattice" / "tasks" / f"{task_id}.json"
        snap = json.loads(snap_path.read_text())
        snap["last_event_id"] = "ev_00000000000000000000000000"
        snap_path.write_text(json.dumps(snap, sort_keys=True, indent=2) + "\n")

        result = invoke("doctor")
        assert result.exit_code == 0
        assert "drift" in result.output.lower()
        assert task_id in result.output

    def test_doctor_missing_relationship_target(self, create_task, invoke, initialized_root):
        """Create a task with relationship to non-existent task. Doctor should detect."""
        task = create_task("Rel test")
        task_id = task["id"]

        snap_path = initialized_root / ".lattice" / "tasks" / f"{task_id}.json"
        snap = json.loads(snap_path.read_text())
        snap["relationships_out"] = [
            {
                "type": "blocks",
                "target_task_id": "task_00000000000000000000ZZZZZZ",
                "created_at": "2025-01-01T00:00:00Z",
                "created_by": "human:test",
                "note": None,
            }
        ]
        snap_path.write_text(json.dumps(snap, sort_keys=True, indent=2) + "\n")

        result = invoke("doctor")
        assert result.exit_code == 0
        assert "non-existent target" in result.output

    def test_doctor_self_link(self, create_task, invoke, initialized_root):
        """Add self-referential relationship. Doctor should detect."""
        task = create_task("Self link test")
        task_id = task["id"]

        snap_path = initialized_root / ".lattice" / "tasks" / f"{task_id}.json"
        snap = json.loads(snap_path.read_text())
        snap["relationships_out"] = [
            {
                "type": "blocks",
                "target_task_id": task_id,
                "created_at": "2025-01-01T00:00:00Z",
                "created_by": "human:test",
                "note": None,
            }
        ]
        snap_path.write_text(json.dumps(snap, sort_keys=True, indent=2) + "\n")

        result = invoke("doctor")
        assert result.exit_code == 0
        assert "self-referential" in result.output

    def test_doctor_duplicate_edge(self, create_task, invoke, initialized_root):
        """Add duplicate relationship. Doctor should detect."""
        task_a = create_task("Dupe source")
        task_b = create_task("Dupe target")
        task_a_id = task_a["id"]
        task_b_id = task_b["id"]

        snap_path = initialized_root / ".lattice" / "tasks" / f"{task_a_id}.json"
        snap = json.loads(snap_path.read_text())
        snap["relationships_out"] = [
            {
                "type": "blocks",
                "target_task_id": task_b_id,
                "created_at": "2025-01-01T00:00:00Z",
                "created_by": "human:test",
                "note": None,
            },
            {
                "type": "blocks",
                "target_task_id": task_b_id,
                "created_at": "2025-01-02T00:00:00Z",
                "created_by": "human:test",
                "note": None,
            },
        ]
        snap_path.write_text(json.dumps(snap, sort_keys=True, indent=2) + "\n")

        result = invoke("doctor")
        assert result.exit_code == 0
        assert "duplicate" in result.output.lower()

    def test_doctor_malformed_id(self, create_task, invoke, initialized_root):
        """Create a task file with a bad ID name. Doctor should detect."""
        # Create a well-formed task first, so doctor has something to scan
        create_task("Good task")

        # Write a file with a bad name (not a valid ULID suffix)
        bad_path = initialized_root / ".lattice" / "tasks" / "task_BADID.json"
        bad_snap = {
            "schema_version": 1,
            "id": "task_BADID",
            "title": "Bad ID task",
            "status": "backlog",
            "priority": "medium",
            "type": "task",
            "relationships_out": [],
            "artifact_refs": [],
            "custom_fields": {},
            "last_event_id": "ev_00000000000000000000000000",
        }
        bad_path.write_text(json.dumps(bad_snap, sort_keys=True, indent=2) + "\n")

        result = invoke("doctor")
        assert result.exit_code == 0
        assert "Malformed task ID" in result.output

    def test_doctor_json_output(self, create_task, invoke):
        """Run doctor with --json, verify structured output."""
        create_task("JSON test")

        result = invoke("doctor", "--json")
        assert result.exit_code == 0

        parsed = json.loads(result.output)
        assert parsed["ok"] is True
        assert "findings" in parsed["data"]
        assert "summary" in parsed["data"]
        summary = parsed["data"]["summary"]
        assert "tasks" in summary
        assert "events" in summary
        assert "artifacts" in summary
        assert "warnings" in summary
        assert "errors" in summary
        assert isinstance(parsed["data"]["findings"], list)

    def test_doctor_missing_artifact(self, create_task, invoke, initialized_root):
        """Task has artifact_ref to non-existent artifact. Doctor should detect."""
        task = create_task("Artifact test")
        task_id = task["id"]

        snap_path = initialized_root / ".lattice" / "tasks" / f"{task_id}.json"
        snap = json.loads(snap_path.read_text())
        snap["artifact_refs"] = ["art_00000000000000000000ZZZZZZ"]
        snap_path.write_text(json.dumps(snap, sort_keys=True, indent=2) + "\n")

        result = invoke("doctor")
        assert result.exit_code == 0
        assert "non-existent" in result.output
        assert "artifact" in result.output.lower()

    def test_doctor_global_log_consistency(self, create_task, invoke, initialized_root):
        """Remove event from per-task log that exists in global. Doctor detects."""
        task = create_task("Global consistency test")
        task_id = task["id"]

        # The global log has the task_created event. Clear the per-task log.
        event_path = initialized_root / ".lattice" / "events" / f"{task_id}.jsonl"
        event_path.write_text("")

        result = invoke("doctor")
        assert result.exit_code == 0
        assert "global" in result.output.lower() or "Global" in result.output


# ---------------------------------------------------------------------------
# Rebuild tests
# ---------------------------------------------------------------------------


class TestRebuild:
    """Tests for `lattice rebuild`."""

    def test_rebuild_single(self, create_task, invoke, invoke_json, initialized_root):
        """Create task, corrupt snapshot, rebuild. Verify snapshot restored."""
        task = create_task("Rebuild me")
        task_id = task["id"]

        # Save original snapshot for comparison
        snap_path = initialized_root / ".lattice" / "tasks" / f"{task_id}.json"
        original = snap_path.read_text()

        # Corrupt the snapshot
        snap = json.loads(original)
        snap["title"] = "CORRUPTED"
        snap["last_event_id"] = "ev_00000000000000000000000000"
        snap_path.write_text(json.dumps(snap, sort_keys=True, indent=2) + "\n")

        # Rebuild
        result = invoke("rebuild", task_id)
        assert result.exit_code == 0
        assert "Rebuilt" in result.output

        # Verify snapshot matches original
        rebuilt = snap_path.read_text()
        assert rebuilt == original

    def test_rebuild_all(self, create_task, invoke, initialized_root):
        """Create multiple tasks, rebuild --all. Verify all correct."""
        task1 = create_task("Task one")
        task2 = create_task("Task two")

        # Save original snapshots
        originals = {}
        for t in [task1, task2]:
            snap_path = initialized_root / ".lattice" / "tasks" / f"{t['id']}.json"
            originals[t["id"]] = snap_path.read_text()

        # Corrupt both
        for tid, _orig in originals.items():
            snap_path = initialized_root / ".lattice" / "tasks" / f"{tid}.json"
            snap = json.loads(snap_path.read_text())
            snap["title"] = "CORRUPTED"
            snap_path.write_text(json.dumps(snap, sort_keys=True, indent=2) + "\n")

        # Rebuild all
        result = invoke("rebuild", "--all")
        assert result.exit_code == 0
        assert "Rebuilt" in result.output
        assert "regenerated global log" in result.output

        # Verify snapshots match originals
        for tid, orig in originals.items():
            snap_path = initialized_root / ".lattice" / "tasks" / f"{tid}.json"
            assert snap_path.read_text() == orig

    def test_rebuild_deterministic(self, create_task, invoke, initialized_root):
        """Rebuild same task twice, verify byte-identical output."""
        task = create_task("Deterministic test")
        task_id = task["id"]

        # Rebuild once
        invoke("rebuild", task_id)
        snap_path = initialized_root / ".lattice" / "tasks" / f"{task_id}.json"
        first = snap_path.read_text()

        # Rebuild again
        invoke("rebuild", task_id)
        second = snap_path.read_text()

        assert first == second

    def test_rebuild_fixes_drift(self, create_task, invoke, initialized_root):
        """Modify snapshot's last_event_id, rebuild, then doctor should pass."""
        task = create_task("Drift fix test")
        task_id = task["id"]

        # Introduce drift
        snap_path = initialized_root / ".lattice" / "tasks" / f"{task_id}.json"
        snap = json.loads(snap_path.read_text())
        snap["last_event_id"] = "ev_00000000000000000000000000"
        snap_path.write_text(json.dumps(snap, sort_keys=True, indent=2) + "\n")

        # Confirm doctor detects drift
        result = invoke("doctor")
        assert "drift" in result.output.lower()

        # Rebuild
        result = invoke("rebuild", task_id)
        assert result.exit_code == 0

        # Doctor should now pass
        result = invoke("doctor")
        assert "No issues found" in result.output

    def test_rebuild_not_found(self, invoke):
        """Try to rebuild non-existent task. Should error."""
        fake_id = "task_00000000000000000000000099"
        result = invoke("rebuild", fake_id)
        assert result.exit_code != 0

    def test_rebuild_json_output(self, create_task, invoke):
        """Rebuild with --json, verify structured envelope."""
        task = create_task("JSON rebuild test")
        task_id = task["id"]

        result = invoke("rebuild", task_id, "--json")
        assert result.exit_code == 0

        parsed = json.loads(result.output)
        assert parsed["ok"] is True
        assert task_id in parsed["data"]["rebuilt_tasks"]
        assert parsed["data"]["global_log_rebuilt"] is False

    def test_rebuild_regenerates_global_log(self, create_task, invoke, initialized_root):
        """Rebuild --all regenerates global log with task_created events sorted by (ts, id)."""
        task1 = create_task("Global log task 1")
        task2 = create_task("Global log task 2")

        # Corrupt the global log
        global_path = initialized_root / ".lattice" / "events" / "_global.jsonl"
        global_path.write_text("")

        # Rebuild all
        result = invoke("rebuild", "--all")
        assert result.exit_code == 0

        # Verify global log was regenerated
        content = global_path.read_text().strip()
        assert content  # not empty

        events = [json.loads(line) for line in content.split("\n") if line.strip()]

        # Should contain exactly task_created events for both tasks
        task_ids_in_global = {e["task_id"] for e in events}
        assert task1["id"] in task_ids_in_global
        assert task2["id"] in task_ids_in_global

        # All events should be lifecycle events
        for ev in events:
            assert ev["type"] in {"task_created", "task_archived"}

        # Should be sorted by (ts, id)
        sorted_events = sorted(events, key=lambda e: (e["ts"], e["id"]))
        assert events == sorted_events

    def test_rebuild_all_json_output(self, create_task, invoke):
        """Rebuild --all with --json, verify structured envelope."""
        create_task("All JSON task 1")
        create_task("All JSON task 2")

        result = invoke("rebuild", "--all", "--json")
        assert result.exit_code == 0

        parsed = json.loads(result.output)
        assert parsed["ok"] is True
        assert len(parsed["data"]["rebuilt_tasks"]) == 2
        assert parsed["data"]["global_log_rebuilt"] is True
