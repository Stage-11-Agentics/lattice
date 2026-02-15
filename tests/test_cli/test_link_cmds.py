"""Tests for lattice link and unlink CLI commands."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from lattice.core.relationships import RELATIONSHIP_TYPES
from lattice.storage.fs import LATTICE_DIR


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _create_two_tasks(invoke) -> tuple[str, str]:
    """Create two tasks and return their IDs."""
    r1 = invoke("create", "Task A", "--actor", "human:test", "--json")
    assert r1.exit_code == 0, f"create Task A failed: {r1.output}"
    id_a = json.loads(r1.output)["data"]["id"]

    r2 = invoke("create", "Task B", "--actor", "human:test", "--json")
    assert r2.exit_code == 0, f"create Task B failed: {r2.output}"
    id_b = json.loads(r2.output)["data"]["id"]

    return id_a, id_b


def _read_snapshot(initialized_root: Path, task_id: str) -> dict:
    """Read a task snapshot directly from disk."""
    path = initialized_root / LATTICE_DIR / "tasks" / f"{task_id}.json"
    return json.loads(path.read_text())


def _read_events(initialized_root: Path, task_id: str) -> list[dict]:
    """Read all events for a task from its JSONL file."""
    path = initialized_root / LATTICE_DIR / "events" / f"{task_id}.jsonl"
    lines = path.read_text().strip().split("\n")
    return [json.loads(line) for line in lines if line]


# ---------------------------------------------------------------------------
# lattice link
# ---------------------------------------------------------------------------


class TestLinkValid:
    """Happy-path tests for lattice link."""

    def test_link_creates_relationship(self, invoke, initialized_root: Path) -> None:
        id_a, id_b = _create_two_tasks(invoke)
        result = invoke("link", id_a, "blocks", id_b, "--actor", "human:test")
        assert result.exit_code == 0
        assert "Linked" in result.output

        # Verify snapshot
        snap = _read_snapshot(initialized_root, id_a)
        assert len(snap["relationships_out"]) == 1
        rel = snap["relationships_out"][0]
        assert rel["type"] == "blocks"
        assert rel["target_task_id"] == id_b
        assert rel["created_by"] == "human:test"
        assert rel["note"] is None

    def test_link_with_note(self, invoke, initialized_root: Path) -> None:
        id_a, id_b = _create_two_tasks(invoke)
        result = invoke(
            "link",
            id_a,
            "blocks",
            id_b,
            "--note",
            "Blocking deploy",
            "--actor",
            "human:test",
        )
        assert result.exit_code == 0

        snap = _read_snapshot(initialized_root, id_a)
        assert snap["relationships_out"][0]["note"] == "Blocking deploy"

    def test_link_appends_event(self, invoke, initialized_root: Path) -> None:
        id_a, id_b = _create_two_tasks(invoke)
        invoke("link", id_a, "blocks", id_b, "--actor", "human:test")

        events = _read_events(initialized_root, id_a)
        rel_events = [e for e in events if e["type"] == "relationship_added"]
        assert len(rel_events) == 1
        assert rel_events[0]["data"]["type"] == "blocks"
        assert rel_events[0]["data"]["target_task_id"] == id_b

    def test_link_updates_snapshot_bookkeeping(self, invoke, initialized_root: Path) -> None:
        id_a, id_b = _create_two_tasks(invoke)
        snap_before = _read_snapshot(initialized_root, id_a)

        invoke("link", id_a, "blocks", id_b, "--actor", "human:test")

        snap_after = _read_snapshot(initialized_root, id_a)
        assert snap_after["last_event_id"] != snap_before["last_event_id"]
        assert snap_after["updated_at"] >= snap_before["updated_at"]

    def test_link_does_not_modify_target_snapshot(self, invoke, initialized_root: Path) -> None:
        """Only the source task's snapshot is modified, not the target's."""
        id_a, id_b = _create_two_tasks(invoke)
        target_before = _read_snapshot(initialized_root, id_b)

        invoke("link", id_a, "blocks", id_b, "--actor", "human:test")

        target_after = _read_snapshot(initialized_root, id_b)
        assert target_after == target_before


class TestLinkAllTypes:
    """All 7 relationship types can be used with link."""

    @pytest.mark.parametrize("rel_type", sorted(RELATIONSHIP_TYPES))
    def test_each_type_succeeds(self, invoke, rel_type: str) -> None:
        id_a, id_b = _create_two_tasks(invoke)
        result = invoke("link", id_a, rel_type, id_b, "--actor", "human:test")
        assert result.exit_code == 0
        assert rel_type in result.output


class TestLinkJsonOutput:
    """--json flag produces structured output."""

    def test_json_success(self, invoke) -> None:
        id_a, id_b = _create_two_tasks(invoke)
        result = invoke("link", id_a, "blocks", id_b, "--actor", "human:test", "--json")
        assert result.exit_code == 0
        parsed = json.loads(result.output)
        assert parsed["ok"] is True
        assert "data" in parsed
        assert len(parsed["data"]["relationships_out"]) == 1

    def test_json_error(self, invoke) -> None:
        id_a, id_b = _create_two_tasks(invoke)
        result = invoke("link", id_a, "invalid_type", id_b, "--actor", "human:test", "--json")
        assert result.exit_code == 1
        parsed = json.loads(result.output)
        assert parsed["ok"] is False
        assert parsed["error"]["code"] == "VALIDATION_ERROR"


class TestLinkQuietOutput:
    """--quiet flag prints only the task ID."""

    def test_quiet_success(self, invoke) -> None:
        id_a, id_b = _create_two_tasks(invoke)
        result = invoke("link", id_a, "blocks", id_b, "--actor", "human:test", "--quiet")
        assert result.exit_code == 0
        assert result.output.strip() == id_a


class TestLinkErrors:
    """Error cases for lattice link."""

    def test_self_link_rejected(self, invoke) -> None:
        id_a, _ = _create_two_tasks(invoke)
        result = invoke("link", id_a, "blocks", id_a, "--actor", "human:test")
        assert result.exit_code == 1
        assert "itself" in result.stderr.lower() or "itself" in result.output.lower()

    def test_self_link_rejected_json(self, invoke) -> None:
        id_a, _ = _create_two_tasks(invoke)
        result = invoke("link", id_a, "blocks", id_a, "--actor", "human:test", "--json")
        assert result.exit_code == 1
        parsed = json.loads(result.output)
        assert parsed["ok"] is False
        assert parsed["error"]["code"] == "VALIDATION_ERROR"

    def test_duplicate_rejected(self, invoke) -> None:
        id_a, id_b = _create_two_tasks(invoke)
        # First link succeeds
        result1 = invoke("link", id_a, "blocks", id_b, "--actor", "human:test")
        assert result1.exit_code == 0

        # Second identical link fails
        result2 = invoke("link", id_a, "blocks", id_b, "--actor", "human:test")
        assert result2.exit_code == 1
        assert "Duplicate" in result2.stderr or "duplicate" in result2.stderr.lower()

    def test_duplicate_rejected_json(self, invoke) -> None:
        id_a, id_b = _create_two_tasks(invoke)
        invoke("link", id_a, "blocks", id_b, "--actor", "human:test")
        result = invoke("link", id_a, "blocks", id_b, "--actor", "human:test", "--json")
        assert result.exit_code == 1
        parsed = json.loads(result.output)
        assert parsed["ok"] is False
        assert parsed["error"]["code"] == "CONFLICT"

    def test_different_type_same_target_allowed(self, invoke) -> None:
        """Same target with different relationship type should succeed."""
        id_a, id_b = _create_two_tasks(invoke)
        r1 = invoke("link", id_a, "blocks", id_b, "--actor", "human:test")
        assert r1.exit_code == 0

        r2 = invoke("link", id_a, "related_to", id_b, "--actor", "human:test")
        assert r2.exit_code == 0

    def test_target_does_not_exist(self, invoke) -> None:
        id_a, _ = _create_two_tasks(invoke)
        fake_target = "task_01ZZZZZZZZZZZZZZZZZZZZZZZZ"
        result = invoke("link", id_a, "blocks", fake_target, "--actor", "human:test")
        assert result.exit_code == 1
        assert "not found" in result.stderr.lower()

    def test_source_does_not_exist(self, invoke) -> None:
        _, id_b = _create_two_tasks(invoke)
        fake_source = "task_01ZZZZZZZZZZZZZZZZZZZZZZZZ"
        result = invoke("link", fake_source, "blocks", id_b, "--actor", "human:test")
        assert result.exit_code == 1
        assert "not found" in result.stderr.lower()

    def test_invalid_relationship_type(self, invoke) -> None:
        id_a, id_b = _create_two_tasks(invoke)
        result = invoke("link", id_a, "parent_of", id_b, "--actor", "human:test")
        assert result.exit_code == 1
        assert "Invalid relationship type" in result.stderr

    def test_invalid_actor(self, invoke) -> None:
        id_a, id_b = _create_two_tasks(invoke)
        result = invoke("link", id_a, "blocks", id_b, "--actor", "bad-format")
        assert result.exit_code == 1
        assert "Invalid actor" in result.stderr


# ---------------------------------------------------------------------------
# lattice unlink
# ---------------------------------------------------------------------------


class TestUnlinkValid:
    """Happy-path tests for lattice unlink."""

    def test_unlink_removes_relationship(self, invoke, initialized_root: Path) -> None:
        id_a, id_b = _create_two_tasks(invoke)
        invoke("link", id_a, "blocks", id_b, "--actor", "human:test")

        result = invoke("unlink", id_a, "blocks", id_b, "--actor", "human:test")
        assert result.exit_code == 0
        assert "Unlinked" in result.output

        snap = _read_snapshot(initialized_root, id_a)
        assert len(snap["relationships_out"]) == 0

    def test_unlink_appends_event(self, invoke, initialized_root: Path) -> None:
        id_a, id_b = _create_two_tasks(invoke)
        invoke("link", id_a, "blocks", id_b, "--actor", "human:test")
        invoke("unlink", id_a, "blocks", id_b, "--actor", "human:test")

        events = _read_events(initialized_root, id_a)
        rm_events = [e for e in events if e["type"] == "relationship_removed"]
        assert len(rm_events) == 1
        assert rm_events[0]["data"]["type"] == "blocks"
        assert rm_events[0]["data"]["target_task_id"] == id_b

    def test_unlink_updates_snapshot_bookkeeping(self, invoke, initialized_root: Path) -> None:
        id_a, id_b = _create_two_tasks(invoke)
        invoke("link", id_a, "blocks", id_b, "--actor", "human:test")
        snap_before = _read_snapshot(initialized_root, id_a)

        invoke("unlink", id_a, "blocks", id_b, "--actor", "human:test")
        snap_after = _read_snapshot(initialized_root, id_a)
        assert snap_after["last_event_id"] != snap_before["last_event_id"]

    def test_unlink_preserves_other_relationships(self, invoke, initialized_root: Path) -> None:
        """Unlinking one relationship does not affect others."""
        id_a, id_b = _create_two_tasks(invoke)
        r3 = invoke("create", "Task C", "--actor", "human:test", "--json")
        id_c = json.loads(r3.output)["data"]["id"]

        invoke("link", id_a, "blocks", id_b, "--actor", "human:test")
        invoke("link", id_a, "related_to", id_c, "--actor", "human:test")

        invoke("unlink", id_a, "blocks", id_b, "--actor", "human:test")

        snap = _read_snapshot(initialized_root, id_a)
        assert len(snap["relationships_out"]) == 1
        assert snap["relationships_out"][0]["type"] == "related_to"
        assert snap["relationships_out"][0]["target_task_id"] == id_c


class TestUnlinkJsonOutput:
    """--json flag produces structured output for unlink."""

    def test_json_success(self, invoke) -> None:
        id_a, id_b = _create_two_tasks(invoke)
        invoke("link", id_a, "blocks", id_b, "--actor", "human:test")

        result = invoke("unlink", id_a, "blocks", id_b, "--actor", "human:test", "--json")
        assert result.exit_code == 0
        parsed = json.loads(result.output)
        assert parsed["ok"] is True
        assert len(parsed["data"]["relationships_out"]) == 0

    def test_json_error_not_found(self, invoke) -> None:
        id_a, id_b = _create_two_tasks(invoke)
        result = invoke("unlink", id_a, "blocks", id_b, "--actor", "human:test", "--json")
        assert result.exit_code == 1
        parsed = json.loads(result.output)
        assert parsed["ok"] is False
        assert parsed["error"]["code"] == "NOT_FOUND"


class TestUnlinkQuietOutput:
    """--quiet flag prints only the task ID for unlink."""

    def test_quiet_success(self, invoke) -> None:
        id_a, id_b = _create_two_tasks(invoke)
        invoke("link", id_a, "blocks", id_b, "--actor", "human:test")

        result = invoke("unlink", id_a, "blocks", id_b, "--actor", "human:test", "--quiet")
        assert result.exit_code == 0
        assert result.output.strip() == id_a


class TestLinkIdValidation:
    """Path traversal prevention via task_id validation."""

    def test_link_rejects_invalid_task_id(self, invoke) -> None:
        """link should reject a malformed source task_id."""
        _, id_b = _create_two_tasks(invoke)
        result = invoke("link", "../../etc/passwd", "blocks", id_b, "--actor", "human:test")
        assert result.exit_code == 1
        assert "INVALID_ID" in result.stderr or "Invalid task ID" in result.stderr

    def test_link_rejects_invalid_task_id_json(self, invoke) -> None:
        _, id_b = _create_two_tasks(invoke)
        result = invoke(
            "link", "../../etc/passwd", "blocks", id_b, "--actor", "human:test", "--json"
        )
        assert result.exit_code == 1
        parsed = json.loads(result.output)
        assert parsed["ok"] is False
        assert parsed["error"]["code"] == "INVALID_ID"

    def test_link_rejects_invalid_target_id(self, invoke) -> None:
        """link should reject a malformed target task_id."""
        id_a, _ = _create_two_tasks(invoke)
        result = invoke("link", id_a, "blocks", "../../etc/passwd", "--actor", "human:test")
        assert result.exit_code == 1
        assert "INVALID_ID" in result.stderr or "Invalid task ID" in result.stderr

    def test_link_rejects_invalid_target_id_json(self, invoke) -> None:
        id_a, _ = _create_two_tasks(invoke)
        result = invoke(
            "link", id_a, "blocks", "../../etc/passwd", "--actor", "human:test", "--json"
        )
        assert result.exit_code == 1
        parsed = json.loads(result.output)
        assert parsed["ok"] is False
        assert parsed["error"]["code"] == "INVALID_ID"

    def test_unlink_rejects_invalid_task_id(self, invoke) -> None:
        """unlink should reject a malformed source task_id."""
        _, id_b = _create_two_tasks(invoke)
        result = invoke("unlink", "../../etc/passwd", "blocks", id_b, "--actor", "human:test")
        assert result.exit_code == 1
        assert "INVALID_ID" in result.stderr or "Invalid task ID" in result.stderr

    def test_unlink_rejects_invalid_task_id_json(self, invoke) -> None:
        _, id_b = _create_two_tasks(invoke)
        result = invoke(
            "unlink", "../../etc/passwd", "blocks", id_b, "--actor", "human:test", "--json"
        )
        assert result.exit_code == 1
        parsed = json.loads(result.output)
        assert parsed["ok"] is False
        assert parsed["error"]["code"] == "INVALID_ID"

    def test_unlink_rejects_invalid_target_id(self, invoke) -> None:
        """unlink should reject a malformed target task_id."""
        id_a, _ = _create_two_tasks(invoke)
        result = invoke("unlink", id_a, "blocks", "../../etc/passwd", "--actor", "human:test")
        assert result.exit_code == 1
        assert "INVALID_ID" in result.stderr or "Invalid task ID" in result.stderr

    def test_unlink_rejects_invalid_target_id_json(self, invoke) -> None:
        id_a, _ = _create_two_tasks(invoke)
        result = invoke(
            "unlink", id_a, "blocks", "../../etc/passwd", "--actor", "human:test", "--json"
        )
        assert result.exit_code == 1
        parsed = json.loads(result.output)
        assert parsed["ok"] is False
        assert parsed["error"]["code"] == "INVALID_ID"


class TestUnlinkErrors:
    """Error cases for lattice unlink."""

    def test_nonexistent_relationship(self, invoke) -> None:
        id_a, id_b = _create_two_tasks(invoke)
        result = invoke("unlink", id_a, "blocks", id_b, "--actor", "human:test")
        assert result.exit_code == 1
        assert "No blocks relationship" in result.stderr

    def test_nonexistent_source_task(self, invoke) -> None:
        _, id_b = _create_two_tasks(invoke)
        fake_source = "task_01ZZZZZZZZZZZZZZZZZZZZZZZZ"
        result = invoke("unlink", fake_source, "blocks", id_b, "--actor", "human:test")
        assert result.exit_code == 1
        assert "not found" in result.stderr.lower()

    def test_invalid_relationship_type(self, invoke) -> None:
        id_a, id_b = _create_two_tasks(invoke)
        result = invoke("unlink", id_a, "parent_of", id_b, "--actor", "human:test")
        assert result.exit_code == 1
        assert "Invalid relationship type" in result.stderr

    def test_wrong_type_wrong_target(self, invoke) -> None:
        """Unlinking with wrong type but existing target still fails."""
        id_a, id_b = _create_two_tasks(invoke)
        invoke("link", id_a, "blocks", id_b, "--actor", "human:test")

        result = invoke("unlink", id_a, "depends_on", id_b, "--actor", "human:test")
        assert result.exit_code == 1
        assert "No depends_on relationship" in result.stderr


# ---------------------------------------------------------------------------
# Event log integrity
# ---------------------------------------------------------------------------


class TestEventLogIntegrity:
    """Verify events are written correctly for link/unlink operations."""

    def test_link_event_not_in_lifecycle_log(self, invoke, initialized_root: Path) -> None:
        """relationship_added events should NOT go to _lifecycle.jsonl."""
        id_a, id_b = _create_two_tasks(invoke)
        invoke("link", id_a, "blocks", id_b, "--actor", "human:test")

        lifecycle_path = initialized_root / LATTICE_DIR / "events" / "_lifecycle.jsonl"
        lifecycle_events = [
            json.loads(line) for line in lifecycle_path.read_text().strip().split("\n") if line
        ]
        # Only task_created events should be in lifecycle log
        for ev in lifecycle_events:
            assert ev["type"] == "task_created"

    def test_unlink_event_not_in_lifecycle_log(self, invoke, initialized_root: Path) -> None:
        """relationship_removed events should NOT go to _lifecycle.jsonl."""
        id_a, id_b = _create_two_tasks(invoke)
        invoke("link", id_a, "blocks", id_b, "--actor", "human:test")
        invoke("unlink", id_a, "blocks", id_b, "--actor", "human:test")

        lifecycle_path = initialized_root / LATTICE_DIR / "events" / "_lifecycle.jsonl"
        lifecycle_events = [
            json.loads(line) for line in lifecycle_path.read_text().strip().split("\n") if line
        ]
        for ev in lifecycle_events:
            assert ev["type"] == "task_created"

    def test_event_order_preserved(self, invoke, initialized_root: Path) -> None:
        """Events should appear in the order they were created."""
        id_a, id_b = _create_two_tasks(invoke)
        invoke("link", id_a, "blocks", id_b, "--actor", "human:test")
        invoke("unlink", id_a, "blocks", id_b, "--actor", "human:test")

        events = _read_events(initialized_root, id_a)
        types = [e["type"] for e in events]
        assert types == ["task_created", "relationship_added", "relationship_removed"]
