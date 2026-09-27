"""History repair by appending events (SPEC §11, "A board doctor refuses").

EVALUATION's LAT-347 fixture row: ``history_fixture.build_fixture`` builds the
board; each test names the case it proves.
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest
from click.testing import CliRunner

from lattice.cli.main import cli
from lattice.core.events import create_event, serialize_event
from lattice.storage.fs import recording
from lattice.storage.operations import (
    AuthoritativeLogError,
    read_task_authority,
    resolve_task_authority,
)
from tests.test_storage.history_fixture import ABSENT, DamagedBoard, build_fixture

ACTOR = ("--actor", "human:t")


def run(root: Path, *args: str):
    return CliRunner().invoke(cli, list(args), env={"LATTICE_ROOT": str(root)})


def run_json(root: Path, *args: str) -> tuple[dict, int]:
    result = run(root, *args, "--json")
    return json.loads(result.output), result.exit_code


def fix(root: Path, *extra: str):
    return run(root, "doctor", "--fix", *ACTOR, *extra)


def summary(root: Path) -> dict:
    data, _ = run_json(root, "doctor")
    return data["data"]["summary"]


def authority(board: DamagedBoard, task_id: str):
    return read_task_authority(board.lattice, task_id)


def appended(before: dict[str, bytes], after: dict[str, bytes]) -> dict[str, list[dict]]:
    """Per log, the events appended after *before*'s bytes."""
    return {
        path: [json.loads(line) for line in after[path][len(before[path]) :].splitlines()]
        for path in after
        if after[path] != before[path]
    }


@pytest.fixture()
def fixture(tmp_path: Path) -> tuple[DamagedBoard, dict[str, str]]:
    return build_fixture(tmp_path)


# ---------------------------------------------------------------------------
# The fixture row
# ---------------------------------------------------------------------------


def test_fix_appends_only(fixture) -> None:
    """Every complete event stays byte-identical in place; no log moves (recorder)."""
    board, _ = fixture
    before = board.logs()
    touched: list[tuple[str, str]] = []

    def before_write(path: Path, kind: str) -> None:
        touched.append((str(path.relative_to(board.lattice.resolve())), kind))

    with recording(before_write):
        result = fix(board.root)
    assert result.exit_code == 0, result.output
    after = board.logs()
    assert sorted(after) == sorted(before)
    for path, raw in before.items():
        assert after[path].startswith(raw), path
    log_writes = [(p, k) for p, k in touched if p.split("/")[-1].startswith("task_")]
    log_writes = [(p, k) for p, k in log_writes if p.endswith(".jsonl")]
    assert log_writes and all(kind == "append" for _p, kind in log_writes), log_writes
    assert sorted({p for p, _k in log_writes}) == sorted(appended(before, after))


def test_doctor_clean_after_fix(fixture) -> None:
    board, _ = fixture
    assert summary(board.root)["errors"] > 0
    assert fix(board.root).exit_code == 0
    assert summary(board.root) | {"tasks": 0, "events": 0} == {
        "tasks": 0,
        "events": 0,
        "artifacts": 0,
        "resources": 0,
        "warnings": 0,
        "errors": 0,
    }
    data, code = run_json(board.root, "doctor")
    assert code == 0
    info = [f for f in data["data"]["findings"] if f["level"] == "info"]
    assert {f["check"] for f in info} == {"history_repair"}
    assert sum("stale from" in f["message"] for f in info) == 7
    assert sum("repaired by supersedes" in f["message"] for f in info) == 4


def test_frozen_tasks_accept_status(fixture) -> None:
    board, t = fixture
    for role in ("done", "back", "assign", "fields"):
        assert run(board.root, "status", t[role], "cancelled", *ACTOR).exit_code == 1
    assert fix(board.root).exit_code == 0
    for role in ("done", "back", "assign", "fields"):
        result = run(board.root, "status", t[role], "cancelled", *ACTOR)
        assert result.exit_code == 0, (role, result.output)
    assert run(board.root, "doctor").exit_code == 0


