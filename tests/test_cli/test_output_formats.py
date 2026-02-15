"""Tests for output formats: --json envelopes, --quiet mode, and error message quality."""

from __future__ import annotations

import json

from ulid import ULID


# ---------------------------------------------------------------------------
# JSON envelope — success (write commands)
# ---------------------------------------------------------------------------


def test_create_json_envelope(invoke):
    result = invoke("create", "Test task", "--actor", "human:test", "--json")
    assert result.exit_code == 0
    data = json.loads(result.output)
    assert data["ok"] is True
    assert "id" in data["data"]
    assert data["data"]["title"] == "Test task"
    assert data["data"]["status"] == "backlog"


def test_update_json_envelope(invoke, create_task):
    task = create_task("Original title")
    task_id = task["id"]
    result = invoke("update", task_id, "title=New title", "--actor", "human:test", "--json")
    assert result.exit_code == 0
    data = json.loads(result.output)
    assert data["ok"] is True
    assert data["data"]["title"] == "New title"


def test_status_json_envelope(invoke, create_task):
    task = create_task("Status test")
    task_id = task["id"]
    result = invoke("status", task_id, "ready", "--actor", "human:test", "--json")
    assert result.exit_code == 0
    data = json.loads(result.output)
    assert data["ok"] is True
    assert data["data"]["status"] == "ready"


def test_assign_json_envelope(invoke, create_task):
    task = create_task("Assign test")
    task_id = task["id"]
    result = invoke("assign", task_id, "agent:bot", "--actor", "human:test", "--json")
    assert result.exit_code == 0
    data = json.loads(result.output)
    assert data["ok"] is True
    assert data["data"]["assigned_to"] == "agent:bot"


def test_comment_json_envelope(invoke, create_task):
    task = create_task("Comment test")
    task_id = task["id"]
    result = invoke("comment", task_id, "A note", "--actor", "human:test", "--json")
    assert result.exit_code == 0
    data = json.loads(result.output)
    assert data["ok"] is True
    assert "id" in data["data"]


def test_link_json_envelope(invoke, create_task):
    task1 = create_task("Blocker")
    task2 = create_task("Blocked")
    result = invoke("link", task1["id"], "blocks", task2["id"], "--actor", "human:test", "--json")
    assert result.exit_code == 0
    data = json.loads(result.output)
    assert data["ok"] is True
    rels = data["data"].get("relationships_out", [])
    assert len(rels) == 1
    assert rels[0]["type"] == "blocks"
    assert rels[0]["target_task_id"] == task2["id"]


def test_archive_json_envelope(invoke, create_task):
    task = create_task("Archive me")
    task_id = task["id"]
    result = invoke("archive", task_id, "--actor", "human:test", "--json")
    assert result.exit_code == 0
    data = json.loads(result.output)
    assert data["ok"] is True


# ---------------------------------------------------------------------------
# JSON envelope — success (read commands)
# ---------------------------------------------------------------------------


def test_list_json_envelope(invoke, create_task):
    create_task("Task A")
    create_task("Task B")
    result = invoke("list", "--json")
    assert result.exit_code == 0
    data = json.loads(result.output)
    assert data["ok"] is True
    assert isinstance(data["data"], list)
    assert len(data["data"]) >= 2


def test_show_json_envelope(invoke, create_task):
    task = create_task("Show me")
    task_id = task["id"]
    result = invoke("show", task_id, "--json")
    assert result.exit_code == 0
    data = json.loads(result.output)
    assert data["ok"] is True
    assert data["data"]["id"] == task_id
    assert data["data"]["title"] == "Show me"


# ---------------------------------------------------------------------------
# JSON envelope — errors
# ---------------------------------------------------------------------------


def test_not_found_json_error(invoke):
    fake_id = f"task_{ULID()}"
    result = invoke("show", fake_id, "--json")
    assert result.exit_code != 0
    data = json.loads(result.output)
    assert data["ok"] is False
    assert data["error"]["code"] == "NOT_FOUND"
    assert fake_id in data["error"]["message"]


