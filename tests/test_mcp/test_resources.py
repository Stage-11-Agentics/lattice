"""Tests for MCP resource functions."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from lattice.core.events import create_event, serialize_event
from lattice.mcp.resources import (
    resource_all_tasks,
    resource_config,
    resource_notes,
    resource_plans,
    resource_task_detail,
    resource_tasks_by_assignee,
    resource_tasks_by_status,
)
from lattice.mcp.tools import (
    lattice_archive,
    lattice_assign,
    lattice_create,
    lattice_status,
)


class TestResourceAllTasks:
    def test_empty(self, lattice_env: Path):
        result = json.loads(resource_all_tasks())
        assert result == []

    def test_with_tasks(self, lattice_env: Path):
        lattice_create(title="Task 1", actor="human:test")
        lattice_create(title="Task 2", actor="human:test")
        result = json.loads(resource_all_tasks())
        assert len(result) == 2


class TestResourceTaskDetail:
    def test_basic(self, lattice_env: Path):
        task = lattice_create(title="Detail test", actor="human:test")
        result = json.loads(resource_task_detail(task["id"]))
        assert result["title"] == "Detail test"
        assert "events" in result
        assert len(result["events"]) >= 1

    def test_with_short_id(self, lattice_env: Path):
        task = lattice_create(title="Short ID", actor="human:test")
        result = json.loads(resource_task_detail(task["short_id"]))
        assert result["id"] == task["id"]

    def test_archived(self, lattice_env: Path):
        task = lattice_create(title="Archived", actor="human:test")
        lattice_archive(task_id=task["id"], actor="human:test")
        result = json.loads(resource_task_detail(task["id"]))
        assert result["archived"] is True

    def test_not_found(self, lattice_env: Path):
        with pytest.raises(ValueError, match="not found"):
            resource_task_detail("task_00000000000000000000000099")


class TestResourceTasksByStatus:
    def test_filter(self, lattice_env: Path):
        task = lattice_create(title="Planning", actor="human:test")
        lattice_status(task_id=task["id"], new_status="in_planning", actor="human:test")
        lattice_create(title="Backlog", actor="human:test")

        result = json.loads(resource_tasks_by_status("in_planning"))
        assert len(result) == 1
        assert result[0]["title"] == "Planning"


class TestResourceTasksByAssignee:
    def test_filter(self, lattice_env: Path):
        task = lattice_create(title="Assigned", actor="human:test")
        lattice_assign(task_id=task["id"], assignee="agent:claude", actor="human:test")
        lattice_create(title="Unassigned", actor="human:test")

        result = json.loads(resource_tasks_by_assignee("agent:claude"))
        assert len(result) == 1
        assert result[0]["title"] == "Assigned"


class TestResourceConfig:
    def test_returns_config(self, lattice_env: Path):
        result = json.loads(resource_config())
        assert "workflow" in result
        assert result["project_code"] == "TST"


class TestResourceNotes:
    def test_notes_exist(self, lattice_env: Path, lattice_dir: Path):
        task = lattice_create(title="Notes task", actor="human:test")
        # Notes are lazy (not scaffolded on create), so create manually
        notes_path = lattice_dir / "notes" / f"{task['id']}.md"
        notes_path.write_text("# Notes task\n\nSome notes.\n")
        result = resource_notes(task["id"])
        assert "Notes task" in result
        assert "Some notes" in result

    def test_notes_not_found(self, lattice_env: Path, lattice_dir: Path):
        task = lattice_create(title="No notes", actor="human:test")
        # Notes are lazy — no file exists by default
        with pytest.raises(ValueError, match="No notes"):
            resource_notes(task["id"])


class TestResourcePlans:
    def test_plan_exist(self, lattice_env: Path):
        task = lattice_create(title="Plan task", actor="human:test")
        # Plans are scaffolded on task create
        result = resource_plans(task["id"])
        assert "Plan task" in result
        assert "Plan task" in result

    def test_plan_not_found(self, lattice_env: Path, lattice_dir: Path):
        task = lattice_create(title="No plan", actor="human:test")
        # Remove the auto-scaffolded plan
        plan_path = lattice_dir / "plans" / f"{task['id']}.md"
        if plan_path.exists():
            plan_path.unlink()
        with pytest.raises(ValueError, match="No plan"):
            resource_plans(task["id"])

    def test_wrong_only_unarchive_uses_authority_for_aggregate_plan_and_notes(
        self, lattice_env: Path, lattice_dir: Path
    ):
        task = lattice_create(title="Interrupted MCP unarchive", actor="human:test")
        task_id = task["id"]
        active_event = lattice_dir / "events" / f"{task_id}.jsonl"
        archived_event = lattice_dir / "archive" / "events" / f"{task_id}.jsonl"
        archived_event.parent.mkdir(parents=True, exist_ok=True)
        archived_event.write_bytes(
            active_event.read_bytes()
            + serialize_event(create_event("task_archived", task_id, "human:test", {})).encode()
            + serialize_event(create_event("task_unarchived", task_id, "human:test", {})).encode()
        )
        active_event.unlink()
        active_plan = lattice_dir / "plans" / f"{task_id}.md"
        archived_plan = lattice_dir / "archive" / "plans" / f"{task_id}.md"
        archived_plan.parent.mkdir(parents=True, exist_ok=True)
        archived_plan.write_text("# MCP plan\n", encoding="utf-8")
        active_plan.unlink()
        archived_notes = lattice_dir / "archive" / "notes" / f"{task_id}.md"
        archived_notes.parent.mkdir(parents=True, exist_ok=True)
        archived_notes.write_text("# MCP notes\n", encoding="utf-8")
        archived_task = lattice_create(title="Interrupted MCP archive", actor="human:test")
        archived_task_id = archived_task["id"]
        archived_active_event = lattice_dir / "events" / f"{archived_task_id}.jsonl"
        split_archived_event = lattice_dir / "archive" / "events" / f"{archived_task_id}.jsonl"
        split_archived_event.write_bytes(
            archived_active_event.read_bytes()
            + serialize_event(
                create_event("task_archived", archived_task_id, "human:test", {})
            ).encode()
        )

        assert [item["id"] for item in json.loads(resource_all_tasks())] == [task_id]
        assert resource_plans(task_id) == "# MCP plan\n"
        assert resource_notes(task_id) == "# MCP notes\n"
        assert json.loads(resource_task_detail(archived_task_id))["archived"] is True
        assert "Interrupted MCP archive" in resource_plans(archived_task_id)
