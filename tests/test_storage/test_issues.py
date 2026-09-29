"""The issue log's files (LAT-361): allocation, write and read, resolve, rebuild."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from lattice.core.errors import OpError
from lattice.core.events import create_issue_event
from lattice.core.ids import generate_issue_id
from lattice.core.issues import apply_issue_event, format_issue_short_id
from lattice.storage.issues import (
    allocate_issue_seq,
    current_issue,
    issue_write_context,
    list_issue_snapshots,
    load_issue_ids,
    read_issue_events,
    read_issue_snapshot,
    rebuild_issue_snapshots,
    resolve_issue,
    write_issue_events,
)


@pytest.fixture()
def board(initialized_root: Path) -> Path:
    return initialized_root / ".lattice"


def file_issue(board: Path, text: str = "Footer overlaps", code: str | None = "LAT") -> dict:
    issue_id = generate_issue_id()
    seq = allocate_issue_seq(board, issue_id)
    data = {"seq": seq, "short_id": format_issue_short_id(code, seq), "text": text}
    event = create_issue_event("issue_filed", issue_id, "agent:qa", data)
    snapshot = apply_issue_event(None, event)
    with issue_write_context(board, issue_id):
        write_issue_events(board, issue_id, [event], snapshot)
    return snapshot


def append(board: Path, issue_id: str, etype: str, data: dict) -> dict:
    with issue_write_context(board, issue_id):
        snapshot = current_issue(board, issue_id)
        event = create_issue_event(etype, issue_id, "agent:qa", data)
        snapshot = apply_issue_event(snapshot, event)
        write_issue_events(board, issue_id, [event], snapshot)
    return snapshot


def test_allocation_counts_up_and_leaves_the_task_index_alone(board: Path) -> None:
    task_index = board / "ids.json"
    task_index.write_text('{"map": {"LAT-1": "task_x"}, "next_seq": {"LAT": 2}}\n')
    before = task_index.read_bytes()
    seqs = [file_issue(board)["seq"] for _ in range(3)]
    assert seqs == [1, 2, 3]
    assert task_index.read_bytes() == before
    ids = load_issue_ids(board)
    assert ids["next_seq"] == 4
    assert sorted(ids["map"]) == ["1", "2", "3"]


def test_allocation_survives_a_stale_next_seq(board: Path) -> None:
    file_issue(board)
    file_issue(board)
    path = board / "issues" / "ids.json"
    data = json.loads(path.read_text())
    data["next_seq"] = 1  # a hand edit or a merge left it behind the map
    path.write_text(json.dumps(data))
    assert file_issue(board)["seq"] == 3


def test_write_then_read(board: Path) -> None:
    snap = file_issue(board)
    assert read_issue_snapshot(board, snap["id"]) == snap
    assert [e["type"] for e in read_issue_events(board, snap["id"])] == ["issue_filed"]
    assert list_issue_snapshots(board) == [snap]
    assert list((board / "events").glob("iss_*")) == []  # never beside task logs


def test_resolve_three_forms(board: Path) -> None:
    snap = file_issue(board)
    assert resolve_issue(board, "LAT-I1") == snap["id"]
    assert resolve_issue(board, "lat-i1") == snap["id"]
    assert resolve_issue(board, "I1") == snap["id"]
    assert resolve_issue(board, snap["id"].lower()) == snap["id"]


@pytest.mark.parametrize(
    ("raw", "code"),
    [
        ("OTHER-I1", "NOT_FOUND"),
        ("I9", "NOT_FOUND"),
        ("iss_01K00000000000000000000000", "NOT_FOUND"),
        ("LAT-1", "INVALID_ID"),
        ("nonsense", "INVALID_ID"),
    ],
)
def test_resolve_refusals(board: Path, raw: str, code: str) -> None:
    file_issue(board)
    with pytest.raises(OpError) as exc:
        resolve_issue(board, raw)
    assert exc.value.code == code


def test_rebuild_matches_the_incremental_files(board: Path) -> None:
    one = file_issue(board)
    file_issue(board, "second")
    append(board, one["id"], "issue_linked", {"task_id": "task_01K00000000000000000000001"})
    append(board, one["id"], "issue_dismissed", {"reason": "r"})
    issues = board / "issues"
    before = {p.name: p.read_bytes() for p in issues.glob("*.json")}
    (issues / f"{one['id']}.json").write_text("{}")  # a stale snapshot is repaired
    result = rebuild_issue_snapshots(board)
    assert len(result.rebuilt) == 2 and result.collisions == []
    assert {p.name: p.read_bytes() for p in issues.glob("*.json")} == before


def test_rebuild_reports_a_sequence_collision(board: Path) -> None:
    """Two clones of a board each filed issue 1, then merged."""
    one = file_issue(board)
    other_id = "iss_01A00000000000000000000000"  # sorts before one["id"]
    event = create_issue_event(
        "issue_filed", other_id, "agent:b", {"seq": 1, "short_id": "LAT-I1", "text": "twin"}
    )
    with issue_write_context(board, other_id):
        write_issue_events(board, other_id, [event], apply_issue_event(None, event))
    result = rebuild_issue_snapshots(board)
    assert result.collisions == [
        {"seq": 1, "issues": sorted([one["id"], other_id]), "mapped_to": other_id}
    ]
    assert load_issue_ids(board)["map"] == {"1": other_id}
    assert read_issue_snapshot(board, one["id"])["text"] == "Footer overlaps"
    assert resolve_issue(board, one["id"]) == one["id"]
