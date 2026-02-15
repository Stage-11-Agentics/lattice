"""Edge-case tests: relationships, archive round-trips, and doctor completeness."""

from __future__ import annotations

import json


# ---------------------------------------------------------------------------
# Relationships
# ---------------------------------------------------------------------------


class TestBidirectionalRelationshipDisplay:
    """Linking A blocks B should appear in A's outgoing and B's incoming."""

    def test_bidirectional_relationship_display(self, invoke, invoke_json, create_task):
        task_a = create_task("Task A")
        task_b = create_task("Task B")
        a_id = task_a["id"]
        b_id = task_b["id"]

        # Create link: A blocks B
        result = invoke("link", a_id, "blocks", b_id, "--actor", "human:test")
        assert result.exit_code == 0

        # Show A -- should have blocks in relationships_out
        data_a, code_a = invoke_json("show", a_id)
        assert code_a == 0
        rels_out = data_a["data"]["relationships_out"]
        assert any(r["type"] == "blocks" and r["target_task_id"] == b_id for r in rels_out)

        # Show B -- should have A blocks in relationships_in
        data_b, code_b = invoke_json("show", b_id)
        assert code_b == 0
        rels_in = data_b["data"]["relationships_in"]
        assert any(r["type"] == "blocks" and r["source_task_id"] == a_id for r in rels_in)


class TestSelfLinkRejection:
    """A task cannot link to itself."""

    def test_self_link_rejection(self, invoke, create_task):
        task = create_task("Self-linker")
        task_id = task["id"]

        result = invoke("link", task_id, "blocks", task_id, "--actor", "human:test", "--json")
        assert result.exit_code != 0
        parsed = json.loads(result.output)
        assert parsed["ok"] is False
        assert "Cannot create a relationship from a task to itself" in parsed["error"]["message"]


class TestDuplicateRelationshipRejection:
    """Adding the same relationship twice should produce a CONFLICT error."""

    def test_duplicate_relationship_rejection(self, invoke, create_task):
        task_a = create_task("Dup A")
        task_b = create_task("Dup B")
        a_id = task_a["id"]
        b_id = task_b["id"]

        # First link succeeds
        r1 = invoke("link", a_id, "blocks", b_id, "--actor", "human:test")
        assert r1.exit_code == 0

        # Second identical link should fail
        r2 = invoke("link", a_id, "blocks", b_id, "--actor", "human:test", "--json")
        assert r2.exit_code != 0
        parsed = json.loads(r2.output)
        assert parsed["ok"] is False
        assert parsed["error"]["code"] == "CONFLICT"


class TestMultipleRelationshipTypes:
    """Two different relationship types between the same tasks should both succeed."""

    def test_multiple_relationship_types_between_same_tasks(
        self, invoke, invoke_json, create_task
    ):
        task_a = create_task("Multi A")
        task_b = create_task("Multi B")
        a_id = task_a["id"]
        b_id = task_b["id"]

        r1 = invoke("link", a_id, "blocks", b_id, "--actor", "human:test")
        assert r1.exit_code == 0

        r2 = invoke("link", a_id, "related_to", b_id, "--actor", "human:test")
        assert r2.exit_code == 0

        # Show A should have 2 outgoing relationships
        data, code = invoke_json("show", a_id)
        assert code == 0
        rels_out = data["data"]["relationships_out"]
        assert len(rels_out) == 2
        types = {r["type"] for r in rels_out}
        assert types == {"blocks", "related_to"}


class TestLinkUnlinkRoundtrip:
    """Link then unlink should leave zero outgoing relationships."""

    def test_link_unlink_roundtrip(self, invoke, invoke_json, create_task):
        task_a = create_task("Roundtrip A")
        task_b = create_task("Roundtrip B")
        a_id = task_a["id"]
        b_id = task_b["id"]

        # Link
        invoke("link", a_id, "depends_on", b_id, "--actor", "human:test")

        # Verify relationship exists
        data, _ = invoke_json("show", a_id)
        assert len(data["data"]["relationships_out"]) == 1

        # Unlink
        r = invoke("unlink", a_id, "depends_on", b_id, "--actor", "human:test")
        assert r.exit_code == 0

        # Verify empty
        data, _ = invoke_json("show", a_id)
        assert len(data["data"]["relationships_out"]) == 0


