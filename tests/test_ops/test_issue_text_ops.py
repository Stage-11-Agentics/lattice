"""Issue title/description edits and comments (LAT-371) through operations."""

from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from lattice.boards import LocalBoard, resolve_board
from lattice.core.events import create_issue_event
from lattice.core.issues import apply_issue_event, format_issue_short_id
from lattice.ops import Caller, OpError
from lattice.storage.issues import (
    allocate_issue_seq,
    issue_write_context,
    list_issue_snapshots,
    read_issue_events,
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


def test_issue_file_accepts_the_legacy_text_operation_parameter(board: LocalBoard) -> None:
    result = run(board, "issue.file", text="Legacy title\nLegacy detail")

    assert result.events[0]["data"]["title"] == "Legacy title"
    assert result.events[0]["data"]["description"] == "Legacy detail"
    assert "text" not in result.events[0]["data"]


@pytest.mark.parametrize(
    ("params", "message", "details"),
    [
        pytest.param(
            {"title": "current", "text": "legacy"},
            "Provide title or legacy text, not both.",
            {},
            id="both-title-and-text",
        ),
        pytest.param(
            {"text": " \t "},
            "Issue title must not be empty.",
            {},
            id="whitespace-only-text",
        ),
        pytest.param(
            {},
            "Issue title is required.",
            {"reason": "MISSING_PARAM", "param": "title"},
            id="missing-title",
        ),
    ],
)
def test_issue_file_rejects_invalid_title_inputs_before_number_allocation(
    board: LocalBoard, params: dict, message: str, details: dict | None
) -> None:
    with pytest.raises(OpError) as exc:
        run(board, "issue.file", **params)

    assert exc.value.code == "VALIDATION_ERROR"
    assert exc.value.message == message
    assert exc.value.details == details
    assert not (board.lattice_dir / "issues").exists()


def test_issue_file_rejects_an_empty_title_without_allocating_a_number(board: LocalBoard) -> None:
    with pytest.raises(OpError) as exc:
        run(board, "issue.file", title="  ")
    assert exc.value.code == "VALIDATION_ERROR"
    assert not (board.lattice_dir / "issues").exists()


def test_issue_file_deduplicates_normalized_source_ref_before_reading_retry_media(
    board: LocalBoard,
) -> None:
    first = run(
        board,
        "issue.file",
        title="Original title",
        source="  reporter-links  ",
        source_ref="  ISS-7K2MQ  ",
    )
    retry = run(
        board,
        "issue.file",
        title="Changed retry title",
        source="reporter-links",
        source_ref="ISS-7K2MQ",
        media=(
            {
                "payload": {
                    "filename": "retry.png",
                    "sha256": "not-a-hash",
                    "size": 3,
                    "staged": True,
                }
            },
        ),
    )

    assert first.value["source"] == "reporter-links"
    assert first.value["source_ref"] == "ISS-7K2MQ"
    assert retry.value["id"] == first.value["id"]
    assert retry.value["title"] == "Original title"
    assert retry.value["deduplicated"] is True
    assert retry.idempotent is True
    assert retry.events == []
    snapshots = list_issue_snapshots(board.lattice_dir)
    assert len(snapshots) == 1
    assert [
        event["type"] for event in read_issue_events(board.lattice_dir, first.value["id"])
    ] == ["issue_filed"]


@pytest.mark.parametrize(
    ("params", "message"),
    [
        ({"source_ref": "R-1"}, "requires a nonempty source"),
        ({"source": "feed", "source_ref": "   "}, "source_ref must not be blank"),
        ({"source": "  ", "source_ref": "R-1"}, "source must not be blank"),
        ({"source": "feed\nother", "source_ref": "R-1"}, "control character"),
        ({"source": "feed", "source_ref": "R\x7f1"}, "control character"),
        ({"source": "x" * 129, "source_ref": "R-1"}, "128 characters"),
        ({"source": "feed", "source_ref": "x" * 257}, "256 characters"),
    ],
)
def test_issue_file_rejects_invalid_source_ref_keys(
    board: LocalBoard, params: dict, message: str
) -> None:
    with pytest.raises(OpError) as exc:
        run(board, "issue.file", title="T", **params)
    assert exc.value.code == "VALIDATION_ERROR"
    assert message in exc.value.message
    assert list_issue_snapshots(board.lattice_dir) == []


def test_issue_file_accepts_a_free_form_reporter_only_for_filing(board: LocalBoard) -> None:
    result = run(
        board,
        "issue.file",
        title="T",
        on_behalf_of="  Alex Example <alex@example.test>  ",
    )
    assert result.events[0]["provenance"]["on_behalf_of"] == "Alex Example <alex@example.test>"
    assert result.value["on_behalf_of"] == "Alex Example <alex@example.test>"

    for invalid in ("  ", "name\nforged", "x" * 257):
        with pytest.raises(OpError) as exc:
            run(board, "issue.file", title="invalid reporter", on_behalf_of=invalid)
        assert exc.value.code == "VALIDATION_ERROR"

    with pytest.raises(OpError) as other_op:
        run(
            board,
            "issue.comment",
            issue=result.value["id"],
            text="comment",
            on_behalf_of="Alex Example",
        )
    assert other_op.value.code == "INVALID_ACTOR"


def test_source_ref_retry_returns_a_dismissed_issue_without_reopening_it(
    board: LocalBoard,
) -> None:
    filed = run(
        board,
        "issue.file",
        title="Duplicate report",
        source="mail",
        source_ref="message-7",
    )
    dismissed = run(
        board,
        "issue.dismiss",
        issue=filed.value["id"],
        reason="not actionable",
    )
    retry = run(
        board,
        "issue.file",
        title="Retry",
        source="mail",
        source_ref="message-7",
    )
    assert retry.value["id"] == dismissed.value["id"]
    assert retry.value["state"] == "dismissed"
    assert retry.value["closure"] == dismissed.value["closure"]
    assert retry.events == [] and retry.idempotent


def test_source_ref_concurrent_distinct_operations_commit_one_issue_event(
    board: LocalBoard,
) -> None:
    def file_one(title: str):  # noqa: ANN202
        return board.execute(
            "issue.file",
            {"title": title, "source": "mail", "source_ref": "message-race"},
            Caller(actor="agent:qa"),
        )

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(file_one, ("first", "second")))

    assert sorted(result.idempotent for result in results) == [False, True]
    issue_id = results[0].value["id"]
    assert {result.value["id"] for result in results} == {issue_id}
    assert [event["type"] for event in read_issue_events(board.lattice_dir, issue_id)] == [
        "issue_filed"
    ]
    assert len(list_issue_snapshots(board.lattice_dir)) == 1


def test_filing_context_sets_external_marker_in_shared_issue_view(board: LocalBoard) -> None:
    result = board.execute(
        "issue.file",
        {"title": "Untrusted report", "source": "reporter-link", "source_ref": "link-7"},
        Caller(actor="agent:intake", filing_only=True),
    )
    assert result.value["external"] is True
    assert result.value["source_ref"] == "link-7"
    assert result.events[0]["data"]["external"] is True


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
