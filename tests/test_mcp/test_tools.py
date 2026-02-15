"""Tests for MCP tool functions."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from lattice.mcp.tools import (
    lattice_archive,
    lattice_assign,
    lattice_attach,
    lattice_comment,
    lattice_config,
    lattice_create,
    lattice_doctor,
    lattice_event,
    lattice_link,
    lattice_list,
    lattice_show,
    lattice_status,
    lattice_unarchive,
    lattice_unlink,
    lattice_update,
)


class TestCreate:
    """Tests for lattice_create tool."""

    def test_create_basic(self, lattice_env: Path):
        result = lattice_create(title="Test task", actor="human:test")
        assert result["title"] == "Test task"
        assert result["status"] == "backlog"
        assert result["priority"] == "medium"
        assert result["type"] == "task"
        assert result["id"].startswith("task_")

    def test_create_with_options(self, lattice_env: Path):
        result = lattice_create(
            title="Important bug",
            actor="agent:claude",
            task_type="bug",
            priority="high",
            description="Fix this ASAP",
            tags="frontend,urgent",
            assigned_to="human:atin",
        )
        assert result["title"] == "Important bug"
        assert result["type"] == "bug"
        assert result["priority"] == "high"
        assert result["description"] == "Fix this ASAP"
        assert result["tags"] == ["frontend", "urgent"]
        assert result["assigned_to"] == "human:atin"

    def test_create_short_id(self, lattice_env: Path, lattice_dir: Path):
        """Tasks get short IDs when project_code is configured."""
        result = lattice_create(title="Short ID task", actor="human:test")
        assert result.get("short_id") is not None
        assert result["short_id"].startswith("TST-")

    def test_create_writes_events(self, lattice_env: Path, lattice_dir: Path):
        result = lattice_create(title="Event test", actor="human:test")
        task_id = result["id"]
        event_path = lattice_dir / "events" / f"{task_id}.jsonl"
        assert event_path.exists()
        events = [json.loads(line) for line in event_path.read_text().strip().split("\n")]
        assert events[0]["type"] == "task_created"

    def test_create_scaffolds_notes(self, lattice_env: Path, lattice_dir: Path):
        result = lattice_create(title="Notes test", actor="human:test")
        task_id = result["id"]
        notes_path = lattice_dir / "notes" / f"{task_id}.md"
        assert notes_path.exists()
        content = notes_path.read_text()
        assert "Notes test" in content
        assert "## Summary" in content
        assert "## Technical Plan" in content

    def test_create_invalid_actor(self, lattice_env: Path):
        with pytest.raises(ValueError, match="Invalid actor"):
            lattice_create(title="Bad actor", actor="invalid")

    def test_create_invalid_priority(self, lattice_env: Path):
        with pytest.raises(ValueError, match="Invalid priority"):
            lattice_create(title="Bad priority", actor="human:test", priority="mega")


class TestUpdate:
    """Tests for lattice_update tool."""

    def test_update_title(self, lattice_env: Path):
        task = lattice_create(title="Original", actor="human:test")
        result = lattice_update(
            task_id=task["id"], actor="human:test", fields={"title": "Updated"}
        )
        assert result["title"] == "Updated"

    def test_update_multiple_fields(self, lattice_env: Path):
        task = lattice_create(title="Multi", actor="human:test")
        result = lattice_update(
            task_id=task["id"],
            actor="human:test",
            fields={"title": "New title", "priority": "high"},
        )
        assert result["title"] == "New title"
        assert result["priority"] == "high"

    def test_update_no_changes(self, lattice_env: Path):
        task = lattice_create(title="Same", actor="human:test")
        result = lattice_update(task_id=task["id"], actor="human:test", fields={"title": "Same"})
        assert "message" in result
        assert result["message"] == "No changes"

    def test_update_rejects_status(self, lattice_env: Path):
        task = lattice_create(title="Status", actor="human:test")
        with pytest.raises(ValueError, match="lattice_status"):
            lattice_update(task_id=task["id"], actor="human:test", fields={"status": "done"})

    def test_update_custom_field(self, lattice_env: Path):
        task = lattice_create(title="Custom", actor="human:test")
        result = lattice_update(
            task_id=task["id"],
            actor="human:test",
            fields={"custom_fields.sprint": "2024-Q1"},
        )
        assert result["custom_fields"]["sprint"] == "2024-Q1"


class TestStatus:
    """Tests for lattice_status tool."""

    def test_status_transition(self, lattice_env: Path):
        task = lattice_create(title="Status test", actor="human:test")
        result = lattice_status(task_id=task["id"], new_status="in_planning", actor="human:test")
        assert result["status"] == "in_planning"

    def test_status_already_at_target(self, lattice_env: Path):
        task = lattice_create(title="Same status", actor="human:test")
        result = lattice_status(task_id=task["id"], new_status="backlog", actor="human:test")
        assert result["message"] == "Already at status backlog"

    def test_status_invalid_transition(self, lattice_env: Path):
        task = lattice_create(title="Bad transition", actor="human:test")
        with pytest.raises(ValueError, match="Invalid transition"):
            lattice_status(task_id=task["id"], new_status="done", actor="human:test")

    def test_status_force(self, lattice_env: Path):
        task = lattice_create(title="Force", actor="human:test")
        result = lattice_status(
            task_id=task["id"],
            new_status="done",
            actor="human:test",
            force=True,
            reason="Emergency",
        )
        assert result["status"] == "done"


class TestAssign:
    """Tests for lattice_assign tool."""

    def test_assign_basic(self, lattice_env: Path):
        task = lattice_create(title="Assign test", actor="human:test")
        result = lattice_assign(task_id=task["id"], assignee="agent:claude", actor="human:test")
        assert result["assigned_to"] == "agent:claude"

    def test_assign_already_assigned(self, lattice_env: Path):
        task = lattice_create(title="Pre-assigned", actor="human:test", assigned_to="agent:claude")
        result = lattice_assign(task_id=task["id"], assignee="agent:claude", actor="human:test")
        assert result["message"] == "Already assigned to agent:claude"


class TestComment:
    """Tests for lattice_comment tool."""

    def test_comment_basic(self, lattice_env: Path, lattice_dir: Path):
        task = lattice_create(title="Comment test", actor="human:test")
        result = lattice_comment(task_id=task["id"], text="Hello world", actor="human:test")
        assert result["id"] == task["id"]

        # Verify event written
        events_path = lattice_dir / "events" / f"{task['id']}.jsonl"
        lines = events_path.read_text().strip().split("\n")
        last_event = json.loads(lines[-1])
        assert last_event["type"] == "comment_added"
        assert last_event["data"]["body"] == "Hello world"


class TestLink:
    """Tests for lattice_link tool."""

    def test_link_basic(self, lattice_env: Path):
        task1 = lattice_create(title="Source", actor="human:test")
        task2 = lattice_create(title="Target", actor="human:test")
        result = lattice_link(
            source_id=task1["id"],
            relationship_type="blocks",
            target_id=task2["id"],
            actor="human:test",
        )
        assert len(result["relationships_out"]) == 1
        assert result["relationships_out"][0]["type"] == "blocks"
        assert result["relationships_out"][0]["target_task_id"] == task2["id"]

    def test_link_self_rejected(self, lattice_env: Path):
        task = lattice_create(title="Self link", actor="human:test")
        with pytest.raises(ValueError, match="itself"):
            lattice_link(
                source_id=task["id"],
                relationship_type="blocks",
                target_id=task["id"],
                actor="human:test",
            )


class TestUnlink:
    """Tests for lattice_unlink tool."""

    def test_unlink_basic(self, lattice_env: Path):
        task1 = lattice_create(title="Source", actor="human:test")
        task2 = lattice_create(title="Target", actor="human:test")
        lattice_link(
            source_id=task1["id"],
            relationship_type="blocks",
            target_id=task2["id"],
            actor="human:test",
        )
        result = lattice_unlink(
            source_id=task1["id"],
            relationship_type="blocks",
            target_id=task2["id"],
            actor="human:test",
        )
        assert len(result["relationships_out"]) == 0

    def test_unlink_not_found(self, lattice_env: Path):
        task1 = lattice_create(title="Source", actor="human:test")
        task2 = lattice_create(title="Target", actor="human:test")
        with pytest.raises(ValueError, match="No blocks relationship"):
            lattice_unlink(
                source_id=task1["id"],
                relationship_type="blocks",
                target_id=task2["id"],
                actor="human:test",
            )


class TestArchive:
    """Tests for lattice_archive tool."""

    def test_archive_basic(self, lattice_env: Path, lattice_dir: Path):
        task = lattice_create(title="Archive me", actor="human:test")
        task_id = task["id"]
        result = lattice_archive(task_id=task_id, actor="human:test")
        assert result["type"] == "task_archived"
        assert result["task_id"] == task_id

        # Snapshot moved
        assert not (lattice_dir / "tasks" / f"{task_id}.json").exists()
        assert (lattice_dir / "archive" / "tasks" / f"{task_id}.json").exists()

    def test_archive_already_archived(self, lattice_env: Path):
        task = lattice_create(title="Double archive", actor="human:test")
        lattice_archive(task_id=task["id"], actor="human:test")
        with pytest.raises(ValueError, match="already archived"):
            lattice_archive(task_id=task["id"], actor="human:test")


class TestUnarchive:
    """Tests for lattice_unarchive tool."""

    def test_unarchive_basic(self, lattice_env: Path, lattice_dir: Path):
        task = lattice_create(title="Unarchive me", actor="human:test")
        task_id = task["id"]
        lattice_archive(task_id=task_id, actor="human:test")
        result = lattice_unarchive(task_id=task_id, actor="human:test")
        assert result["type"] == "task_unarchived"

        # Snapshot back in active
        assert (lattice_dir / "tasks" / f"{task_id}.json").exists()
        assert not (lattice_dir / "archive" / "tasks" / f"{task_id}.json").exists()

    def test_unarchive_already_active(self, lattice_env: Path):
        task = lattice_create(title="Already active", actor="human:test")
        with pytest.raises(ValueError, match="already active"):
            lattice_unarchive(task_id=task["id"], actor="human:test")


class TestEvent:
    """Tests for lattice_event tool."""

    def test_custom_event(self, lattice_env: Path, lattice_dir: Path):
        task = lattice_create(title="Event test", actor="human:test")
        result = lattice_event(
            task_id=task["id"],
            event_type="x_deployment_started",
            actor="agent:claude",
            data={"env": "staging"},
        )
        assert result["type"] == "x_deployment_started"
        assert result["data"]["env"] == "staging"

    def test_builtin_event_rejected(self, lattice_env: Path):
        task = lattice_create(title="Builtin", actor="human:test")
        with pytest.raises(ValueError, match="reserved"):
            lattice_event(
                task_id=task["id"],
                event_type="task_created",
                actor="human:test",
            )


class TestList:
    """Tests for lattice_list tool."""

    def test_list_all(self, lattice_env: Path):
        lattice_create(title="Task 1", actor="human:test")
        lattice_create(title="Task 2", actor="human:test")
        result = lattice_list()
        assert len(result) == 2

    def test_list_filter_status(self, lattice_env: Path):
        task = lattice_create(title="Planning", actor="human:test")
        lattice_status(task_id=task["id"], new_status="in_planning", actor="human:test")
        lattice_create(title="Backlog", actor="human:test")

        result = lattice_list(status="in_planning")
        assert len(result) == 1
        assert result[0]["title"] == "Planning"

    def test_list_filter_assigned(self, lattice_env: Path):
        task = lattice_create(title="Assigned", actor="human:test")
        lattice_assign(task_id=task["id"], assignee="agent:claude", actor="human:test")
        lattice_create(title="Unassigned", actor="human:test")

        result = lattice_list(assigned="agent:claude")
        assert len(result) == 1
        assert result[0]["title"] == "Assigned"

    def test_list_filter_priority(self, lattice_env: Path):
        lattice_create(title="High", actor="human:test", priority="high")
        lattice_create(title="Low", actor="human:test", priority="low")

        result = lattice_list(priority="high")
        assert len(result) == 1
        assert result[0]["title"] == "High"


class TestShow:
    """Tests for lattice_show tool."""

    def test_show_basic(self, lattice_env: Path):
        task = lattice_create(title="Show me", actor="human:test")
        result = lattice_show(task_id=task["id"])
        assert result["title"] == "Show me"
        assert "events" in result
        assert len(result["events"]) >= 1

    def test_show_with_short_id(self, lattice_env: Path):
        task = lattice_create(title="Short ID show", actor="human:test")
        short_id = task["short_id"]
        result = lattice_show(task_id=short_id)
        assert result["id"] == task["id"]

    def test_show_archived(self, lattice_env: Path):
        task = lattice_create(title="Archive show", actor="human:test")
        lattice_archive(task_id=task["id"], actor="human:test")
        result = lattice_show(task_id=task["id"])
        assert result["archived"] is True

    def test_show_not_found(self, lattice_env: Path):
        with pytest.raises(ValueError, match="not found"):
            lattice_show(task_id="task_00000000000000000000000099")


class TestConfig:
    """Tests for lattice_config tool."""

    def test_config_returns_dict(self, lattice_env: Path):
        result = lattice_config()
        assert "workflow" in result
        assert "statuses" in result["workflow"]
        assert result["project_code"] == "TST"


class TestDoctor:
    """Tests for lattice_doctor tool."""

    def test_doctor_healthy(self, lattice_env: Path):
        lattice_create(title="Healthy task", actor="human:test")
        result = lattice_doctor()
        assert result["ok"] is True
        assert result["task_count"] >= 1

    def test_doctor_detects_orphan(self, lattice_env: Path, lattice_dir: Path):
        # Create an orphaned event log (no matching snapshot)
        orphan_path = lattice_dir / "events" / "task_ORPHAN00000000000000000.jsonl"
        orphan_path.write_text('{"type":"task_created"}\n')

        result = lattice_doctor()
        orphan_issues = [i for i in result["issues"] if "orphan" in i["message"].lower()]
        assert len(orphan_issues) >= 1


class TestAttach:
    """Tests for lattice_attach tool."""

    def test_attach_file(self, lattice_env: Path, lattice_dir: Path, tmp_path: Path):
        task = lattice_create(title="Attach test", actor="human:test")
        src_file = tmp_path / "test.txt"
        src_file.write_text("hello")

        result = lattice_attach(
            task_id=task["id"],
            source=str(src_file),
            actor="human:test",
            title="Test file",
        )
        assert result["title"] == "Test file"
        assert result["type"] == "file"
        assert result["id"].startswith("art_")

    def test_attach_url(self, lattice_env: Path):
        task = lattice_create(title="URL test", actor="human:test")
        result = lattice_attach(
            task_id=task["id"],
            source="https://example.com/doc.pdf",
            actor="human:test",
            title="Example doc",
        )
        assert result["type"] == "reference"
        assert result["custom_fields"]["url"] == "https://example.com/doc.pdf"
