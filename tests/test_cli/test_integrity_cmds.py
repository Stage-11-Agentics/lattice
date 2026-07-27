"""Tests for integrity commands: doctor, rebuild."""

from __future__ import annotations

import json
from pathlib import Path

from lattice.core.events import create_event, serialize_event


def _durable_bytes(lattice_dir: Path) -> dict[str, bytes]:
    """Capture every durable project file; lock/runtime files are excluded."""
    return {
        str(path.relative_to(lattice_dir)): path.read_bytes()
        for path in sorted(lattice_dir.rglob("*"))
        if path.is_file()
        and "locks" not in path.parts
        and ".daemon" not in path.parts
        and "review_state" not in path.parts
    }


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

    def test_doctor_detects_full_snapshot_drift_with_matching_last_event(
        self, create_task, invoke, initialized_root
    ):
        task = create_task("Full byte drift")
        task_id = task["id"]
        snap_path = initialized_root / ".lattice" / "tasks" / f"{task_id}.json"
        snapshot = json.loads(snap_path.read_text())
        original_last_event = snapshot["last_event_id"]
        snapshot["acceptance_criteria"] = [
            {
                "id": "AC-forged",
                "outcome": "Not authoritative.",
                "criterion_ids": ["AC-missing"],
            }
        ]
        snap_path.write_text(json.dumps(snapshot, sort_keys=True, indent=2) + "\n")

        result = invoke("doctor")
        assert result.exit_code == 0
        assert "full authoritative replay" in result.output
        assert json.loads(snap_path.read_text())["last_event_id"] == original_last_event

    def test_authoritative_dangling_criterion_link_is_nonrepairable(
        self, create_task, invoke, initialized_root
    ):
        task = create_task("Malformed evidence authority")
        task_id = task["id"]
        snap_path = initialized_root / ".lattice" / "tasks" / f"{task_id}.json"
        original_snapshot = snap_path.read_bytes()
        event_path = initialized_root / ".lattice" / "events" / f"{task_id}.jsonl"
        event = create_event(
            "artifact_attached",
            task_id,
            "human:test",
            {
                "artifact_id": "art_01AAAAAAAAAAAAAAAAAAAAAAAA",
                "criterion_ids": ["AC-404"],
            },
        )
        with event_path.open("a", encoding="utf-8") as handle:
            handle.write(serialize_event(event))

        doctor_result = invoke("doctor")
        assert doctor_result.exit_code != 0
        assert "AC-404 not found" in doctor_result.output
        assert "manual recovery" in doctor_result.output

        rebuild_result = invoke("rebuild", task_id)
        assert rebuild_result.exit_code != 0
        assert "cannot be materialized" in rebuild_result.output
        assert snap_path.read_bytes() == original_snapshot

    def test_event_only_snapshot_is_replay_repairable(self, create_task, invoke, initialized_root):
        task_id = create_task("Missing snapshot")["id"]
        snap_path = initialized_root / ".lattice" / "tasks" / f"{task_id}.json"
        expected = snap_path.read_bytes()
        snap_path.unlink()

        doctor_result = invoke("doctor")
        assert doctor_result.exit_code == 0
        assert "Snapshot drift" in doctor_result.output
        assert invoke("rebuild", task_id).exit_code == 0
        assert snap_path.read_bytes() == expected

    def test_ids_json_is_checked_against_authoritative_creation(
        self, create_task, invoke, initialized_root
    ):
        config_path = initialized_root / ".lattice" / "config.json"
        config = json.loads(config_path.read_text())
        config["project_code"] = "TST"
        config_path.write_text(json.dumps(config, sort_keys=True, indent=2) + "\n")
        task = create_task("Authoritative alias")
        ids_path = initialized_root / ".lattice" / "ids.json"
        index = json.loads(ids_path.read_text())
        original_next = index["next_seqs"]["TST"]
        index["map"] = {}
        ids_path.write_text(json.dumps(index, sort_keys=True, indent=2) + "\n")

        doctor_result = invoke("doctor")
        assert doctor_result.exit_code == 0
        assert "does not map it exactly" in doctor_result.output
        invoke("doctor", "--fix")
        assert json.loads(ids_path.read_text())["map"] == {}

        assert invoke("rebuild", "--all").exit_code == 0
        rebuilt = json.loads(ids_path.read_text())
        assert rebuilt["map"][task["short_id"]] == task["id"]
        assert rebuilt["next_seqs"]["TST"] >= original_next

    def test_malformed_authoritative_short_id_blocks_doctor_and_rebuild_without_writes(
        self, create_task, invoke, initialized_root
    ):
        task = create_task("Malformed authoritative alias")
        lattice_dir = initialized_root / ".lattice"
        config_path = lattice_dir / "config.json"
        config = json.loads(config_path.read_text())
        config["project_code"] = "TST"
        config_path.write_text(json.dumps(config, sort_keys=True, indent=2) + "\n")
        event_path = lattice_dir / "events" / f"{task['id']}.jsonl"
        event = json.loads(event_path.read_text().splitlines()[0])
        event["data"]["short_id"] = "TST-not-a-number"
        event_path.write_text(serialize_event(event), encoding="utf-8")
        ids_path = lattice_dir / "ids.json"
        ids_path.write_text(json.dumps({"schema_version": 2, "map": {}, "next_seqs": {}}) + "\n")
        before = _durable_bytes(lattice_dir)

        doctor = invoke("doctor")
        assert doctor.exit_code != 0
        assert "malformed" in doctor.output.lower()
        assert str(event_path) in doctor.output
        rebuild = invoke("rebuild", "--all")
        assert rebuild.exit_code != 0
        assert "malformed" in rebuild.output.lower()
        assert str(event_path) in rebuild.output
        assert _durable_bytes(lattice_dir) == before

    def test_duplicate_authoritative_short_id_blocks_doctor_and_rebuild_without_writes(
        self, create_task, invoke, initialized_root
    ):
        tasks = [create_task("Duplicate alias A"), create_task("Duplicate alias B")]
        lattice_dir = initialized_root / ".lattice"
        config_path = lattice_dir / "config.json"
        config = json.loads(config_path.read_text())
        config["project_code"] = "TST"
        config_path.write_text(json.dumps(config, sort_keys=True, indent=2) + "\n")
        event_paths: list[Path] = []
        for task in tasks:
            event_path = lattice_dir / "events" / f"{task['id']}.jsonl"
            event = json.loads(event_path.read_text().splitlines()[0])
            event["data"]["short_id"] = "TST-1"
            event_path.write_text(serialize_event(event), encoding="utf-8")
            event_paths.append(event_path)
        ids_path = lattice_dir / "ids.json"
        ids_path.write_text(json.dumps({"schema_version": 2, "map": {}, "next_seqs": {}}) + "\n")
        before = _durable_bytes(lattice_dir)

        doctor = invoke("doctor")
        assert doctor.exit_code != 0
        assert "duplicate authoritative short id" in doctor.output.lower()
        for task in tasks:
            assert task["id"] in doctor.output
        rebuild = invoke("rebuild", "--all")
        assert rebuild.exit_code != 0
        assert "duplicate authoritative short id" in rebuild.output.lower()
        assert _durable_bytes(lattice_dir) == before

    def test_wrong_configured_prefix_blocks_doctor_and_rebuild_without_any_write(
        self, create_task, invoke, initialized_root
    ):
        task = create_task("Wrong authoritative prefix")
        lattice_dir = initialized_root / ".lattice"
        config_path = lattice_dir / "config.json"
        config = json.loads(config_path.read_text())
        config["project_code"] = "TST"
        config_path.write_text(json.dumps(config, sort_keys=True, indent=2) + "\n")
        event_path = lattice_dir / "events" / f"{task['id']}.jsonl"
        event = json.loads(event_path.read_text().splitlines()[0])
        event["data"]["short_id"] = "WRONG-1"
        event_path.write_text(serialize_event(event), encoding="utf-8")
        snapshot_path = lattice_dir / "tasks" / f"{task['id']}.json"
        snapshot_path.write_text('{"sentinel":"snapshot"}\n', encoding="utf-8")
        (lattice_dir / "notes" / f"{task['id']}.md").write_text(
            "sentinel notes\n", encoding="utf-8"
        )
        artifact_path = lattice_dir / "artifacts" / "meta" / "sentinel.json"
        artifact_path.write_text('{"sentinel":"artifact"}\n', encoding="utf-8")
        before = _durable_bytes(lattice_dir)

        doctor = invoke("doctor")
        assert doctor.exit_code != 0
        assert "configured prefix 'TST'" in doctor.output
        rebuild = invoke("rebuild", "--all")
        assert rebuild.exit_code != 0
        assert "configured prefix 'TST'" in rebuild.output
        assert str(event_path) in rebuild.output
        assert _durable_bytes(lattice_dir) == before

    def test_malformed_complete_index_blocks_rebuild_before_any_write(
        self, create_task, invoke, initialized_root
    ):
        task = create_task("Malformed index preflight")
        lattice_dir = initialized_root / ".lattice"
        snapshot_path = lattice_dir / "tasks" / f"{task['id']}.json"
        snapshot_path.write_text('{"sentinel":"snapshot"}\n', encoding="utf-8")
        ids_path = lattice_dir / "ids.json"
        ids_path.write_text('{"map":{"BROKEN":42},"next_seqs":{},"schema_version":2}\n')
        before = _durable_bytes(lattice_dir)

        rebuild = invoke("rebuild", "--all")
        assert rebuild.exit_code != 0
        assert "malformed mapping" in rebuild.output
        assert str(ids_path) in rebuild.output
        assert _durable_bytes(lattice_dir) == before

    def test_unrelated_malformed_index_entry_blocks_ordinary_create_without_writes(
        self, invoke, initialized_root
    ):
        lattice_dir = initialized_root / ".lattice"
        config_path = lattice_dir / "config.json"
        config = json.loads(config_path.read_text())
        config["project_code"] = "TST"
        config_path.write_text(json.dumps(config, sort_keys=True, indent=2) + "\n")
        ids_path = lattice_dir / "ids.json"
        index = {"schema_version": 2, "map": {}, "next_seqs": {}}
        index["map"]["BROKEN"] = 42
        ids_path.write_text(json.dumps(index, sort_keys=True, indent=2) + "\n")
        before_ids = ids_path.read_bytes()
        before_events = sorted((lattice_dir / "events").glob("task_*.jsonl"))

        result = invoke("create", "Must not allocate", "--actor", "human:test")
        assert result.exit_code != 0
        assert "malformed mapping" in result.output.lower()
        assert ids_path.read_bytes() == before_ids
        assert sorted((lattice_dir / "events").glob("task_*.jsonl")) == before_events

    def test_doctor_missing_relationship_target(self, create_task, invoke, initialized_root):
        """Snapshot-only relationship corruption is classified as replayable drift."""
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
        assert "Snapshot drift" in result.output

    def test_doctor_self_link(self, create_task, invoke, initialized_root):
        """Snapshot-only self-link corruption is classified as replayable drift."""
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
        assert "Snapshot drift" in result.output

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
            "evidence_refs": [],
            "custom_fields": {},
            "last_event_id": "ev_00000000000000000000000000",
        }
        bad_path.write_text(json.dumps(bad_snap, sort_keys=True, indent=2) + "\n")

        result = invoke("doctor")
        assert result.exit_code != 0
        assert "Malformed task ID" in result.output
        assert "no authoritative event log" in result.output

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
        """Snapshot-only artifact corruption is classified as replayable drift."""
        task = create_task("Artifact test")
        task_id = task["id"]

        snap_path = initialized_root / ".lattice" / "tasks" / f"{task_id}.json"
        snap = json.loads(snap_path.read_text())
        snap["evidence_refs"] = [
            {"id": "art_00000000000000000000ZZZZZZ", "role": None, "source_type": "artifact"}
        ]
        snap_path.write_text(json.dumps(snap, sort_keys=True, indent=2) + "\n")

        result = invoke("doctor")
        assert result.exit_code == 0
        assert "Snapshot drift" in result.output

    def test_doctor_lifecycle_log_consistency(self, create_task, invoke, initialized_root):
        """Remove event from per-task log that exists in lifecycle log. Doctor detects."""
        task = create_task("Lifecycle consistency test")
        task_id = task["id"]

        # The lifecycle log has the task_created event. Clear the per-task log.
        event_path = initialized_root / ".lattice" / "events" / f"{task_id}.jsonl"
        event_path.write_text("")

        result = invoke("doctor")
        assert result.exit_code != 0
        assert "authoritative" in result.output.lower()
        assert "lifecycle" in result.output.lower() or "Lifecycle" in result.output

    def test_doctor_detects_mismatched_lifecycle_copy(self, create_task, invoke, initialized_root):
        task = create_task("Lifecycle mismatch")
        lifecycle_path = initialized_root / ".lattice" / "events" / "_lifecycle.jsonl"
        events = [
            json.loads(line) for line in lifecycle_path.read_text().splitlines() if line.strip()
        ]
        event = next(item for item in events if item["task_id"] == task["id"])
        event["data"]["title"] = "Mismatched derived copy"
        lifecycle_path.write_text(serialize_event(event), encoding="utf-8")

        result = invoke("doctor")
        assert result.exit_code == 0
        assert "does not match per-task authority" in result.output


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
        assert "regenerated lifecycle log" in result.output

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

    def test_rebuild_uses_latest_event_for_archived_destination(
        self, create_task, invoke, initialized_root
    ):
        task_id = create_task("Archive placement")["id"]
        assert invoke("archive", task_id, "--actor", "human:test").exit_code == 0
        lattice_dir = initialized_root / ".lattice"
        archived_event = lattice_dir / "archive" / "events" / f"{task_id}.jsonl"
        active_event = lattice_dir / "events" / f"{task_id}.jsonl"
        active_event.write_bytes(archived_event.read_bytes())
        archived_event.unlink()

        result = invoke("rebuild", task_id)
        assert result.exit_code == 0, result.output
        assert archived_event.exists()
        assert not active_event.exists()
        assert (lattice_dir / "archive" / "tasks" / f"{task_id}.json").exists()
        assert not (lattice_dir / "tasks" / f"{task_id}.json").exists()

    def test_rebuild_refuses_divergent_event_candidates_without_overwrite(
        self, create_task, invoke, initialized_root
    ):
        task_id = create_task("Divergent authority")["id"]
        lattice_dir = initialized_root / ".lattice"
        active_event = lattice_dir / "events" / f"{task_id}.jsonl"
        archived_event = lattice_dir / "archive" / "events" / f"{task_id}.jsonl"
        archived_event.parent.mkdir(parents=True, exist_ok=True)
        event = json.loads(active_event.read_text().splitlines()[0])
        event["data"]["title"] = "Different authoritative title"
        archived_event.write_text(serialize_event(event), encoding="utf-8")
        snapshot_path = lattice_dir / "tasks" / f"{task_id}.json"
        original_snapshot = snapshot_path.read_bytes()

        doctor_result = invoke("doctor")
        assert doctor_result.exit_code != 0
        assert "diverge" in doctor_result.output

        rebuild_result = invoke("rebuild", task_id)
        assert rebuild_result.exit_code != 0
        assert "diverge" in rebuild_result.output
        assert snapshot_path.read_bytes() == original_snapshot
        assert active_event.exists()
        assert archived_event.exists()

    def test_rebuild_refuses_divergent_plan_before_snapshot_overwrite(
        self, create_task, invoke, initialized_root
    ):
        task_id = create_task("Divergent plan")["id"]
        lattice_dir = initialized_root / ".lattice"
        active_plan = lattice_dir / "plans" / f"{task_id}.md"
        archived_plan = lattice_dir / "archive" / "plans" / f"{task_id}.md"
        archived_plan.parent.mkdir(parents=True, exist_ok=True)
        archived_plan.write_text(active_plan.read_text() + "\nDifferent.\n")
        snapshot_path = lattice_dir / "tasks" / f"{task_id}.json"
        corrupted = json.loads(snapshot_path.read_text())
        corrupted["title"] = "CORRUPTED CACHE"
        snapshot_path.write_text(json.dumps(corrupted, sort_keys=True, indent=2) + "\n")
        corrupted_bytes = snapshot_path.read_bytes()

        result = invoke("rebuild", task_id)
        assert result.exit_code != 0
        assert "supplementary files diverge" in result.output
        assert snapshot_path.read_bytes() == corrupted_bytes
        assert active_plan.exists()
        assert archived_plan.exists()

    def test_rebuild_restores_byte_exact_snapshot_only_criterion_drift(
        self, create_task, invoke, initialized_root
    ):
        task_id = create_task("Criterion cache drift")["id"]
        snap_path = initialized_root / ".lattice" / "tasks" / f"{task_id}.json"
        authoritative_bytes = snap_path.read_bytes()
        snapshot = json.loads(authoritative_bytes)
        snapshot["evidence_refs"] = [
            {
                "id": "ev_forged",
                "role": None,
                "source_type": "comment",
                "criterion_ids": ["AC-404"],
            }
        ]
        snap_path.write_text(json.dumps(snapshot, sort_keys=True, indent=2) + "\n")

        doctor_result = invoke("doctor")
        assert doctor_result.exit_code == 0
        assert "Snapshot drift" in doctor_result.output
        assert invoke("rebuild", task_id).exit_code == 0
        assert snap_path.read_bytes() == authoritative_bytes

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

    def test_rebuild_regenerates_lifecycle_log(self, create_task, invoke, initialized_root):
        """Rebuild --all regenerates lifecycle log with task_created events sorted by (ts, id)."""
        task1 = create_task("Lifecycle log task 1")
        task2 = create_task("Lifecycle log task 2")

        # Corrupt the lifecycle log
        lifecycle_path = initialized_root / ".lattice" / "events" / "_lifecycle.jsonl"
        lifecycle_path.write_text("")

        # Rebuild all
        result = invoke("rebuild", "--all")
        assert result.exit_code == 0

        # Verify lifecycle log was regenerated
        content = lifecycle_path.read_text().strip()
        assert content  # not empty

        events = [json.loads(line) for line in content.split("\n") if line.strip()]

        # Should contain exactly task_created events for both tasks
        task_ids_in_lifecycle = {e["task_id"] for e in events}
        assert task1["id"] in task_ids_in_lifecycle
        assert task2["id"] in task_ids_in_lifecycle

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
