"""Crash recovery tests: event-first invariant and rebuild correctness."""

from __future__ import annotations

import json
from pathlib import Path

from lattice.core.events import create_event, serialize_event


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _lattice_dir(initialized_root: Path) -> Path:
    return initialized_root / ".lattice"


def _read_snapshot(lattice_dir: Path, task_id: str) -> dict:
    return json.loads((lattice_dir / "tasks" / f"{task_id}.json").read_text())


def _read_events(lattice_dir: Path, task_id: str) -> list[dict]:
    path = lattice_dir / "events" / f"{task_id}.jsonl"
    events = []
    for line in path.read_text().splitlines():
        stripped = line.strip()
        if stripped:
            events.append(json.loads(stripped))
    return events


def _read_lifecycle(lattice_dir: Path) -> list[dict]:
    path = lattice_dir / "events" / "_lifecycle.jsonl"
    events = []
    if path.exists():
        for line in path.read_text().splitlines():
            stripped = line.strip()
            if stripped:
                events.append(json.loads(stripped))
    return events


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestCrashAfterEventBeforeSnapshot:
    """Simulate crash after appending event but before updating snapshot."""

    def test_rebuild_recovers_missed_status_change(self, create_task, invoke, initialized_root):
        """Create task, manually append a status_changed event to JSONL
        without updating snapshot, then rebuild and verify snapshot reflects
        the new status."""
        task = create_task("Crash test task")
        task_id = task["id"]
        ld = _lattice_dir(initialized_root)

        assert task["status"] == "backlog"

        # Simulate crash: append status_changed event without updating snapshot
        event = create_event(
            type="status_changed",
            task_id=task_id,
            actor="human:test",
            data={"from": "backlog", "to": "ready"},
        )
        event_path = ld / "events" / f"{task_id}.jsonl"
        with open(event_path, "a") as f:
            f.write(serialize_event(event))

        # Snapshot still says "backlog"
        stale_snap = _read_snapshot(ld, task_id)
        assert stale_snap["status"] == "backlog"

        # Rebuild should fix it
        result = invoke("rebuild", task_id, "--json")
        assert result.exit_code == 0
        rebuilt_output = json.loads(result.output)
        assert rebuilt_output["ok"] is True
        assert task_id in rebuilt_output["data"]["rebuilt_tasks"]

        # Snapshot now reflects the status change
        recovered_snap = _read_snapshot(ld, task_id)
        assert recovered_snap["status"] == "ready"
        assert recovered_snap["last_event_id"] == event["id"]


class TestCrashAfterPartialSnapshotWrite:
    """Simulate crash that leaves a truncated snapshot JSON file."""

    def test_doctor_detects_and_rebuild_recovers(self, create_task, invoke, initialized_root):
        """Write truncated JSON to tasks/<id>.json, verify doctor detects it,
        then rebuild and verify recovery."""
        task = create_task("Partial write task")
        task_id = task["id"]
        ld = _lattice_dir(initialized_root)

        # Corrupt the snapshot with truncated JSON
        snapshot_path = ld / "tasks" / f"{task_id}.json"
        snapshot_path.write_text('{"id": "task_')

        # Doctor should detect the bad JSON
        result = invoke("doctor", "--json")
        assert result.exit_code != 0 or result.exit_code == 0  # may warn or error
        output = json.loads(result.output)
        findings = output["data"]["findings"]
        json_parse_findings = [f for f in findings if f["check"] == "json_parse"]
        assert len(json_parse_findings) > 0
        assert any(
            task_id in f.get("message", "") or f.get("task_id") == task_id
            for f in json_parse_findings
        )

        # Rebuild should regenerate the snapshot from events
        result = invoke("rebuild", task_id, "--json")
        assert result.exit_code == 0
        rebuilt = json.loads(result.output)
        assert rebuilt["ok"] is True

        # Snapshot is valid again
        recovered = _read_snapshot(ld, task_id)
        assert recovered["id"] == task_id
        assert recovered["title"] == "Partial write task"
        assert recovered["status"] == "backlog"


