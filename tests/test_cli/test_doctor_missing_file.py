"""AC-27: ``lattice doctor`` reports a task file removed by hand (SPEC §7).

A task named by ``_lifecycle.jsonl`` or ``ids.json`` whose event log is gone
is a ``missing_task_file`` finding. Erasing never removes files, so an erased
task is not one.
"""

from __future__ import annotations

import json
from pathlib import Path

from click.testing import CliRunner

from lattice.cli.main import cli
from lattice.storage.short_ids import save_id_index

ACTOR = ("--actor", "human:test")


def _run(runner: CliRunner, env: dict[str, str], *args: str):  # noqa: ANN202
    return runner.invoke(cli, list(args), env=env)


def _missing(result) -> list[dict]:  # noqa: ANN001
    return [
        f
        for f in json.loads(result.output)["data"]["findings"]
        if f["check"] == "missing_task_file"
    ]


def _create(runner: CliRunner, env: dict[str, str], title: str) -> str:
    result = _run(runner, env, "create", title, *ACTOR, "--json")
    return json.loads(result.output)["data"]["id"]


def test_doctor_reports_a_manually_removed_task_file(
    cli_runner: CliRunner, cli_env: dict[str, str], initialized_root: Path
) -> None:
    kept = _create(cli_runner, cli_env, "Kept")
    gone = _create(cli_runner, cli_env, "Gone")
    erased = _create(cli_runner, cli_env, "Erased")
    _run(cli_runner, cli_env, "erase", erased, "--reason", "noise", *ACTOR)

    clean = _run(cli_runner, cli_env, "doctor", "--json")
    assert clean.exit_code == 0, clean.output
    assert _missing(clean) == []

    (initialized_root / ".lattice" / "events" / f"{gone}.jsonl").unlink()

    result = _run(cli_runner, cli_env, "doctor", "--json")
    assert result.exit_code == 1
    assert _missing(result) == [
        {
            "level": "error",
            "check": "missing_task_file",
            "message": (
                f"Task {gone} is referenced by _lifecycle.jsonl but its event log is "
                f"missing (events/{gone}.jsonl)"
            ),
            "task_id": gone,
        }
    ]
    plain = _run(cli_runner, cli_env, "doctor")
    assert plain.exit_code == 1
    assert f"Task {gone} is referenced by _lifecycle.jsonl" in plain.output
    assert kept not in plain.output and erased not in plain.output


def test_doctor_reports_a_task_named_only_by_ids_json(
    cli_runner: CliRunner, cli_env: dict[str, str], initialized_root: Path
) -> None:
    lattice_dir = initialized_root / ".lattice"
    orphan = "task_01J9ZABCDEFGHJKMNPQRSTVWXY"
    save_id_index(
        lattice_dir, {"schema_version": 2, "next_seqs": {"X": 2}, "map": {"X-1": orphan}}
    )

    result = _run(cli_runner, cli_env, "doctor", "--json")
    assert [(f["task_id"], "ids.json" in f["message"]) for f in _missing(result)] == [
        (orphan, True)
    ]


def test_an_archived_task_is_not_missing(
    cli_runner: CliRunner, cli_env: dict[str, str], initialized_root: Path
) -> None:
    task = _create(cli_runner, cli_env, "Archived")
    assert _run(cli_runner, cli_env, "archive", task, *ACTOR).exit_code == 0
    assert _missing(_run(cli_runner, cli_env, "doctor", "--json")) == []
