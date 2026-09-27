"""Doctor reports every short-ID problem, not only the first (SPEC §5, AC-28)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from click.testing import CliRunner

from lattice.cli.main import cli
from lattice.core.config import default_config, serialize_config
from lattice.core.events import serialize_event
from lattice.storage.fs import LATTICE_DIR, atomic_write, ensure_lattice_dirs
from lattice.storage.short_ids import save_id_index

MISSING_TASK = "task_01KMISSINGMISSINGMISSING00"


def _board(root: Path) -> Path:
    ensure_lattice_dirs(root)
    lattice_dir = root / LATTICE_DIR
    config = dict(default_config())
    config["project_code"] = "LAT"
    config["auto_code_review_on_transition"] = False
    config["auto_plan_review_on_transition"] = False
    atomic_write(lattice_dir / "config.json", serialize_config(config))
    save_id_index(lattice_dir, {"schema_version": 2, "next_seqs": {}, "map": {}})
    (lattice_dir / "events" / "_lifecycle.jsonl").touch()
    return lattice_dir


def _invoke(root: Path, *args: str):
    return CliRunner().invoke(cli, list(args), env={"LATTICE_ROOT": str(root)})


def _set_short_id(lattice_dir: Path, task_id: str, short_id: str) -> None:
    """Rewrite a task's creation event to carry *short_id* (and its snapshot)."""
    log = lattice_dir / "events" / f"{task_id}.jsonl"
    lines = log.read_text().splitlines()
    event = json.loads(lines[0])
    event["data"]["short_id"] = short_id
    lines[0] = serialize_event(event).rstrip("\n")
    log.write_text("\n".join(lines) + "\n")
    snapshot_path = lattice_dir / "tasks" / f"{task_id}.json"
    snapshot = json.loads(snapshot_path.read_text())
    snapshot["short_id"] = short_id
    snapshot_path.write_text(json.dumps(snapshot, sort_keys=True, indent=2) + "\n")


def _fixture(root: Path) -> tuple[Path, list[str]]:
    """Two unresolvable IDs, two duplicated IDs, and a counter below the logs.

    Tasks (sorted by ULID, the order doctor walks): LAT-1, LAT-2, LAT-3, LAT-4,
    LAT-5, LAT-6. Their logs are then rewritten so LAT-2 holds LAT-1's ID and
    LAT-4 holds LAT-3's ID (two duplicates). ``ids.json`` maps LAT-7 to a task
    that does not exist and omits the logged LAT-6 (two unresolvable IDs), and
    its counter says LAT-3 is next although the logs reach LAT-6.
    """
    lattice_dir = _board(root)
    ids = []
    for n in range(1, 7):
        result = _invoke(root, "create", f"Task {n}", "--actor", "human:test", "--json")
        assert result.exit_code == 0, result.output
        ids.append(json.loads(result.output)["data"]["id"])
    assert ids == sorted(ids)
    _set_short_id(lattice_dir, ids[1], "LAT-1")
    _set_short_id(lattice_dir, ids[3], "LAT-3")
    save_id_index(
        lattice_dir,
        {
            "schema_version": 2,
            "next_seqs": {"LAT": 3},
            "map": {
                "LAT-1": ids[0],
                "LAT-3": ids[2],
                "LAT-5": ids[4],
                "LAT-7": MISSING_TASK,
            },
        },
    )
    return lattice_dir, ids


def _alias_findings(root: Path) -> list[dict]:
    result = _invoke(root, "doctor", "--json")
    payload = json.loads(result.output)
    assert payload["ok"] is True
    return [f for f in payload["data"]["findings"] if f["check"] == "alias_integrity"]