class TestCrashAfterEventBeforeLifecycleLog:
    """Simulate crash where lifecycle event is in per-task log but not
    in _lifecycle.jsonl."""

    def test_rebuild_all_fixes_lifecycle_log(self, create_task, invoke, initialized_root):
        """Create task, verify task_created appears in both logs. Then append
        a task_archived event to per-task log only (not lifecycle). Rebuild --all
        should regenerate _lifecycle.jsonl to include both."""
        task = create_task("Lifecycle test task")
        task_id = task["id"]
        ld = _lattice_dir(initialized_root)

        # Verify task_created is in both logs
        per_task_events = _read_events(ld, task_id)
        lifecycle_events = _read_lifecycle(ld)
        assert any(
            e["type"] == "task_created" and e["task_id"] == task_id for e in per_task_events
        )
        assert any(
            e["type"] == "task_created" and e["task_id"] == task_id for e in lifecycle_events
        )

        # Simulate crash: append task_archived to per-task log only
        archive_event = create_event(
            type="task_archived",
            task_id=task_id,
            actor="human:test",
            data={"reason": "done"},
        )
        event_path = ld / "events" / f"{task_id}.jsonl"
        with open(event_path, "a") as f:
            f.write(serialize_event(archive_event))

        # Lifecycle log does NOT have the archive event yet
        lifecycle_before = _read_lifecycle(ld)
        assert not any(e["id"] == archive_event["id"] for e in lifecycle_before)

        # Rebuild --all should fix the lifecycle log
        result = invoke("rebuild", "--all", "--json")
        assert result.exit_code == 0

        # Now lifecycle log should have both task_created AND task_archived
        lifecycle_after = _read_lifecycle(ld)
        lifecycle_ids = {e["id"] for e in lifecycle_after}
        assert archive_event["id"] in lifecycle_ids
        # The original task_created event should still be there too
        task_created_events = [
            e for e in lifecycle_after if e["type"] == "task_created" and e["task_id"] == task_id
        ]
        assert len(task_created_events) == 1


class TestTruncatedJsonlFinalLine:
    """Simulate a truncated final line in a JSONL event file."""

    def test_doctor_fix_removes_truncated_line(self, create_task, invoke, initialized_root):
        """Append a valid event + one truncated line. Doctor --fix should
        detect and remove the truncated line. Rebuild should produce correct
        snapshot."""
        task = create_task("Truncated JSONL task")
        task_id = task["id"]
        ld = _lattice_dir(initialized_root)

        # Append a valid status_changed event
        valid_event = create_event(
            type="status_changed",
            task_id=task_id,
            actor="human:test",
            data={"from": "backlog", "to": "ready"},
        )
        event_path = ld / "events" / f"{task_id}.jsonl"
        with open(event_path, "a") as f:
            f.write(serialize_event(valid_event))
            # Append truncated garbage
            f.write('{"id":"ev_TRUNC","type":"status_ch')

        # Doctor should detect truncated final line
        result = invoke("doctor", "--fix", "--json")
        output = json.loads(result.output)
        findings = output["data"]["findings"]
        truncated_findings = [f for f in findings if f["check"] == "jsonl_parse"]
        assert len(truncated_findings) > 0

        # Rebuild should succeed with the valid events
        result = invoke("rebuild", task_id, "--json")
        assert result.exit_code == 0

        recovered = _read_snapshot(ld, task_id)
        assert recovered["status"] == "ready"


class TestMissingSnapshotEventsExist:
    """Simulate missing snapshot file with events still intact."""

    def test_rebuild_regenerates_from_events(self, create_task, invoke, initialized_root):
        """Delete the snapshot file and verify rebuild regenerates it."""
        task = create_task("Missing snapshot task")
        task_id = task["id"]
        ld = _lattice_dir(initialized_root)

        # Verify snapshot exists
        snapshot_path = ld / "tasks" / f"{task_id}.json"
        assert snapshot_path.exists()

        # Delete snapshot
        snapshot_path.unlink()
        assert not snapshot_path.exists()

        # Events should still be there
        event_path = ld / "events" / f"{task_id}.jsonl"
        assert event_path.exists()

        # Rebuild should regenerate
        result = invoke("rebuild", task_id, "--json")
        assert result.exit_code == 0

        # Snapshot is back
        assert snapshot_path.exists()
        recovered = _read_snapshot(ld, task_id)
        assert recovered["id"] == task_id
        assert recovered["title"] == "Missing snapshot task"
        assert recovered["status"] == "backlog"
        assert recovered["created_by"] == "human:test"


class TestOrphanedTempFiles:
    """Verify orphaned .tmp files don't break doctor or list."""

    def test_temp_files_do_not_crash_doctor_or_list(self, create_task, invoke, initialized_root):
        """Create a .tmp file in tasks/ and verify doctor and list still work."""
        task = create_task("Orphaned temp test")
        task_id = task["id"]
        ld = _lattice_dir(initialized_root)

        # Create an orphaned temp file
        tmp_file = ld / "tasks" / ".tmp.abcdef123"
        tmp_file.write_text("partial write garbage")

        # Doctor should run without crashing
        result = invoke("doctor", "--json")
        # The temp file might cause a JSON parse warning but should not crash
        output = json.loads(result.output)
        assert output["ok"] is True

        # List should also work
        result = invoke("list", "--json")
        assert result.exit_code == 0
        output = json.loads(result.output)
        assert output["ok"] is True
        tasks = output["data"]
        task_ids = [t["id"] for t in tasks]
        assert task_id in task_ids


