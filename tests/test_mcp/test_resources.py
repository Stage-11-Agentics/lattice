"""Tests for MCP resource functions."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

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
    lattice_raise_alert,
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

    def test_alerts_field_round_trips_through_detail(self, lattice_env: Path):
        """LAT-210: alerts must surface through the MCP task resource."""
        task = lattice_create(title="Alerted", actor="agent:claude")
        lattice_raise_alert(
            task_id=task["id"],
            alert_name="needs_human",
            short="?",
            actor="agent:claude",
        )
        result = json.loads(resource_task_detail(task["id"]))
        assert "needs_human" in result.get("alerts", {})

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