# ---------------------------------------------------------------------------
# Archive
# ---------------------------------------------------------------------------


class TestArchiveFullRoundTrip:
    """Create, modify, then archive — verify files move correctly."""

    def test_archive_full_round_trip(self, invoke, create_task, initialized_root):
        task = create_task("Archive me")
        task_id = task["id"]
        lattice_dir = initialized_root / ".lattice"

        # Modify: change status to ready
        invoke("status", task_id, "ready", "--actor", "human:test")

        # Archive
        result = invoke("archive", task_id, "--actor", "human:test")
        assert result.exit_code == 0

        # Snapshot should be in archive/tasks, not active tasks/
        assert not (lattice_dir / "tasks" / f"{task_id}.json").exists()
        assert (lattice_dir / "archive" / "tasks" / f"{task_id}.json").exists()

        # Events should be in archive/events, not active events/
        assert not (lattice_dir / "events" / f"{task_id}.jsonl").exists()
        assert (lattice_dir / "archive" / "events" / f"{task_id}.jsonl").exists()

        # Verify archived snapshot has updated status
        snap = json.loads((lattice_dir / "archive" / "tasks" / f"{task_id}.json").read_text())
        assert snap["status"] == "ready"


class TestArchiveWithNotes:
    """Notes file should move to archive/notes/ on archive."""

    def test_archive_with_notes(self, invoke, create_task, initialized_root):
        task = create_task("Notes task")
        task_id = task["id"]
        lattice_dir = initialized_root / ".lattice"

        # Create a notes file manually
        notes_path = lattice_dir / "notes" / f"{task_id}.md"
        notes_path.write_text("# Notes\nSome important notes here.\n")

        # Archive
        result = invoke("archive", task_id, "--actor", "human:test")
        assert result.exit_code == 0

        # Notes should have moved
        assert not notes_path.exists()
        archived_notes = lattice_dir / "archive" / "notes" / f"{task_id}.md"
        assert archived_notes.exists()
        assert "Some important notes here." in archived_notes.read_text()


class TestArchivePreservesArtifacts:
    """Artifacts (meta + payload) stay in place when their task is archived."""

    def test_archive_preserves_artifacts(self, invoke, create_task, initialized_root):
        task = create_task("Artifact task")
        task_id = task["id"]
        lattice_dir = initialized_root / ".lattice"

        # Create a temp file to attach
        temp_file = initialized_root / "attachment.txt"
        temp_file.write_text("artifact content")

        # Attach artifact
        result = invoke("attach", task_id, str(temp_file), "--actor", "human:test", "--json")
        assert result.exit_code == 0
        art_data = json.loads(result.output)
        art_id = art_data["data"]["id"]

        # Archive the task
        result = invoke("archive", task_id, "--actor", "human:test")
        assert result.exit_code == 0

        # Artifact metadata and payload should still be in artifacts/
        assert (lattice_dir / "artifacts" / "meta" / f"{art_id}.json").exists()
        # Payload file should still exist
        payload_dir = lattice_dir / "artifacts" / "payload"
        payload_files = list(payload_dir.glob(f"{art_id}.*"))
        assert len(payload_files) == 1


class TestArchiveAlreadyArchivedErrors:
    """Archiving an already-archived task should return CONFLICT."""

    def test_archive_already_archived_errors(self, invoke, create_task):
        task = create_task("Double archive")
        task_id = task["id"]

        # First archive
        r1 = invoke("archive", task_id, "--actor", "human:test")
        assert r1.exit_code == 0

        # Second archive should fail
        r2 = invoke("archive", task_id, "--actor", "human:test", "--json")
        assert r2.exit_code != 0
        parsed = json.loads(r2.output)
        assert parsed["ok"] is False
        assert parsed["error"]["code"] == "CONFLICT"
        assert "already archived" in parsed["error"]["message"]


# ---------------------------------------------------------------------------
# Doctor
# ---------------------------------------------------------------------------