def test_doctor_reports_all_five_short_id_findings(tmp_path: Path) -> None:
    _lattice_dir, ids = _fixture(tmp_path)
    messages = [f["message"] for f in _alias_findings(tmp_path)]

    duplicates = [m for m in messages if "duplicate authoritative short ID" in m]
    assert len(duplicates) == 2
    assert any(f"LAT-1: {ids[0]}" in m and ids[1] in m for m in duplicates)
    assert any(f"LAT-3: {ids[2]}" in m and ids[3] in m for m in duplicates)

    unmapped = [m for m in messages if "does not map it exactly" in m]
    assert unmapped == [
        f"Authoritative task {ids[5]} has short_id LAT-6 but ids.json does not map it "
        "exactly; run lattice rebuild --all"
    ]
    dangling = [m for m in messages if "without valid authority" in m]
    assert len(dangling) == 1 and "LAT-7" in dangling[0] and MISSING_TASK in dangling[0]

    counter = [m for m in messages if m.startswith("next_seqs['LAT']")]
    assert counter == [
        "next_seqs['LAT'] (3) is at or below the max short-ID seq in the event logs (6); "
        "run lattice rebuild --all"
    ]
    assert len(messages) == 5


def test_doctor_plain_output_lists_every_finding(tmp_path: Path) -> None:
    _lattice_dir, ids = _fixture(tmp_path)
    result = _invoke(tmp_path, "doctor")
    assert result.exit_code != 0  # duplicates are errors
    assert result.output.count("duplicate authoritative short ID") == 2
    assert "LAT-6 but ids.json does not map it exactly" in result.output
    assert "LAT-7" in result.output
    assert "is at or below the max short-ID seq in the event logs (6)" in result.output


def test_counter_behind_logs_alone_is_reported(tmp_path: Path) -> None:
    """A regressed counter with a consistent map is caught only through the logs."""
    lattice_dir = _board(tmp_path)
    ids = []
    for n in range(1, 4):
        result = _invoke(tmp_path, "create", f"Task {n}", "--actor", "human:test", "--json")
        ids.append(json.loads(result.output)["data"]["id"])
    save_id_index(
        lattice_dir,
        {
            "schema_version": 2,
            "next_seqs": {"LAT": 2},
            "map": {"LAT-1": ids[0], "LAT-2": ids[1], "LAT-3": ids[2]},
        },
    )
    # The map alone would say "not greater than max assigned seq (3)"; the log
    # finding replaces it rather than doubling it.
    assert [f["message"] for f in _alias_findings(tmp_path)] == [
        "next_seqs['LAT'] (2) is at or below the max short-ID seq in the event logs (3); "
        "run lattice rebuild --all"
    ]


def test_rebuild_all_names_every_short_id_problem_and_writes_nothing(tmp_path: Path) -> None:
    lattice_dir, _ids = _fixture(tmp_path)
    before = (lattice_dir / "ids.json").read_bytes()
    result = _invoke(tmp_path, "rebuild", "--all")
    assert result.exit_code != 0
    assert result.output.count("duplicate authoritative short ID") == 2
    assert (lattice_dir / "ids.json").read_bytes() == before


def test_healthy_board_has_no_short_id_findings(tmp_path: Path) -> None:
    _board(tmp_path)
    for n in range(1, 4):
        _invoke(tmp_path, "create", f"Task {n}", "--actor", "human:test")
    assert _alias_findings(tmp_path) == []


def _append(lattice_dir: Path, task_id: str, type_: str, data: dict) -> None:
    from lattice.core.events import create_event

    log = lattice_dir / "events" / f"{task_id}.jsonl"
    with log.open("a", encoding="utf-8") as handle:
        handle.write(serialize_event(create_event(type_, task_id, "human:test", data)))