def test_restored_fields_match_pre_repair_snapshot(fixture) -> None:
    board, t = fixture
    shown = {role: board.snapshot(t[role]) for role in ("done", "back", "assign", "fields")}
    shown["archived"] = board.snapshot(t["archived"])
    assert fix(board.root).exit_code == 0
    for role, snap in shown.items():
        repaired = authority(board, t[role]).snapshot
        for name in ("status", "assigned_to", "tags", "title", "priority"):
            assert repaired.get(name) == snap.get(name), (role, name)
        assert repaired["custom_fields"].get("k") == snap["custom_fields"].get("k")
    # The absent custom key comes back as null, the closest reachable state.
    assert authority(board, t["fields"]).snapshot["custom_fields"] == {"k": None}


def test_erased_task_keeps_tombstone(fixture) -> None:
    board, t = fixture
    before = board.logs()
    assert fix(board.root).exit_code == 0
    added = appended(before, board.logs())[f"events/{t['erased']}.jsonl"]
    assert [e["type"] for e in added] == ["task_history_reconciled"]
    snap = authority(board, t["erased"]).snapshot
    assert snap["tombstoned"] is True
    assert snap["status"] == "review"  # the reconciled replay stands; nothing restored
    # t1 is erased and moved off its duplicate: reassignment only.
    added = appended(before, board.logs())[f"events/{t['t1']}.jsonl"]
    assert [e["type"] for e in added] == ["task_short_id_assigned"]
    assert authority(board, t["t1"]).snapshot["tombstoned"] is True


def test_archived_repair_lands_in_archive(fixture) -> None:
    board, t = fixture
    task_id = t["archived"]
    before = board.logs()
    assert fix(board.root).exit_code == 0
    added = appended(before, board.logs())
    assert [e["type"] for e in added[f"archive/events/{task_id}.jsonl"]] == [
        "status_changed",
        "task_history_reconciled",
    ]
    assert not (board.lattice / "events" / f"{task_id}.jsonl").exists()
    assert not (board.lattice / "tasks" / f"{task_id}.json").exists()
    on_disk = json.loads((board.lattice / "archive" / "tasks" / f"{task_id}.json").read_text())
    assert on_disk["status"] == "in_planning"
    assert authority(board, task_id).location == "archived"


def test_every_out_of_prefix_holder_moves(fixture) -> None:
    board, t = fixture
    assert fix(board.root).exit_code == 0
    for role, old in (("old7", "OLD-7"), ("old9a", "OLD-9"), ("old9b", "OLD-9")):
        snap = authority(board, t[role]).snapshot
        assert snap["short_id"].startswith("LAT-")
        assert authority(board, t[role]).events[-1]["data"] == {
            "short_id": snap["short_id"],
            "supersedes": old,
        }


def test_prefix_before_duplicates(fixture) -> None:
    board, t = fixture
    assert fix(board.root).exit_code == 0
    seq = {role: int(authority(board, t[role]).snapshot["short_id"][4:]) for role in t}
    prefix_moves = [seq[r] for r in ("old7", "old9a", "old9b")]
    duplicate_moves = [seq[r] for r in ("m1", "m3", "u2", "u3", "t1", "t3")]
    assert prefix_moves == [10, 11, 12]
    assert min(duplicate_moves) > max(prefix_moves)
    assert sorted(prefix_moves + duplicate_moves) == list(range(10, 19))


def test_old_new_printed_with_title(fixture) -> None:
    board, _ = fixture
    result = fix(board.root)
    assert result.exit_code == 0, result.output
    assert 'OLD-7 -> LAT-10  "Out of prefix"' in result.output
    assert 'LAT-2 -> LAT-13  "Mapped dup, earliest"' in result.output
    assert 'restore status: "backlog" -> "planned"' in result.output
    assert 'restore custom_fields.k: "new" -> null' in result.output
    assert "20 events: 6 reconciliations, 9 reassignments, 5 restores." in result.output
    assert "lattice rebuild --all clears the drift" in result.output


def test_fix_json_reports_the_repair(fixture) -> None:
    board, t = fixture
    data, code = run_json(board.root, "doctor", "--fix", *ACTOR)
    assert code == 0
    repair = data["data"]["history_repair"]
    assert repair["applied"] is True
    assert repair["counts"] == {
        "events": 20,
        "reconciliations": 6,
        "reassignments": 9,
        "restores": 5,
    }
    assert {"task_id": t["old7"], "title": "Out of prefix", "old": "OLD-7", "new": "LAT-10"} in (
        repair["reassigned"]
    )
    assert data["data"]["summary"]["errors"] == 0