def test_invalid_actor_json_error(invoke):
    result = invoke("create", "Test", "--actor", "badformat", "--json")
    assert result.exit_code != 0
    data = json.loads(result.output)
    assert data["ok"] is False
    assert data["error"]["code"] == "INVALID_ACTOR"
    assert "badformat" in data["error"]["message"]


def test_invalid_transition_json_error(invoke, create_task):
    task = create_task("Transition test")
    task_id = task["id"]
    # backlog -> done is not a valid transition
    result = invoke("status", task_id, "done", "--actor", "human:test", "--json")
    assert result.exit_code != 0
    data = json.loads(result.output)
    assert data["ok"] is False
    assert data["error"]["code"] == "INVALID_TRANSITION"


# ---------------------------------------------------------------------------
# Quiet mode
# ---------------------------------------------------------------------------


def test_create_quiet_outputs_only_id(invoke):
    result = invoke("create", "Quiet task", "--actor", "human:test", "--quiet")
    assert result.exit_code == 0
    output = result.output.strip()
    assert output.startswith("task_")
    # Should be a single line
    assert "\n" not in output


def test_status_quiet_outputs_ok(invoke, create_task):
    task = create_task("Quiet status")
    task_id = task["id"]
    result = invoke("status", task_id, "ready", "--actor", "human:test", "--quiet")
    assert result.exit_code == 0
    assert result.output.strip() == "ok"


def test_comment_quiet_outputs_ok(invoke, create_task):
    task = create_task("Quiet comment")
    task_id = task["id"]
    result = invoke("comment", task_id, "Note", "--actor", "human:test", "--quiet")
    assert result.exit_code == 0
    assert result.output.strip() == "ok"


# ---------------------------------------------------------------------------
# Error messages list valid values
# ---------------------------------------------------------------------------


def test_invalid_status_lists_valid(invoke):
    result = invoke("create", "Test", "--status", "bogus", "--actor", "human:test")
    assert result.exit_code != 0
    # Error message should list valid statuses (goes to stderr or stdout depending on mode)
    combined = result.output + (result.output if not result.output else "")
    assert "backlog" in combined or "backlog" in str(result.exception or "")
    # Try --json mode for reliable parsing
    result_json = invoke("create", "Test", "--status", "bogus", "--actor", "human:test", "--json")
    data = json.loads(result_json.output)
    assert data["ok"] is False
    msg = data["error"]["message"]
    assert "backlog" in msg
    assert "ready" in msg
    assert "done" in msg


def test_invalid_priority_lists_valid(invoke):
    result = invoke("create", "Test", "--priority", "bogus", "--actor", "human:test", "--json")
    assert result.exit_code != 0
    data = json.loads(result.output)
    assert data["ok"] is False
    msg = data["error"]["message"]
    assert "critical" in msg
    assert "high" in msg
    assert "medium" in msg
    assert "low" in msg


def test_invalid_type_lists_valid(invoke):
    result = invoke("create", "Test", "--type", "bogus", "--actor", "human:test", "--json")
    assert result.exit_code != 0
    data = json.loads(result.output)
    assert data["ok"] is False
    msg = data["error"]["message"]
    assert "task" in msg
    assert "epic" in msg
    assert "bug" in msg


def test_invalid_relationship_type_lists_valid(invoke, create_task):
    task1 = create_task("Source")
    task2 = create_task("Target")
    result = invoke("link", task1["id"], "bogus", task2["id"], "--actor", "human:test", "--json")
    assert result.exit_code != 0
    data = json.loads(result.output)
    assert data["ok"] is False
    msg = data["error"]["message"]
    assert "blocks" in msg
    assert "depends_on" in msg


def test_invalid_transition_suggests_force(invoke, create_task):
    task = create_task("Force test")
    task_id = task["id"]
    # backlog -> done is invalid
    result = invoke("status", task_id, "done", "--actor", "human:test", "--json")
    assert result.exit_code != 0
    data = json.loads(result.output)
    assert data["ok"] is False
    assert "--force" in data["error"]["message"]