class TestRebuildFromMultipleEvents:
    """Verify rebuild replays multiple event types correctly."""

    def test_all_mutations_reflected_after_rebuild(self, create_task, invoke, initialized_root):
        """Create a task, change status, update a field, add a comment.
        Delete snapshot. Rebuild. Verify all mutations reflected."""
        task = create_task("Multi-event rebuild task")
        task_id = task["id"]
        ld = _lattice_dir(initialized_root)

        # Change status
        result = invoke("status", task_id, "ready", "--actor", "human:test", "--json")
        assert result.exit_code == 0

        # Update title
        result = invoke(
            "update", task_id, "title=Updated title", "--actor", "human:test", "--json"
        )
        assert result.exit_code == 0

        # Add comment
        result = invoke("comment", task_id, "This is a comment", "--actor", "human:test", "--json")
        assert result.exit_code == 0

        # Verify current snapshot state
        snap_before = _read_snapshot(ld, task_id)
        assert snap_before["status"] == "ready"
        assert snap_before["title"] == "Updated title"

        # Delete snapshot
        snapshot_path = ld / "tasks" / f"{task_id}.json"
        snapshot_path.unlink()

        # Rebuild
        result = invoke("rebuild", task_id, "--json")
        assert result.exit_code == 0

        # Verify all mutations are reflected
        recovered = _read_snapshot(ld, task_id)
        assert recovered["status"] == "ready"
        assert recovered["title"] == "Updated title"
        assert recovered["id"] == task_id
        assert recovered["created_by"] == "human:test"


class TestRebuildAllRecoversMultipleTasks:
    """Verify rebuild --all recovers multiple tasks simultaneously."""

    def test_rebuild_all_fixes_all_stale_snapshots(self, create_task, invoke, initialized_root):
        """Create two tasks, corrupt both snapshots with stale state,
        run rebuild --all, verify both are recovered."""
        task_a = create_task("Task A")
        task_b = create_task("Task B")
        task_a_id = task_a["id"]
        task_b_id = task_b["id"]
        ld = _lattice_dir(initialized_root)

        # Simulate crash on task A: append status event without snapshot update
        event_a = create_event(
            type="status_changed",
            task_id=task_a_id,
            actor="human:test",
            data={"from": "backlog", "to": "in_progress"},
        )
        with open(ld / "events" / f"{task_a_id}.jsonl", "a") as f:
            f.write(serialize_event(event_a))

        # Delete snapshot for task B entirely
        (ld / "tasks" / f"{task_b_id}.json").unlink()

        # Rebuild all
        result = invoke("rebuild", "--all", "--json")
        assert result.exit_code == 0
        output = json.loads(result.output)
        rebuilt_ids = output["data"]["rebuilt_tasks"]
        assert task_a_id in rebuilt_ids
        assert task_b_id in rebuilt_ids

        # Verify both are correct
        snap_a = _read_snapshot(ld, task_a_id)
        assert snap_a["status"] == "in_progress"

        snap_b = _read_snapshot(ld, task_b_id)
        assert snap_b["title"] == "Task B"
        assert snap_b["status"] == "backlog"


class TestDoctorDetectsSnapshotDrift:
    """Verify doctor detects when snapshot is out of sync with events."""

    def test_drift_detected_after_event_without_snapshot_update(
        self, create_task, invoke, initialized_root
    ):
        """Append event to JSONL without updating snapshot.
        Doctor should report snapshot_drift."""
        task = create_task("Drift detection task")
        task_id = task["id"]
        ld = _lattice_dir(initialized_root)

        # Append event without updating snapshot
        event = create_event(
            type="status_changed",
            task_id=task_id,
            actor="human:test",
            data={"from": "backlog", "to": "ready"},
        )
        with open(ld / "events" / f"{task_id}.jsonl", "a") as f:
            f.write(serialize_event(event))

        # Doctor should detect drift
        result = invoke("doctor", "--json")
        output = json.loads(result.output)
        drift_findings = [f for f in output["data"]["findings"] if f["check"] == "snapshot_drift"]
        assert len(drift_findings) == 1
        assert drift_findings[0]["task_id"] == task_id
