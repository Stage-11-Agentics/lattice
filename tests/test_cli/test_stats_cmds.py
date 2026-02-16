"""Tests for lattice stats command."""

from __future__ import annotations


class TestStatsCommand:
    """Tests for the stats CLI command."""

    def test_stats_empty_project(self, invoke):
        """Stats on a project with no tasks."""
        result = invoke("stats")
        assert result.exit_code == 0
        assert "0 active" in result.output
        assert "0 archived" in result.output

    def test_stats_json_empty(self, invoke_json):
        """JSON stats on empty project."""
        parsed, code = invoke_json("stats")
        assert code == 0
        assert parsed["ok"] is True
        data = parsed["data"]
        assert data["summary"]["active_tasks"] == 0
        assert data["summary"]["archived_tasks"] == 0
        assert data["summary"]["total_events"] == 0

    def test_stats_with_tasks(self, invoke, create_task):
        """Stats with a few tasks created."""
        create_task("Task A", "--priority", "high")
        create_task("Task B", "--priority", "low")
        create_task("Task C")

        result = invoke("stats")
        assert result.exit_code == 0
        assert "3 active" in result.output
        assert "0 archived" in result.output

    def test_stats_json_with_tasks(self, invoke_json, create_task):
        """JSON stats with tasks."""
        create_task("Alpha", "--priority", "high")
        create_task("Beta", "--priority", "high")
        create_task("Gamma", "--priority", "medium")

        parsed, code = invoke_json("stats")
        assert code == 0
        data = parsed["data"]
        assert data["summary"]["active_tasks"] == 3
        assert data["summary"]["total_tasks"] == 3

        # Check priority distribution
        priority_map = dict(data["by_priority"])
        assert priority_map["high"] == 2
        assert priority_map["medium"] == 1

    def test_stats_status_distribution(self, invoke, invoke_json, create_task):
        """Stats show correct status counts after transitions."""
        task = create_task("To do")
        task_id = task["id"]

        # Move to in_planning
        invoke("status", task_id, "in_planning", "--actor", "human:test")

        parsed, code = invoke_json("stats")
        assert code == 0
        status_map = dict(parsed["data"]["by_status"])
        assert status_map.get("in_planning", 0) == 1

    def test_stats_assignee_distribution(self, invoke_json, invoke, create_task):
        """Stats show assignment counts."""
        task = create_task("Assigned task")
        task_id = task["id"]
        invoke("assign", task_id, "agent:claude", "--actor", "human:test")

        parsed, code = invoke_json("stats")
        assert code == 0
        assignee_map = dict(parsed["data"]["by_assignee"])
        assert assignee_map.get("agent:claude", 0) == 1

    def test_stats_type_distribution(self, invoke_json, create_task):
        """Stats show type breakdown."""
        create_task("Bug fix", "--type", "bug")
        create_task("Feature", "--type", "task")
        create_task("Another bug", "--type", "bug")

        parsed, code = invoke_json("stats")
        assert code == 0
        type_map = dict(parsed["data"]["by_type"])
        assert type_map["bug"] == 2
        assert type_map["task"] == 1

    def test_stats_event_count(self, invoke_json, invoke, create_task):
        """Total events increase with operations."""
        task = create_task("Busy task")
        task_id = task["id"]

        # Create adds 1 event (task_created). Status change adds 1 more.
        invoke("status", task_id, "in_planning", "--actor", "human:test")
        invoke("comment", task_id, "A note", "--actor", "human:test")

        parsed, code = invoke_json("stats")
        assert code == 0
        # At least 3 events: task_created + short_id_assigned + status_changed + comment_added
        assert parsed["data"]["summary"]["total_events"] >= 3

    def test_stats_recently_active(self, invoke_json, create_task):
        """Recently active list includes created tasks."""
        create_task("Fresh task")

        parsed, code = invoke_json("stats")
        assert code == 0
        recent = parsed["data"]["recently_active"]
        assert len(recent) >= 1
        assert recent[0]["title"] == "Fresh task"

    def test_stats_busiest(self, invoke_json, invoke, create_task):
        """Busiest tasks list reflects event activity."""
        task = create_task("Active task")
        task_id = task["id"]

        invoke("comment", task_id, "Comment 1", "--actor", "human:test")
        invoke("comment", task_id, "Comment 2", "--actor", "human:test")

        parsed, code = invoke_json("stats")
        assert code == 0
        busiest = parsed["data"]["busiest"]
        assert len(busiest) >= 1
        # The task we commented on should be in the busiest list
        ids = [b["id"] for b in busiest]
        short_id = task.get("short_id") or task_id
        assert short_id in ids or task_id in ids

    def test_stats_with_archived(self, invoke, invoke_json, create_task):
        """Archived tasks show in archived count."""
        task = create_task("Archive me")
        task_id = task["id"]

        invoke("archive", task_id, "--actor", "human:test")

        parsed, code = invoke_json("stats")
        assert code == 0
        assert parsed["data"]["summary"]["archived_tasks"] == 1
        assert parsed["data"]["summary"]["active_tasks"] == 0

    def test_stats_json_structure(self, invoke_json):
        """JSON output has all expected top-level keys."""
        parsed, code = invoke_json("stats")
        assert code == 0
        data = parsed["data"]
        expected_keys = {
            "summary", "by_status", "by_priority", "by_type",
            "by_assignee", "by_tag", "wip", "recently_active",
            "stale", "busiest",
        }
        assert expected_keys == set(data.keys())