def test_historical_duplicate_is_reported_when_final_aliases_differ(tmp_path: Path) -> None:
    """Task A: created LAT-1, then assigned LAT-2. Task B: created LAT-1.

    The final aliases (LAT-2, LAT-1) are distinct and the map and counter are
    consistent with them, but LAT-1 was issued twice.
    """
    lattice_dir = _board(tmp_path)
    ids = []
    for n in range(1, 3):
        result = _invoke(tmp_path, "create", f"Task {n}", "--actor", "human:test", "--json")
        ids.append(json.loads(result.output)["data"]["id"])
    task_a, task_b = ids
    _set_short_id(lattice_dir, task_b, "LAT-1")
    _append(lattice_dir, task_a, "task_short_id_assigned", {"short_id": "LAT-2"})
    assert _invoke(tmp_path, "rebuild", task_a).exit_code == 0
    save_id_index(
        lattice_dir,
        {
            "schema_version": 2,
            "next_seqs": {"LAT": 3},
            "map": {"LAT-1": task_b, "LAT-2": task_a},
        },
    )

    findings = _alias_findings(tmp_path)
    assert len(findings) == 1
    assert findings[0]["level"] == "error"
    message = findings[0]["message"]
    assert message.startswith("short ID LAT-1 was issued to more than one task:")
    assert task_a in message and task_b in message
    assert _invoke(tmp_path, "doctor").exit_code != 0


def test_counter_for_a_prefix_only_in_the_logs_is_checked(tmp_path: Path) -> None:
    """A historical prefix missing from next_seqs has the implicit counter 1."""
    lattice_dir = _board(tmp_path)
    result = _invoke(tmp_path, "create", "Task 1", "--actor", "human:test", "--json")
    task_id = json.loads(result.output)["data"]["id"]
    _append(lattice_dir, task_id, "x_import", {"short_id": "OLD-4"})

    assert [f["message"] for f in _alias_findings(tmp_path)] == [
        "next_seqs['OLD'] (unset, implicitly 1) is at or below the max short-ID seq in "
        "the event logs (4); run lattice rebuild --all"
    ]


@pytest.mark.parametrize("bad", [[], ["LAT-1"], {"id": "LAT-1"}], ids=["list", "list1", "object"])
def test_non_string_short_id_is_reported_not_crashed_on(tmp_path: Path, bad: object) -> None:
    """A replayable non-string alias is a malformed-ID finding; create still works."""
    lattice_dir = _board(tmp_path)
    ids = []
    for n in range(1, 3):
        result = _invoke(tmp_path, "create", f"Task {n}", "--actor", "human:test", "--json")
        ids.append(json.loads(result.output)["data"]["id"])
    log = lattice_dir / "events" / f"{ids[1]}.jsonl"
    lines = log.read_text().splitlines()
    event = json.loads(lines[0])
    event["data"]["short_id"] = bad
    lines[0] = serialize_event(event).rstrip("\n")
    log.write_text("\n".join(lines) + "\n")
    # A later assignment in another log also carries the malformed value.
    _append(lattice_dir, ids[0], "x_note", {"short_id": bad})

    plain = _invoke(tmp_path, "doctor")
    assert plain.exception is None or isinstance(plain.exception, SystemExit), plain.exception
    assert plain.exit_code == 1
    assert f"task {ids[1]} has malformed authoritative short ID {bad!r}" in plain.output

    result = _invoke(tmp_path, "doctor", "--json")
    assert result.exception is None or isinstance(result.exception, SystemExit)
    assert result.exit_code == 1
    payload = json.loads(result.output)
    assert payload["ok"] is True
    alias = [f for f in payload["data"]["findings"] if f["check"] == "alias_integrity"]
    malformed = [f for f in alias if "malformed authoritative short ID" in f["message"]]
    assert len(malformed) == 1 and malformed[0]["level"] == "error"
    assert f"task {ids[1]} has malformed authoritative short ID {bad!r}" in malformed[0]["message"]
    assert payload["data"]["summary"]["errors"] >= 1

    created = _invoke(tmp_path, "create", "Task 3", "--actor", "human:test", "--json")
    assert created.exit_code == 0, created.output
    assert json.loads(created.output)["data"]["short_id"] == "LAT-3"
