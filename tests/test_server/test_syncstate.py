"""Unit tests for the sync path's memory: length history, the manifest, events, paths."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from lattice.core.errors import OpError
from lattice.server.journal import Journal
from lattice.server.syncstate import Manifest, check_file_path, entry_events, synced_files
from lattice.storage.ownership import owning_board


@pytest.fixture()
def board(tmp_path: Path) -> Path:
    lattice = tmp_path / ".lattice"
    (lattice / "events").mkdir(parents=True)
    (lattice / "hosted").mkdir()
    (lattice / "config.json").write_text("{}")
    (lattice / "events" / "a.jsonl").write_bytes(b"x" * 10)
    return lattice


def _line(journal: Journal, seq: int, paths: list[str], lengths: dict[str, int], **kw) -> dict:
    line = {"seq": seq, "paths": paths, "lengths": lengths, "event_ids": [], **kw}
    journal.accept(line, json.dumps(line).encode())
    return line


def test_length_history(board: Path) -> None:
    with owning_board(board):
        journal = Journal.create(board)
    assert journal.baseline == {"events/a.jsonl": 10}
    _line(journal, 1, ["events/a.jsonl"], {"events/a.jsonl": 20})
    _line(journal, 2, ["events/a.jsonl"], {})  # relocated: no append base
    _line(journal, 3, ["events/a.jsonl"], {"events/a.jsonl": 40})
    _line(journal, 4, ["events/b.jsonl"], {"events/b.jsonl": 5})
    _line(journal, 5, ["tasks/x.json"], {})
    assert [journal.length_at("events/a.jsonl", n) for n in range(5)] == [10, 20, None, 40, 40]
    assert journal.length_at("events/b.jsonl", 3) is None  # did not exist yet
    assert journal.length_at("events/b.jsonl", 4) == 5
    assert journal.is_log("events/b.jsonl") and not journal.is_log("tasks/x.json")
    assert journal.head == (journal.epoch, 5, journal.hash_at(5))


def test_offsets_survive_a_reload(board: Path) -> None:
    with owning_board(board):
        journal = Journal.create(board)
        for seq in (1, 2, 3):
            journal.append({"op": "external", "paths": [], "lengths": {}, "n": seq})
        reloaded = Journal.load(board)
    assert reloaded.line_offsets == journal.line_offsets
    assert reloaded.end_offset == (board / "hosted" / "journal.jsonl").stat().st_size
    assert [json.loads(raw)["n"] for _, raw in reloaded.read_lines(1)] == [2, 3]
    assert [seq for seq, _ in reloaded.read_lines(0, 2)] == [1, 2]


def test_incremental_hash_equals_a_full_hash(board: Path) -> None:
    manifest = Manifest.build(board)
    log = board / "events" / "a.jsonl"
    for chunk in (b"y" * 7, b"z" * 3_000_000):
        with open(log, "ab") as fh:
            fh.write(chunk)
        manifest.update(board, ["events/a.jsonl"], {"events/a.jsonl"})
        entry = manifest.get("events/a.jsonl")
        assert entry is not None
        assert entry.sha256 == hashlib.sha256(log.read_bytes()).hexdigest()
        assert entry.size == log.stat().st_size
    log.write_bytes(b"replaced")  # not an append: rehashed from byte 0
    manifest.update(board, ["events/a.jsonl"], set())
    assert manifest.get("events/a.jsonl").sha256 == hashlib.sha256(b"replaced").hexdigest()
    log.unlink()
    manifest.update(board, ["events/a.jsonl", "hosted/journal.jsonl"], set())
    assert manifest.get("events/a.jsonl") is None
    assert "hosted/journal.jsonl" not in manifest.entries


def test_synced_files_are_durable_and_workspace_only(board: Path) -> None:
    for rel in (
        "orchestration/run.md",
        "reviews/r.md",
        "locks/l",
        "cache/state.json",
        "events/.tmp.abc",
        "plans/p.md",
    ):
        (board / rel).parent.mkdir(parents=True, exist_ok=True)
        (board / rel).write_text("x")
    assert synced_files(board) == [
        "config.json",
        "events/a.jsonl",
        "orchestration/run.md",
        "plans/p.md",
    ]


def test_events_are_read_back_by_id_including_after_relocation(board: Path) -> None:
    with owning_board(board):
        journal = Journal.create(board)
    log = board / "events" / "t.jsonl"
    first = {"id": "ev_1", "type": "a"}
    second = {"id": "ev_2", "type": "b"}
    log.write_bytes(json.dumps(first).encode() + b"\n")
    line1 = _line(
        journal,
        1,
        ["events/t.jsonl"],
        {"events/t.jsonl": log.stat().st_size},
        event_ids=["ev_1"],
        task_id="t",
    )
    with open(log, "ab") as fh:
        fh.write(json.dumps(second).encode() + b"\n")
    line2 = _line(
        journal,
        2,
        ["events/t.jsonl"],
        {"events/t.jsonl": log.stat().st_size},
        event_ids=["ev_2"],
        task_id="t",
    )
    assert entry_events(board, journal, line1) == [first]
    assert entry_events(board, journal, line2) == [second]
    (board / "archive" / "events").mkdir(parents=True)
    log.rename(board / "archive" / "events" / "t.jsonl")
    assert entry_events(board, journal, line2) == [second]  # same offsets, relocated
    assert entry_events(board, journal, {**line1, "event_ids": []}) == []


@pytest.mark.parametrize(
    ("rel", "code"),
    [
        ("", "VALIDATION_ERROR"),
        ("../x", "VALIDATION_ERROR"),
        ("events/../../x", "VALIDATION_ERROR"),
        ("/etc/passwd", "VALIDATION_ERROR"),
        ("events//a", "VALIDATION_ERROR"),
        ("events/./a", "VALIDATION_ERROR"),
        ("events\\a", "VALIDATION_ERROR"),
        ("events/a\x00", "VALIDATION_ERROR"),
        ("hosted/journal.jsonl", "NOT_FOUND"),
        ("locks/x", "NOT_FOUND"),
        ("runner.log", "NOT_FOUND"),
        ("events/.tmp.x", "NOT_FOUND"),
    ],
)
def test_file_path_checks(rel: str, code: str) -> None:
    with pytest.raises(OpError) as caught:
        check_file_path(rel)
    assert caught.value.code == code


def test_board_paths_pass() -> None:
    for rel in ("events/a.jsonl", "config.json", "orchestration/x/y.md", "archive/plans/p.md"):
        assert check_file_path(rel) == rel