def test_superseded_id_never_reissued(fixture) -> None:
    board, _ = fixture
    assert fix(board.root).exit_code == 0
    issued = set()
    for raw in board.logs().values():
        for line in raw.splitlines():
            data = json.loads(line)["data"]
            issued.update(v for k, v in data.items() if k in ("short_id", "supersedes"))
    for n in range(3):
        data, code = run_json(board.root, "create", f"New {n}", *ACTOR)
        assert code == 0
        assert data["data"]["short_id"] not in issued
    assert data["data"]["short_id"] == "LAT-21"


def test_second_fix_appends_nothing(fixture) -> None:
    board, _ = fixture
    assert fix(board.root).exit_code == 0
    before = board.logs()
    result = fix(board.root)
    assert result.exit_code == 0
    assert "History repair" not in result.output
    assert board.logs() == before


def test_fix_without_actor_lists_and_appends_nothing(fixture) -> None:
    board, _ = fixture
    before = board.logs()
    result = run(board.root, "doctor", "--fix")
    assert result.exit_code == 1
    assert "History repair would append 20 events:" in result.output
    assert "Run lattice doctor --fix --actor <you> to append these events." in result.output
    assert board.logs() == before
    data, code = run_json(board.root, "doctor", "--fix")
    assert code == 1
    assert data["data"]["history_repair"]["applied"] is False
    assert data["data"]["history_repair"]["counts"]["events"] == 20
    assert board.logs() == before


def test_actor_is_validated_before_anything_is_written(fixture) -> None:
    board, _ = fixture
    before = board.logs()
    data, code = run_json(board.root, "doctor", "--fix", "--actor", "no-colon")
    assert code == 1 and data["error"]["code"] == "INVALID_ACTOR"
    data, code = run_json(board.root, "doctor", "--actor", "human:t")
    assert code == 1 and data["error"]["code"] == "VALIDATION_ERROR"
    assert board.logs() == before


def test_truncated_final_record_trimmed_then_repaired(fixture) -> None:
    board, t = fixture
    with board.log(t["assign"]).open("a") as fh:
        fh.write('{"id": "ev_torn", "type": "comm')
    result = fix(board.root)
    assert result.exit_code == 0, result.output
    assert "Truncated final line" in result.output and "(fixed)" in result.output
    assert authority(board, t["assign"]).snapshot["assigned_to"] == "agent:z"
    assert run(board.root, "doctor").exit_code == 0


def test_unsupported_error_blocks_history_appends(fixture) -> None:
    board, t = fixture
    log = board.log(t["back"])
    lines = log.read_bytes().splitlines(keepends=True)
    log.write_bytes(lines[0] + b"not json\n" + b"".join(lines[1:]))
    before = board.logs()
    result = fix(board.root)
    assert result.exit_code == 1
    assert "History repair appends nothing" in result.output
    assert "Invalid JSON at line 2" in result.output
    assert board.logs() == before


def test_refuses_when_the_derived_rebuild_would_fail(fixture) -> None:
    """An ids.json key the rebuild cannot parse refuses before any append."""
    board, t = fixture
    index = json.loads((board.lattice / "ids.json").read_text())
    index["map"]["BAD!-2"] = t["back"]
    (board.lattice / "ids.json").write_text(json.dumps(index))
    before = board.logs()
    touched: list[tuple[Path, str]] = []
    with recording(lambda path, kind: touched.append((path, kind))):
        result = fix(board.root)
    assert result.exit_code == 1
    assert "History repair appends nothing" in result.output
    assert "derived rebuild would fail" in result.output and "BAD!-2" in result.output
    assert not [kind for path, kind in touched if kind == "append"]
    assert board.logs() == before


def test_refuses_stale_history_with_divergent_plan_same_task(fixture) -> None:
    board, t = fixture
    task_id = t["back"]
    (board.lattice / "archive" / "plans" / f"{task_id}.md").write_text("# something else\n")
    before = board.logs()
    result = fix(board.root)
    assert result.exit_code == 1
    assert "divergent active/archive plan files" in result.output
    assert board.logs() == before


