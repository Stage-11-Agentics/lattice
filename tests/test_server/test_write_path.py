"""The journal line of a server write (SPEC §8.6): ``paths`` holds every durable path
changed; ``lengths`` only logs the write did nothing to but append (and that survive)."""

from __future__ import annotations

import json
from pathlib import Path

from lattice.server.floors import ShortIdFloors
from lattice.server.project import MutationTracker
from lattice.server.testing import ServerHandle
from tests.test_server.conftest import create_task, mint


def test_lengths_only_for_append_only_survivors(tmp_path: Path) -> None:
    board = tmp_path / ".lattice"
    board.mkdir()
    files = {
        "appended.jsonl": ["append", "append"],
        "created.jsonl": ["create"],
        "append_then_unlink.jsonl": ["append", "unlink"],
        "replace_then_append.jsonl": ["replace", "append"],
        "append_then_replace.jsonl": ["append", "replace"],
        "gone.jsonl": ["append"],
    }
    tracker = MutationTracker(board, "task.archive")
    for name, kinds in files.items():
        if name != "gone.jsonl":
            (board / name).write_text("0123456789")
        for kind in kinds:
            tracker(board.resolve() / name, kind)
    assert tracker.lengths() == {"appended.jsonl": 10}
    assert tracker.relative_paths() == sorted(files)
    assert tracker.kinds[board.resolve() / "replace_then_append.jsonl"] == ["replace", "append"]


def _journal(root: Path) -> list[dict]:
    path = root / "projects" / "alpha" / ".lattice" / "hosted" / "journal.jsonl"
    return [json.loads(line) for line in path.read_text().splitlines()]


def test_journal_lines_of_real_writes(server: ServerHandle, root: Path) -> None:
    token = mint(root)
    task = create_task(server, token)
    board = root / "projects" / "alpha" / ".lattice"
    log = f"events/{task['id']}.jsonl"
    status, _, body = server.op(
        "alpha", "task.comment", {"task": task["id"], "text": "hello"}, token=token
    )
    assert status == 200
    created, commented = _journal(root)
    assert created["seq"] == 1 and commented["seq"] == 2
    assert created["op"] == "task.create" and created["task_id"] == task["id"]
    assert created["token_id"].startswith("tok_") and len(created["fp"]) == 32
    assert log in created["paths"] and f"tasks/{task['id']}.json" in created["paths"]
    assert created["lengths"][log] < commented["lengths"][log]
    assert commented["lengths"][log] == (board / log).stat().st_size
    assert f"tasks/{task['id']}.json" not in commented["lengths"]
    assert commented["event_ids"] == [e["id"] for e in body["data"]["result"]["events"]]
    # a no-op still commits a journal line
    server.op("alpha", "xtest.sleep", {"ms": 0}, token=token)
    last = _journal(root)[-1]
    assert last["op"] == "xtest.sleep" and last["paths"] == [] and last["lengths"] == {}


def test_short_id_floors_follow_creates(server: ServerHandle, root: Path) -> None:
    token = mint(root)
    for _ in range(3):
        create_task(server, token)
    floors = server.project("alpha").floors
    assert floors.max_observed == {"ALP": 3} and floors.floor_for("ALP") == 4
    rescanned = ShortIdFloors.from_board(root / "projects" / "alpha" / ".lattice")
    assert rescanned.max_observed == {"ALP": 3}
    assert rescanned.floor_for("NEW") == 1
