"""The journal and its epoch (SPEC §8.6, §8.2 rotation)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from lattice.server.journal import (
    Journal,
    JournalError,
    finish_rotation,
    fingerprint,
    line_hash,
    log_lengths,
    rotate_epoch,
)
from lattice.storage.fs import ensure_lattice_dirs
from lattice.storage.ownership import owning_board


@pytest.fixture()
def board(tmp_path: Path):
    ensure_lattice_dirs(tmp_path)
    board = tmp_path / ".lattice"
    (board / "events" / "task_A.jsonl").write_text('{"x":1}\n')
    with owning_board(board):
        yield board


def _entry(**extra):
    return {
        "op": "task.create",
        "op_id": "op_01J9Z00000000000000000000A",
        "fp": "f" * 32,
        "token_id": "tok_1",
        "task_id": None,
        "event_ids": [],
        "paths": [],
        "lengths": {},
        **extra,
    }


def test_create_starts_at_seq_zero_with_a_baseline(board: Path) -> None:
    journal = Journal.create(board)
    assert journal.head_seq == 0
    assert journal.epoch.startswith("ep_")
    meta = json.loads((board / "hosted" / "journal_meta.json").read_text())
    assert meta["baseline"] == {"events/_lifecycle.jsonl": 0, "events/task_A.jsonl": 8}
    assert meta["clean_shutdown"] is None
    assert (board / "hosted" / "journal.jsonl").read_bytes() == b""


def test_append_assigns_seq_ts_and_line_hashes(board: Path) -> None:
    journal = Journal.create(board)
    seq1, line1 = journal.append(_entry(lengths={"events/task_A.jsonl": 20}))
    seq2, _ = journal.append(_entry())
    assert (seq1, seq2) == (1, 2)
    assert line1["ts"].endswith("Z") and "." in line1["ts"]
    raw = (board / "hosted" / "journal.jsonl").read_bytes().splitlines()
    assert journal.line_hashes == [line_hash(r) for r in raw]
    assert journal.known_lengths["events/task_A.jsonl"] == 20
    reloaded = Journal.load(board)
    assert reloaded.head_seq == 2
    assert reloaded.line_hashes == journal.line_hashes
    assert reloaded.epoch == journal.epoch


def test_load_drops_a_torn_final_line(board: Path) -> None:
    journal = Journal.create(board)
    journal.append(_entry())
    path = board / "hosted" / "journal.jsonl"
    with open(path, "ab") as fh:
        fh.write(b'{"seq":2,"op"')
    reloaded = Journal.load(board)
    assert reloaded.head_seq == 1
    assert path.read_bytes().endswith(b"\n")
    assert len(path.read_bytes().splitlines()) == 1


def test_load_refuses_a_missing_or_corrupt_journal(board: Path) -> None:
    with pytest.raises(JournalError):
        Journal.load(board)
    journal = Journal.create(board)
    journal.append(_entry())
    path = board / "hosted" / "journal.jsonl"
    path.write_bytes(b"not json\n" + path.read_bytes())
    with pytest.raises(JournalError):
        Journal.load(board)


def test_rotation_keeps_the_old_journal_and_starts_at_seq_zero(board: Path) -> None:
    journal = Journal.create(board)
    journal.append(_entry())
    old = journal.epoch
    new = journal.rotate()
    assert new.epoch != old and new.head_seq == 0
    hosted = board / "hosted"
    assert (hosted / f"journal.{old}.jsonl").read_bytes().count(b"\n") == 1
    assert (hosted / "journal.jsonl").read_bytes() == b""
    assert not (hosted / "rotation.json").exists()
    assert Journal.load(board).epoch == new.epoch


@pytest.mark.parametrize("stop_after", ["marker", "rename", "meta"])
def test_an_interrupted_rotation_completes_from_its_marker(board: Path, stop_after: str) -> None:
    journal = Journal.create(board)
    journal.append(_entry())
    old = journal.epoch
    hosted = board / "hosted"
    marker = {"old_epoch": old, "new_epoch": "ep_01J9Z0000000000000000000ZZ"}
    (hosted / "rotation.json").write_text(json.dumps(marker))
    if stop_after in ("rename", "meta"):
        (hosted / "journal.jsonl").rename(hosted / f"journal.{old}.jsonl")
    if stop_after == "meta":
        meta = {"epoch": marker["new_epoch"], "created_at": "", "baseline": {}}
        (hosted / "journal_meta.json").write_text(json.dumps(meta))
    finished = finish_rotation(board)
    assert finished.epoch == marker["new_epoch"]
    assert (hosted / f"journal.{old}.jsonl").read_bytes().count(b"\n") == 1
    assert Journal.load(board).head_seq == 0
    assert not (hosted / "rotation.json").exists()
    # repeating it changes nothing
    (hosted / "rotation.json").write_text(json.dumps(marker))
    assert finish_rotation(board).epoch == marker["new_epoch"]


def test_rotation_without_a_readable_journal(board: Path) -> None:
    journal = rotate_epoch(board, old_epoch=None)
    assert Journal.load(board).epoch == journal.epoch


def test_fingerprint_is_canonical() -> None:
    a = fingerprint("task.create", {"b": 1, "a": 2}, "human:x", None, {}, None)
    b = fingerprint("task.create", {"a": 2, "b": 1}, "human:x", None, {}, None)
    assert a == b and len(a) == 32
    assert a != fingerprint("task.create", {"a": 2, "b": 1}, "human:y", None, {}, None)


def test_log_lengths_skips_non_durable_paths(board: Path) -> None:
    (board / "locks" / "x.jsonl").write_text("abc\n")
    (board / "hosted").mkdir(exist_ok=True)
    (board / "hosted" / "receipts.jsonl").write_text("abc\n")
    assert set(log_lengths(board)) == {"events/_lifecycle.jsonl", "events/task_A.jsonl"}
