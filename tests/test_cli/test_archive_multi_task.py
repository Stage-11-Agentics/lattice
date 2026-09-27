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


# ---------------------------------------------------------------------------
# Board- and storage-level refusals stay typed (review round 2)
# ---------------------------------------------------------------------------


def _plant(lattice_dir: Path, marker: str) -> None:
    if marker == "cache":
        (lattice_dir / "cache").mkdir(exist_ok=True)
        (lattice_dir / "cache" / "state.json").write_text(
            json.dumps({"remote": "studio", "project": "apollo"})
        )
    else:
        (lattice_dir / "hosted").mkdir(exist_ok=True)
        (lattice_dir / "hosted" / "owner.json").write_text(
            json.dumps({"server_id": "srv_1", "host": "atlas", "pid": 4242, "started_at": "x"})
        )


def _tree(lattice_dir: Path) -> dict[str, bytes]:
    """Every board file but lock files: the bytes a refusal must not change."""
    return {
        p.relative_to(lattice_dir).as_posix(): p.read_bytes()
        for p in sorted(lattice_dir.rglob("*"))
        if p.is_file() and "locks" not in p.relative_to(lattice_dir).parts
    }


_CODES = {"cache": "BOARD_IS_CACHE", "hosted": "BOARD_IS_HOSTED"}
_MESSAGES = {
    "BOARD_IS_CACHE": "read-only mirror of studio/apollo",
    "BOARD_IS_HOSTED": "owned by a Lattice server (srv_1 on atlas pid 4242)",
}


def _assert_typed(result, code: str, as_json: bool) -> None:  # noqa: ANN001
    assert result.exit_code == 1, result.output
    if as_json:
        error = json.loads(result.stdout)["error"]
        assert error["code"] == code
        assert _MESSAGES[code] in error["message"]
    else:
        assert result.stdout == ""
        assert result.stderr.startswith("Error: ")
        assert _MESSAGES[code] in result.stderr
        assert len(result.stderr.splitlines()) == 1


@pytest.mark.parametrize("as_json", [False, True], ids=["plain", "json"])
@pytest.mark.parametrize("marker", ["cache", "hosted"])
@pytest.mark.parametrize(
    "argv",
    [
        ["archive", "<a>", "<b>"],
        ["unarchive", "<a>", "<b>"],
        ["archive", "--stale"],
    ],
    ids=["archive-multi", "unarchive-multi", "archive-stale-empty"],
)
def test_unwritable_board_is_refused_typed(
    invoke,
    initialized_root: Path,
    marker: str,
    argv: list[str],
    as_json: bool,  # noqa: ANN001
) -> None:
    ids = {"<a>": _create(invoke, "one"), "<b>": _create(invoke, "two")}
    if argv[0] == "unarchive":
        invoke("archive", *ids.values(), "--actor", _ACTOR)
    lattice_dir = initialized_root / ".lattice"
    _plant(lattice_dir, marker)
    before = _tree(lattice_dir)

    args = [ids.get(a, a) for a in argv]
    result = invoke(*args, "--actor", _ACTOR, *(["--json"] if as_json else []))

    _assert_typed(result, _CODES[marker], as_json)
    assert _tree(lattice_dir) == before


@pytest.mark.parametrize("as_json", [False, True], ids=["plain", "json"])
def test_unwritable_board_is_refused_before_the_actor(
    invoke,
    initialized_root: Path,
    as_json: bool,  # noqa: ANN001
) -> None:
    _plant(initialized_root / ".lattice", "cache")
    result = invoke("archive", "--stale", *(["--json"] if as_json else []))
    _assert_typed(result, "BOARD_IS_CACHE", as_json)


@pytest.mark.parametrize("as_json", [False, True], ids=["plain", "json"])
def test_corrupt_log_among_several_ids_is_integrity_error(
    invoke,
    initialized_root: Path,
    as_json: bool,  # noqa: ANN001
) -> None:
    first, corrupt, last = (_create(invoke, t) for t in ("one", "two", "three"))
    lattice_dir = initialized_root / ".lattice"
    (lattice_dir / "events" / f"{corrupt}.jsonl").write_text("{not json\n")

    result = invoke(
        "archive", first, corrupt, last, "--actor", _ACTOR, *(["--json"] if as_json else [])
    )

    assert result.exit_code == 1, result.output
    if as_json:
        body = json.loads(result.stdout)
        assert body["ok"] is False and body["error"]["code"] == "INTEGRITY_ERROR"
        assert corrupt in body["error"]["message"]
    else:
        assert result.stdout == ""
        assert result.stderr.startswith("Error: ") and corrupt in result.stderr
    # The command stopped at the corrupt task: the one before moved, the one after did not.
    assert (lattice_dir / "archive" / "events" / f"{first}.jsonl").exists()
    assert (lattice_dir / "events" / f"{last}.jsonl").exists()
    assert not (lattice_dir / "archive" / "events" / f"{last}.jsonl").exists()