def test_task_event_refuses_reconciled_type(fixture) -> None:
    board, t = fixture
    assert fix(board.root).exit_code == 0
    data, code = run_json(
        board.root, "event", t["m2"], "task_history_reconciled", "--data", "{}", *ACTOR
    )
    assert code == 1
    assert data["error"]["code"] == "VALIDATION_ERROR"
    assert "reserved" in data["error"]["message"]


# ---------------------------------------------------------------------------
# Duplicate keepers
# ---------------------------------------------------------------------------


KEEPER_CASES = {
    # variant: (holders erased, ids.json maps LAT-2 to, expected keeper)
    "mapped": ((), "h2", "h2"),
    "unmapped": ((), None, "h1"),
    "tombstoned_holder": (("h1",), "h1", "h2"),
    "stale_mapping": ((), "other", "h1"),
    "all_tombstoned": (("h1", "h2", "h3"), "h3", "h1"),
}


@pytest.mark.parametrize("case", KEEPER_CASES)
def test_duplicate_keeper(tmp_path: Path, case: str) -> None:
    erased, mapped, keeper = KEEPER_CASES[case]
    board = DamagedBoard(tmp_path)
    t = {name: board.task(f"Holder {name}", "LAT-2") for name in ("h1", "h2", "h3")}
    t["other"] = board.task("Other", "LAT-1")
    for name in erased:
        board.erase(t[name])
    board.ids_map = {"LAT-1": t["other"]}
    if mapped is not None:
        board.ids_map["LAT-2"] = t[mapped]
    board.next_seq = 3
    board.write_ids()
    result = fix(tmp_path)
    assert result.exit_code == 0, result.output
    ids = {name: authority(board, t[name]).snapshot["short_id"] for name in t}
    assert ids[keeper] == "LAT-2"
    moved = sorted(ids[n] for n in ("h1", "h2", "h3") if n != keeper)
    assert moved == ["LAT-3", "LAT-4"]
    assert ids["other"] == "LAT-1"
    assert run(tmp_path, "doctor").exit_code == 0


# ---------------------------------------------------------------------------
# Restoring events
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("shown", "expected"),
    [(ABSENT, None), (None, None), ("shown", "shown")],
    ids=["absent", "explicit_null", "populated"],
)
def test_custom_field_restore(tmp_path: Path, shown, expected) -> None:
    board = DamagedBoard(tmp_path)
    task_id = board.task("Custom", "LAT-1")
    board.append(task_id, "field_updated", {"field": "custom_fields.k", "from": "x", "to": "new"})
    board.show(task_id, custom__k=shown)
    board.ids_map = {"LAT-1": task_id}
    board.next_seq = 2
    board.write_ids()
    assert fix(tmp_path).exit_code == 0
    snap = authority(board, task_id).snapshot
    # field_updated only sets keys, so an absent key is restored as null.
    assert snap["custom_fields"] == {"k": expected}
    restores = [e for e in authority(board, task_id).events if e["actor"] == "human:t"]
    assert [e["type"] for e in restores] == ["field_updated", "task_history_reconciled"]
    assert restores[0]["data"] == {"field": "custom_fields.k", "from": "new", "to": expected}


def test_list_field_restore(tmp_path: Path) -> None:
    board = DamagedBoard(tmp_path)
    task_id = board.task("Tags", "LAT-1")
    board.append(task_id, "field_updated", {"field": "tags", "from": ["a"], "to": ["b"]})
    board.show(task_id, tags=["b", "c"])
    board.ids_map = {"LAT-1": task_id}
    board.next_seq = 2
    board.write_ids()
    assert fix(tmp_path).exit_code == 0
    assert authority(board, task_id).snapshot["tags"] == ["b", "c"]