class TestDoctorCleanProject:
    """Doctor on a healthy project should report zero findings."""

    def test_doctor_clean_project_zero_findings(self, invoke, create_task):
        # Create a few tasks with some operations
        t1 = create_task("Clean task 1")
        t2 = create_task("Clean task 2")

        invoke("status", t1["id"], "ready", "--actor", "human:test")
        invoke("link", t1["id"], "blocks", t2["id"], "--actor", "human:test")
        invoke("comment", t2["id"], "A note", "--actor", "human:test")

        # Doctor should find no issues
        result = invoke("doctor", "--json")
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert data["ok"] is True
        assert data["data"]["findings"] == []
        assert data["data"]["summary"]["errors"] == 0
        assert data["data"]["summary"]["warnings"] == 0


class TestDoctorDetectsComprehensiveCorruption:
    """Manually corrupt various aspects and verify doctor catches them all."""

    def test_doctor_detects_comprehensive_corruption(self, invoke, create_task, initialized_root):
        task1 = create_task("Corrupt 1")
        task2 = create_task("Corrupt 2")
        t1_id = task1["id"]
        t2_id = task2["id"]
        lattice_dir = initialized_root / ".lattice"

        # 1. Corrupt snapshot with invalid JSON
        snap_path = lattice_dir / "tasks" / f"{t1_id}.json"
        snap_path.write_text("{invalid json here")

        # 2. Append truncated JSONL line to task2's event log
        event_path = lattice_dir / "events" / f"{t2_id}.jsonl"
        with open(event_path, "a") as f:
            f.write('{"type":"status_changed","data":{"from":"backlog","to":"rea\n')

        # 3. Add self-link to task2 snapshot
        snap2_path = lattice_dir / "tasks" / f"{t2_id}.json"
        snap2 = json.loads(snap2_path.read_text())
        snap2.setdefault("relationships_out", []).append(
            {"type": "blocks", "target_task_id": t2_id}
        )
        snap2_path.write_text(json.dumps(snap2, sort_keys=True, indent=2) + "\n")

        # 4. Add duplicate edge to task2 snapshot
        snap2["relationships_out"].append({"type": "blocks", "target_task_id": t2_id})
        snap2_path.write_text(json.dumps(snap2, sort_keys=True, indent=2) + "\n")

        # 5. Write a file with malformed task ID
        bad_path = lattice_dir / "tasks" / "not_a_valid_id.json"
        bad_path.write_text(
            json.dumps({"id": "not_a_valid_id", "title": "bad"}, sort_keys=True, indent=2) + "\n"
        )

        # Run doctor
        result = invoke("doctor", "--json")
        data = json.loads(result.output)
        assert data["ok"] is True  # doctor always returns ok:true in JSON

        findings = data["data"]["findings"]
        check_types_found = {f["check"] for f in findings}

        # Verify multiple check types detected
        assert "json_parse" in check_types_found, "Should detect invalid JSON"
        assert "jsonl_parse" in check_types_found, "Should detect truncated JSONL"
        assert "self_link" in check_types_found, "Should detect self-link"
        assert "duplicate_edge" in check_types_found, "Should detect duplicate edge"
        assert "malformed_id" in check_types_found, "Should detect malformed ID"


class TestDoctorFixRepairsTruncatedJsonl:
    """Doctor --fix should remove truncated final JSONL lines."""

    def test_doctor_fix_repairs_truncated_jsonl(self, invoke, create_task, initialized_root):
        task = create_task("Fix me")
        task_id = task["id"]
        lattice_dir = initialized_root / ".lattice"

        # Append a truncated line
        event_path = lattice_dir / "events" / f"{task_id}.jsonl"
        with open(event_path, "a") as f:
            f.write('{"type":"trunca\n')

        # Run doctor --fix
        result = invoke("doctor", "--fix", "--json")
        data = json.loads(result.output)
        assert data["ok"] is True

        # Find the jsonl_parse finding, it should say "fixed"
        jsonl_findings = [f for f in data["data"]["findings"] if f["check"] == "jsonl_parse"]
        assert len(jsonl_findings) >= 1
        assert any("fixed" in f["message"].lower() for f in jsonl_findings)

        # Verify the truncated line is gone from the JSONL
        lines = event_path.read_text().strip().split("\n")
        for line in lines:
            stripped = line.strip()
            if stripped:
                # Every remaining line should be valid JSON
                json.loads(stripped)
