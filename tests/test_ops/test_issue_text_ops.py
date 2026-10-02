"""Issue title/description edits and comments (LAT-371) through operations."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from lattice.boards import LocalBoard, resolve_board
from lattice.core.events import create_issue_event
from lattice.core.issues import apply_issue_event, format_issue_short_id
from lattice.ops import Caller, OpError
from lattice.storage.issues import (
    allocate_issue_seq,
    issue_write_context,
    write_issue_events,
)


@pytest.fixture()
def board(initialized_root: Path, monkeypatch: pytest.MonkeyPatch) -> LocalBoard:
    monkeypatch.delenv("LATTICE_ROOT", raising=False)
    config_path = initialized_root / ".lattice" / "config.json"
    config = json.loads(config_path.read_text())
    config.update(project_code="LAT", issues={"enabled": True})
    config_path.write_text(json.dumps(config))
    return resolve_board(initialized_root)


def run(board: LocalBoard, op: str, **params):  # noqa: ANN003, ANN201
    return board.execute(op, params, Caller(actor="agent:qa"))


def file_old_issue(board: LocalBoard, text: str = "Old title\nOld detail") -> str:
    issue_id = "iss_01K00000000000000000000000"
    seq = allocate_issue_seq(board.lattice_dir, issue_id)
    event = create_issue_event(
        "issue_filed",
        issue_id,
        "agent:qa",
        {"seq": seq, "short_id": format_issue_short_id("LAT", seq), "text": text},
    )
    snapshot = apply_issue_event(None, event)
    with issue_write_context(board.lattice_dir, issue_id):
        write_issue_events(board.lattice_dir, issue_id, [event], snapshot)
    return issue_id


def test_issue_file_splits_title_and_keeps_operation_value_as_view(board: LocalBoard) -> None:
    raw_title = "An observation sentence. " + "x" * 140
    result = run(
        board,
        "issue.file",
        title=raw_title,
        description="Extra detail  \n",
    )
    event = result.events[0]
    assert event["type"] == "issue_filed"
    assert event["data"]["title"] == "An observation sentence."
    assert event["data"]["description"] == f"{raw_title}\n\nExtra detail"
    assert "text" not in event["data"]
    assert "title_shortened" not in result.value
    assert "text" not in result.value
    assert result.value["description"] == f"{raw_title}\n\nExtra detail"


def test_issue_file_rejects_an_empty_title_without_allocating_a_number(board: LocalBoard) -> None:
    with pytest.raises(OpError) as exc:
        run(board, "issue.file", title="  ")
    assert exc.value.code == "VALIDATION_ERROR"
    assert not (board.lattice_dir / "issues").exists()


def test_issue_edit_is_idempotent_and_materializes_an_old_issue(board: LocalBoard) -> None:
    issue_id = file_old_issue(board)
    result = run(board, "issue.edit", issue="LAT-I1", title="Corrected title")
    assert result.events[0]["type"] == "issue_edited"
    assert result.events[0]["data"] == {
        "from_title": "Old title",
        "title": "Corrected title",
        "from_description": "Old detail",
        "description": "Old detail",
    }
    assert result.value["title"] == "Corrected title"
    assert result.value["description"] == "Old detail"
    assert "text" not in result.value

    unchanged = run(board, "issue.edit", issue=issue_id, title="Corrected title")
    assert unchanged.idempotent and unchanged.events == []
    assert unchanged.value["title"] == "Corrected title"

    cleared = run(board, "issue.edit", issue="LAT-I1", description=" \n ")
    assert cleared.events[0]["data"] == {"from_description": "Old detail", "description": ""}
    assert cleared.value["description"] == ""


@pytest.mark.parametrize("title", ["x" * 121, "first\nsecond"])
def test_issue_edit_refuses_overlong_or_multiline_title(board: LocalBoard, title: str) -> None:
    run(board, "issue.file", title="T")
    with pytest.raises(OpError) as exc:
        run(board, "issue.edit", issue="LAT-I1", title=title)
    assert exc.value.code == "VALIDATION_ERROR"


def test_issue_comment_adds_and_replies_on_a_closed_issue_without_changing_links(
    board: LocalBoard,
) -> None:
    view = run(board, "issue.file", title="T").value
    run(board, "issue.dismiss", issue=view["short_id"], reason="not actionable")
    first = run(board, "issue.comment", issue=view["short_id"], text="  Reproduced.  ")
    first_id = first.events[0]["id"]
    assert first.events[0]["type"] == "issue_comment_added"
    assert first.events[0]["data"] == {"body": "Reproduced."}
    assert first.value["comment"]["id"] == first_id
    assert first.value["comment"]["body"] == "Reproduced."
    assert "replies" not in first.value["comment"]
    assert first.value["state"] == "dismissed"
    assert first.value["tasks"] == []

    reply = run(
        board,
        "issue.comment",
        issue=view["short_id"],
        text="Confirmed.",
        reply_to=first_id,
    )
    assert reply.events[0]["data"]["parent_id"] == first_id
    assert reply.value["comment"]["parent_id"] == first_id
    assert reply.value["comment"]["id"] == reply.events[0]["id"]
    assert "replies" not in reply.value["comment"]
    assert reply.value["comment_count"] == 2
    assert reply.value["state"] == "dismissed"

    with pytest.raises(OpError) as unknown:
        run(board, "issue.comment", issue=view["short_id"], text="x", reply_to="ev_unknown")
    assert unknown.value.code == "VALIDATION_ERROR"
    with pytest.raises(OpError) as nested:
        run(
            board,
            "issue.comment",
            issue=view["short_id"],
            text="x",
            reply_to=reply.events[0]["id"],
        )
    assert nested.value.code == "VALIDATION_ERROR"