@pytest.mark.parametrize("snapshot", ["missing", "brace", "null", "list", "invalid_utf8"])
def test_unreadable_snapshot(tmp_path: Path, snapshot: str) -> None:
    """No readable snapshot file: reconcile, restore nothing, rebuild the snapshot."""
    board = DamagedBoard(tmp_path)
    task_id = board.task("Unreadable", "LAT-1")
    board.append(task_id, "status_changed", {"from": "planned", "to": "review"})
    path = board.snapshot_path(task_id)
    if snapshot == "missing":
        path.unlink()
    else:
        path.write_bytes(
            {"brace": b"{", "null": b"null\n", "list": b"[1]\n", "invalid_utf8": b'{"\xff": 1}'}[
                snapshot
            ]
        )
    data, code = run_json(tmp_path, "doctor")
    assert code == 1  # the stale history, not a crash
    assert any(f.get("check") == "authoritative_log" for f in data["data"]["findings"])
    board.ids_map = {"LAT-1": task_id}
    board.next_seq = 2
    board.write_ids()
    result = fix(tmp_path)
    assert result.exit_code == 0, result.output
    added = [e for e in authority(board, task_id).events if e["actor"] == "human:t"]
    assert [e["type"] for e in added] == ["task_history_reconciled"]
    assert json.loads(path.read_text())["status"] == "review"
    assert run(tmp_path, "doctor").exit_code == 0


# ---------------------------------------------------------------------------
# The replay rule and bad reconciliations
# ---------------------------------------------------------------------------


def _board_with_valid_status(tmp_path: Path) -> tuple[DamagedBoard, str, dict]:
    board = DamagedBoard(tmp_path)
    task_id = board.task("Valid", "LAT-1")
    valid = board.append(task_id, "status_changed", {"from": "backlog", "to": "in_planning"})
    board.ids_map = {"LAT-1": task_id}
    board.next_seq = 2
    board.write_ids()
    return board, task_id, valid


def _append_raw(board: DamagedBoard, task_id: str, line: str) -> None:
    with board.log(task_id).open("a") as fh:
        fh.write(line)


def test_only_named_stale_events_are_accepted(tmp_path: Path) -> None:
    board = DamagedBoard(tmp_path)
    task_id = board.task("Two stale", "LAT-1")
    first = board.append(task_id, "status_changed", {"from": "planned", "to": "review"})
    board.append(task_id, "status_changed", {"from": "done", "to": "blocked"})
    named = create_event(
        "task_history_reconciled", task_id, "human:t", {"event_ids": [first["id"]]}
    )
    _append_raw(board, task_id, serialize_event(named))
    with pytest.raises(AuthoritativeLogError, match="from value does not match"):
        resolve_task_authority(board.lattice, task_id)
    lenient = resolve_task_authority(board.lattice, task_id, lenient=True)
    assert lenient.reconciled == (first["id"],) and len(lenient.stale) == 1


def test_reconciliation_naming_matching_event_is_doctor_error(tmp_path: Path) -> None:
    board, task_id, valid = _board_with_valid_status(tmp_path)
    event = create_event(
        "task_history_reconciled", task_id, "human:t", {"event_ids": [valid["id"]]}
    )
    _append_raw(board, task_id, serialize_event(event))
    data, code = run_json(tmp_path, "doctor")
    assert code == 1
    assert any("from value is not stale" in f["message"] for f in data["data"]["findings"])


def test_reconciliation_naming_absent_event_is_doctor_error(tmp_path: Path) -> None:
    board, task_id, _valid = _board_with_valid_status(tmp_path)
    event = create_event("task_history_reconciled", task_id, "human:t", {"event_ids": ["ev_gone"]})
    _append_raw(board, task_id, serialize_event(event))
    data, code = run_json(tmp_path, "doctor")
    assert code == 1
    assert any("absent from the log: ev_gone" in f["message"] for f in data["data"]["findings"])


@pytest.mark.parametrize("bad", ["matching", "absent", "malformed"])
def test_fix_refuses_bad_reconciliation(tmp_path: Path, bad: str) -> None:
    board, task_id, valid = _board_with_valid_status(tmp_path)
    board.append(task_id, "status_changed", {"from": "backlog", "to": "planned"})  # stale
    names = {"matching": [valid["id"]], "absent": ["ev_gone"], "malformed": "ev_x"}[bad]
    event = create_event("task_history_reconciled", task_id, "human:t", {"event_ids": names})
    _append_raw(board, task_id, serialize_event(event))
    before = board.logs()
    result = fix(tmp_path)
    assert result.exit_code == 1
    message = {
        "matching": "from value is not stale",
        "absent": "from value does not match",  # the unnamed stale event fails first
        "malformed": "must name a non-empty list of event IDs",
    }[bad]
    assert message in result.output
    assert "History repair appended" not in result.output
    assert board.logs() == before


