"""Task type defaults and compatibility with explicitly configured old types."""

from __future__ import annotations

import json


def test_new_board_defaults_to_task_bug_and_chore(initialized_root):
    config = json.loads((initialized_root / ".lattice" / "config.json").read_text())
    assert config["task_types"] == ["task", "bug", "chore"]


def test_old_configured_types_still_create_read_list_show_and_transition(
    initialized_root, invoke, invoke_json
):
    config_path = initialized_root / ".lattice" / "config.json"
    config = json.loads(config_path.read_text())
    legacy_types = ["task", "bug", "spike", "chore", "epic"]
    config["task_types"] = legacy_types
    config_path.write_text(json.dumps(config), encoding="utf-8")

    task_ids = {}
    for task_type in ("spike", "epic"):
        created, code = invoke_json(
            "create", f"Existing {task_type}", "--type", task_type, "--actor", "human:test"
        )
        assert code == 0 and created["ok"] is True
        task_id = created["data"]["id"]
        task_ids[task_type] = task_id

        shown, code = invoke_json("show", task_id)
        assert code == 0 and shown["data"]["type"] == task_type

    listed, code = invoke_json("list")
    assert code == 0
    listed_types = {task["id"]: task["type"] for task in listed["data"]}
    for task_type, task_id in task_ids.items():
        assert listed_types[task_id] == task_type
        transitioned, code = invoke_json("status", task_id, "in_planning", "--actor", "human:test")
        assert code == 0 and transitioned["data"]["status"] == "in_planning"

    assert json.loads(config_path.read_text())["task_types"] == legacy_types


def test_create_help_describes_configured_task_types(invoke):
    result = invoke("create", "--help")
    assert result.exit_code == 0
    assert "task, bug, chore" in result.output
    assert "custom types come from config" in " ".join(result.output.split())
