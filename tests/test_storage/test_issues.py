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
    issue_detail,
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


def append(
    board: Path, issue_id: str, etype: str, data: dict, *, origin: dict | None = None
) -> dict:
    with issue_write_context(board, issue_id):
        snapshot = current_issue(board, issue_id)
        event = create_issue_event(etype, issue_id, "agent:qa", data)
        if origin is not None:
            event["origin"] = origin
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


def test_issue_without_a_snapshot_is_replayed_by_every_read(board: Path) -> None:
    """A crash between the first event and the snapshot: the issue still lists,
    resolves by its display ID, and links; the reads write nothing."""
    from lattice.boards import resolve_board
    from lattice.ops import Caller

    file_issue(board, "first")
    lost = file_issue(board, "lost in a crash")
    (board / "issues" / f"{lost['id']}.json").unlink()
    before = {p: p.read_bytes() for p in (board / "issues").rglob("*") if p.is_file()}

    assert [s["short_id"] for s in list_issue_snapshots(board)] == ["LAT-I1", "LAT-I2"]
    assert read_issue_snapshot(board, lost["id"]) == lost
    assert resolve_issue(board, "LAT-I2") == lost["id"]
    after = {p: p.read_bytes() for p in (board / "issues").rglob("*") if p.is_file()}
    assert after == before  # no reader wrote anything

    config = json.loads((board / "config.json").read_text())
    config["issues"] = {"enabled": True}
    (board / "config.json").write_text(json.dumps(config))
    local = resolve_board(board.parent)
    task = local.execute("task.create", {"title": "T"}, Caller(actor="agent:qa")).task
    result = local.execute(
        "issue.link", {"issue": "LAT-I2", "task": task["id"]}, Caller(actor="agent:qa")
    )
    assert result.value["state"] == "linked"
    assert (board / "issues" / f"{lost['id']}.json").exists()  # the write repaired it


def test_issue_detail_carries_origins_and_redacts_removed_media_names(board: Path) -> None:
    issue_id = generate_issue_id()
    seq = allocate_issue_seq(board, issue_id)
    filed = create_issue_event(
        "issue_filed",
        issue_id,
        "agent:qa",
        {"seq": seq, "short_id": format_issue_short_id("LAT", seq), "title": "Title"},
    )
    filed["origin"] = {
        "reported": {"os_user": "local-user", "host": "Hyperion"},
        "authenticated": {"user": "server-user", "machine": "Atlas"},
    }
    snapshot = apply_issue_event(None, filed)
    with issue_write_context(board, issue_id):
        write_issue_events(board, issue_id, [filed], snapshot)

    append(
        board,
        issue_id,
        "issue_media_added",
        {
            "media_id": "med_01K00000000000000000000001",
            "n": 1,
            "kind": "photo",
            "content_type": "image/png",
            "original_name": "sensitive-name.png",
            "size_bytes": 12,
            "sha256": "a" * 64,
        },
    )
    append(
        board,
        issue_id,
        "issue_media_removed",
        {"media_id": "med_01K00000000000000000000001", "reason": "private"},
    )
    append(
        board,
        issue_id,
        "issue_comment_added",
        {"body": "Reproduced"},
        origin={"reported": {"os_user": "atin", "host": "Hyperion"}},
    )

    detail = issue_detail(board, issue_id)
    assert detail is not None
    assert detail["title"] == "Title" and "text" not in detail
    assert detail["filed_origin"] == {"user": "server-user", "machine": "Atlas"}
    assert detail["comments"][0]["origin"] == {"user": "atin", "machine": "Hyperion"}
    assert "original_name" not in detail["events"][1]["data"]
    assert "sensitive-name.png" not in json.dumps(detail)


def test_an_unreadable_snapshot_is_replayed_and_reported(board: Path) -> None:
    good = file_issue(board, "good")
    bad = file_issue(board, "bad")
    path = board / "issues" / f"{bad['id']}.json"
    path.write_text("{not json")
    reported: list[Path] = []
    listed = list_issue_snapshots(board, on_unreadable=lambda p, _e: reported.append(p))
    assert listed == [good, bad] and reported == [path]
    assert read_issue_snapshot(board, bad["id"]) == bad
    assert path.read_text() == "{not json"  # reads wrote nothing

    # The log unreadable too: skipped (reported), or raised without a reporter.
    (board / "issues" / "events" / f"{bad['id']}.jsonl").write_text("{broken\n")
    reported.clear()
    assert list_issue_snapshots(board, on_unreadable=lambda p, _e: reported.append(p)) == [good]
    assert reported == [path]
    with pytest.raises(OpError):
        list_issue_snapshots(board)
    with pytest.raises(OpError):
        read_issue_snapshot(board, bad["id"])