def _escaped(event: dict) -> str:
    line = serialize_event(event)
    return line.replace('"task_history_reconciled"', '"task_history_reconcil\\u0065d"')


def test_escaped_type_reconciles(tmp_path: Path) -> None:
    board = DamagedBoard(tmp_path)
    task_id = board.task("Escaped", "LAT-1")
    stale = board.append(task_id, "status_changed", {"from": "planned", "to": "review"})
    event = create_event(
        "task_history_reconciled", task_id, "human:t", {"event_ids": [stale["id"]]}
    )
    _append_raw(board, task_id, _escaped(event))
    resolved = resolve_task_authority(board.lattice, task_id)
    assert resolved.reconciled == (stale["id"],)
    assert resolved.snapshot["status"] == "review"


def test_escaped_type_naming_matching_event_is_error(tmp_path: Path) -> None:
    board, task_id, valid = _board_with_valid_status(tmp_path)
    event = create_event(
        "task_history_reconciled", task_id, "human:t", {"event_ids": [valid["id"]]}
    )
    _append_raw(board, task_id, _escaped(event))
    with pytest.raises(AuthoritativeLogError, match="not stale"):
        resolve_task_authority(board.lattice, task_id)


# ---------------------------------------------------------------------------
# The physical log: two copies, and a log at the wrong placement
# ---------------------------------------------------------------------------


def _stale_task(tmp_path: Path) -> tuple[DamagedBoard, str]:
    board = DamagedBoard(tmp_path)
    task_id = board.task("Copies", "LAT-1")
    board.append(task_id, "status_changed", {"from": "planned", "to": "review"})
    board.ids_map = {"LAT-1": task_id}
    board.next_seq = 2
    board.write_ids()
    return board, task_id


@pytest.mark.parametrize("copy", ["identical", "prefix"])
def test_copy_repairs_in_place(tmp_path: Path, copy: str) -> None:
    """test_identical_copy_repairs_in_place / test_prefix_copy_repairs_in_place."""
    board, task_id = _stale_task(tmp_path)
    active = board.log(task_id)
    archived = board.lattice / "archive" / "events" / f"{task_id}.jsonl"
    shutil.copyfile(active, archived)  # both copies hold the stale event
    if copy == "prefix":
        # The active copy grows past the archived one, which stays its exact prefix.
        board.append(task_id, "status_changed", {"from": "review", "to": "blocked"})
    before = board.logs()
    result = fix(tmp_path)
    assert result.exit_code == 0, result.output
    after = board.logs()
    assert sorted(after) == sorted(before)
    grown = appended(before, after)
    assert list(grown) == [f"events/{task_id}.jsonl"]
    resolved = resolve_task_authority(board.lattice, task_id)
    assert resolved.snapshot["status"] == ("review" if copy == "identical" else "blocked")
    assert resolved.event_path == active
    assert run(tmp_path, "show", task_id).exit_code == 0
    data, _ = run_json(tmp_path, "doctor")
    assert data["data"]["summary"]["errors"] == 0


def test_wrong_placement_appends_to_existing_log(tmp_path: Path) -> None:
    """A task archived by its events whose only log is still in events/."""
    board, task_id = _stale_task(tmp_path)
    board.append(task_id, "task_archived", {})
    before = board.logs()
    assert list(before) == [f"events/{task_id}.jsonl"]
    result = fix(tmp_path)
    assert result.exit_code == 0, result.output
    after = board.logs()
    assert list(after) == list(before)
    assert after[f"events/{task_id}.jsonl"].startswith(before[f"events/{task_id}.jsonl"])
    resolved = resolve_task_authority(board.lattice, task_id)
    assert resolved.location == "archived"
    assert resolved.event_path == board.log(task_id)
