"""CLI coverage for optional task-local acceptance criteria."""

from __future__ import annotations

import json
from pathlib import Path


def test_zero_criteria_list_is_compatible(create_task, invoke) -> None:
    task = create_task("Zero criteria")
    result = invoke("criterion", "list", task["id"], "--json")
    assert result.exit_code == 0
    assert json.loads(result.output)["data"]["criteria"] == []


def test_auto_add_edit_retire_and_history(create_task, invoke) -> None:
    task = create_task("Criterion lifecycle")
    task_id = task["id"]
    add = invoke(
        "criterion",
        "add",
        task_id,
        "  Playback resumes.  ",
        "--actor",
        "agent:test",
        "--json",
    )
    assert add.exit_code == 0, add.output
    added = json.loads(add.output)["data"]["criterion"]
    assert added["id"] == "AC-1"
    assert added["revision"] == 1

    edit = invoke(
        "criterion",
        "edit",
        task_id,
        "AC-1",
        "Playback resumes without app restart.",
        "--actor",
        "agent:test",
        "--json",
    )
    assert edit.exit_code == 0, edit.output
    assert json.loads(edit.output)["data"]["criterion"]["revision"] == 2

    retire = invoke(
        "criterion", "retire", task_id, "AC-1", "--actor", "human:test", "--json"
    )
    assert retire.exit_code == 0, retire.output
    assert json.loads(retire.output)["data"]["criterion"]["retired"] is True

    default_list = invoke("criterion", "list", task_id, "--json")
    assert json.loads(default_list.output)["data"]["criteria"] == []
    history = invoke(
        "criterion", "list", task_id, "--include-retired", "--history", "--json"
    )
    criterion = json.loads(history.output)["data"]["criteria"][0]
    assert [revision["revision"] for revision in criterion["revisions"]] == [1, 2]


def test_explicit_retry_conflict_and_noop_edit(create_task, invoke) -> None:
    task_id = create_task("Retries")["id"]
    args = (
        "criterion",
        "add",
        task_id,
        "Observable.",
        "--id",
        "custom",
        "--actor",
        "agent:test",
    )
    assert invoke(*args).exit_code == 0
    retry = invoke(*args)
    assert retry.exit_code == 0
    assert "idempotent" in retry.output.lower()
    conflict = invoke(
        "criterion",
        "add",
        task_id,
        "Different.",
        "--id",
        "custom",
        "--actor",
        "agent:test",
    )
    assert conflict.exit_code != 0
    before = invoke("criterion", "list", task_id, "--history", "--json")
    noop = invoke(
        "criterion",
        "edit",
        task_id,
        "custom",
        " Observable. ",
        "--actor",
        "agent:test",
    )
    assert noop.exit_code == 0
    after = invoke("criterion", "list", task_id, "--history", "--json")
    assert json.loads(before.output)["data"] == json.loads(after.output)["data"]


def test_multiline_file_and_quiet(create_task, invoke, tmp_path: Path) -> None:
    task_id = create_task("File criterion")["id"]
    outcome_path = tmp_path / "outcome.md"
    outcome_path.write_text("First line\nsecond line\n", encoding="utf-8")
    result = invoke(
        "criterion",
        "add",
        task_id,
        "--file",
        str(outcome_path),
        "--actor",
        "agent:test",
        "--quiet",
    )
    assert result.exit_code == 0
    assert result.output.strip() == "AC-1"


def test_archived_list_and_mutation_rejection(create_task, invoke) -> None:
    task_id = create_task("Archived criteria")["id"]
    assert (
        invoke(
            "criterion",
            "add",
            task_id,
            "Exists in archive.",
            "--actor",
            "agent:test",
        ).exit_code
        == 0
    )
    assert invoke("archive", task_id, "--actor", "human:test").exit_code == 0
    listed = invoke("criterion", "list", task_id, "--json")
    assert listed.exit_code == 0
    assert json.loads(listed.output)["data"]["archived"] is True
    rejected = invoke(
        "criterion",
        "add",
        task_id,
        "Must not resurrect.",
        "--actor",
        "agent:test",
    )
    assert rejected.exit_code != 0
