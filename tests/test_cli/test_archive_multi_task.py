"""Multi-task ``archive`` / ``unarchive`` (LAT-298 review round 1): one
configuration governs the whole command, and an unresolvable ID still prints
its ``Error:`` line to stderr in every output mode."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

_ACTOR = "human:test"


def _create(invoke, title: str) -> str:  # noqa: ANN001
    return json.loads(invoke("create", title, "--actor", _ACTOR, "--json").output)["data"]["id"]


@pytest.mark.parametrize("command", ["archive", "unarchive"])
def test_hook_that_edits_config_does_not_change_later_tasks(
    invoke,
    initialized_root: Path,
    tmp_path: Path,
    command: str,  # noqa: ANN001
) -> None:
    first, second = _create(invoke, "one"), _create(invoke, "two")
    if command == "unarchive":
        invoke("archive", first, second, "--actor", _ACTOR)
    config_path = initialized_root / ".lattice" / "config.json"
    log = tmp_path / "hook.log"
    hook = tmp_path / "hook.py"
    # Logs the task, then removes every hook from config.json.
    hook.write_text(
        "import json, os, sys\n"
        f"open({str(log)!r}, 'a').write(os.environ['LATTICE_TASK_ID'] + '\\n')\n"
        f"p = {str(config_path)!r}\n"
        "c = json.load(open(p))\n"
        "c.pop('hooks', None)\n"
        "open(p, 'w').write(json.dumps(c))\n"
    )
    event_type = "task_archived" if command == "archive" else "task_unarchived"
    config = json.loads(config_path.read_text())
    config["hooks"] = {"on": {event_type: f"{sys.executable} {hook}"}}
    config_path.write_text(json.dumps(config))

    result = invoke(command, first, second, "--actor", _ACTOR)

    assert result.exit_code == 0, result.output
    assert "hooks" not in json.loads(config_path.read_text())  # the first hook ran...
    assert log.read_text().splitlines() == [first, second]  # ...and the second still fired


@pytest.mark.parametrize("command", ["archive", "unarchive"])
@pytest.mark.parametrize("mode", ["plain", "json", "quiet"])
def test_unresolvable_id_prints_its_error_line(invoke, command: str, mode: str) -> None:  # noqa: ANN001
    task_id = _create(invoke, "one")
    if command == "unarchive":
        invoke("archive", task_id, "--actor", _ACTOR)
    flags = {"plain": [], "json": ["--json"], "quiet": ["--quiet"]}[mode]

    result = invoke(command, task_id, "NOPE-1", "junk!", "--actor", _ACTOR, *flags)

    assert result.exit_code == 1
    assert result.stderr.splitlines()[:2] == [
        "Error: Short ID 'NOPE-1' not found.",
        "Error: Invalid task ID format: 'junk!'.",
    ]
    failures = [
        {"id": "NOPE-1", "error": "Invalid or unresolvable task ID: NOPE-1"},
        {"id": "junk!", "error": "Invalid or unresolvable task ID: junk!"},
    ]
    if mode == "json":
        data = json.loads(result.stdout)["data"]
        assert data[f"{command}d"] == [task_id] and data["failed"] == failures
    elif mode == "quiet":
        assert result.stdout == f"{task_id}\n"
        assert len(result.stderr.splitlines()) == 2
    else:
        assert result.stderr.splitlines()[2:] == [
            f"  Failed {f['id']}: {f['error']}" for f in failures
        ]
