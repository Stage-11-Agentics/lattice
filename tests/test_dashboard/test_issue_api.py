"""Issue Inbox read and write translation boundaries (LAT-365)."""

from __future__ import annotations

import json
from pathlib import Path
from urllib.parse import urlencode

import pytest

from lattice.boards import resolve_board
from lattice.core.config import default_config, serialize_config
from lattice.dashboard import api
from lattice.ops import Caller, get_operation
from lattice.ops.task_attach import encode_payload
from lattice.storage.fs import atomic_write, ensure_lattice_dirs
from tests.issue_media_helpers import png


@pytest.fixture()
def issue_board(tmp_path: Path):  # noqa: ANN201
    ensure_lattice_dirs(tmp_path)
    lattice_dir = tmp_path / ".lattice"
    config = default_config()
    atomic_write(lattice_dir / "config.json", serialize_config(config))
    return resolve_board(tmp_path), lattice_dir, config


def enable_issues(lattice_dir: Path, config: dict) -> None:
    config["issues"] = {"enabled": True}
    atomic_write(lattice_dir / "config.json", serialize_config(config))


def file_issue(board, title: str, *, actor: str = "agent:qa") -> dict:  # noqa: ANN001
    result = board.execute(
        "issue.file",
        {"text": title},
        Caller(actor=actor, origin={"reported": {"os_user": "atin", "host": "Atlas"}}),
    )
    return result.value


def data(response: api.ApiResponse) -> object:
    assert response.envelope is not None
    return response.envelope["data"]


def test_issues_are_not_exposed_when_the_feature_is_disabled(issue_board) -> None:  # noqa: ANN001
    _board, lattice_dir, _config = issue_board
    response = api.route_get(lattice_dir, "/api/issues")
    assert response.status == 409
    assert response.envelope["error"]["code"] == "ISSUES_DISABLED"
    assert not (lattice_dir / "issues").exists()


def test_enabled_issue_list_and_detail_use_the_dashboard_contract(issue_board) -> None:  # noqa: ANN001
    board, lattice_dir, config = issue_board
    enable_issues(lattice_dir, config)
    issue = file_issue(board, "Header covers the close button\n\nAt 320px the text overlaps.")

    rows = data(api.route_get(lattice_dir, "/api/issues"))
    assert len(rows) == 1
    row = rows[0]
    assert row["id"] == issue["id"]
    assert row["title"] == "Header covers the close button"
    assert row["description"] == "At 320px the text overlaps."
    assert row["state"] == "open"
    assert "events" not in row and "comments" not in row

    detail = data(api.route_get(lattice_dir, f"/api/issues/{issue['id']}"))
    assert detail["title"] == row["title"]
    assert detail["description"] == row["description"]
    assert detail["filed_origin"] == {"user": "atin", "machine": "Atlas"}
    assert detail["events"][0]["type"] == "issue_filed"
    assert detail["comments"] == []
    assert all("path" not in media for media in detail["media"])


def test_by_filter_marks_file_activity_across_the_issue_list(issue_board) -> None:  # noqa: ANN001
    board, lattice_dir, config = issue_board
    enable_issues(lattice_dir, config)
    own = file_issue(board, "Filed by qa")
    file_issue(board, "Filed by someone else", actor="agent:other")

    rows = data(api.route_get(lattice_dir, "/api/issues", urlencode({"by": "agent:qa"})))
    assert [row["id"] for row in rows] == [own["id"]]
    assert rows[0]["matched_by"] == "filed"
    assert rows[0]["actor_activity_at"] == own["filed_at"]


def test_by_filter_keeps_full_human_session_keys_exact(issue_board) -> None:  # noqa: ANN001
    board, lattice_dir, config = issue_board
    enable_issues(lattice_dir, config)
    session_one = file_issue(board, "Atin session one", actor="human:Atin-1")
    file_issue(board, "Atin session two", actor="human:Atin-2")

    rows = data(api.route_get(lattice_dir, "/api/issues", urlencode({"by": "human:Atin-1"})))

    assert [row["id"] for row in rows] == [session_one["id"]]
    assert rows[0]["matched_by"] == "filed"


def test_by_filter_includes_nested_comments_and_unknown_origins(issue_board) -> None:  # noqa: ANN001
    board, lattice_dir, config = issue_board
    enable_issues(lattice_dir, config)
    issue = file_issue(board, "Commented issue", actor="agent:reporter")
    event_path = lattice_dir / "issues" / "events" / f"{issue['id']}.jsonl"
    events = [
        {
            "id": "event-comment-root",
            "type": "issue_comment_added",
            "ts": "2026-01-02T00:00:00Z",
            "actor": "human:atin",
            "origin": {"reported": {"os_user": "atin", "host": "Atlas"}},
            "data": {"body": "Top level"},
        },
        {
            "id": "event-comment-reply",
            "type": "issue_comment_added",
            "ts": "2026-01-03T00:00:00Z",
            "actor": "human:atin",
            "data": {"body": "Nested reply", "parent_id": "event-comment-root"},
        },
    ]
    with event_path.open("a", encoding="utf-8") as handle:
        handle.writelines(json.dumps(event, separators=(",", ":")) + "\n" for event in events)

    rows = data(api.route_get(lattice_dir, "/api/issues", urlencode({"by": "human:atin"})))
    assert len(rows) == 1
    assert rows[0]["id"] == issue["id"]
    assert rows[0]["matched_by"] == "commented"
    assert rows[0]["actor_comment_count"] == 2
    assert rows[0]["actor_activity_at"] == "2026-01-03T00:00:00Z"
    assert rows[0]["actor_comment_origins"] == [
        {"user": "atin", "machine": "Atlas"},
        None,
    ]
    assert rows[0]["comment_count"] == 2

    detail = data(api.route_get(lattice_dir, f"/api/issues/{issue['id']}"))
    assert detail["comments"][0]["replies"][0]["body"] == "Nested reply"
    assert detail["comments"][0]["replies"][0]["origin"] is None


def test_detail_media_uses_the_shared_media_route_and_hides_local_paths(issue_board) -> None:  # noqa: ANN001
    board, lattice_dir, config = issue_board
    enable_issues(lattice_dir, config)
    issue = board.execute(
        "issue.file",
        {"text": "Screenshot", "media": [{"payload": encode_payload("shot.png", png())}]},
        Caller(actor="human:atin"),
    ).value

    detail = data(api.route_get(lattice_dir, f"/api/issues/{issue['id']}"))
    media = detail["media"][0]
    assert media["url"] == f"/api/issues/{issue['id']}/media/{media['id']}"
    assert "path" not in media


def test_media_urls_are_built_only_from_validated_ids() -> None:
    detail = api._normalize_issue_detail(
        {
            "id": "iss_01ARZ3NDEKTSV4RRFFQ69G5FAV",
            "media": [
                {
                    "id": "med_bad/../id",
                    "path": "/tmp/media",
                    "frames": [{"path": "/tmp/t0000.000s.jpg"}],
                }
            ],
        }
    )
    assert detail["media"][0]["url"] is None
    assert detail["media"][0]["frames"][0]["url"] is None


def test_future_issue_reader_title_description_shape_is_preserved() -> None:
    detail = api._normalize_issue_detail(
        {"id": "iss_01ARZ3NDEKTSV4RRFFQ69G5FAV", "title": "Title", "description": "Body"}
    )
    assert detail["title"] == "Title"
    assert detail["description"] == "Body"


def test_file_translation_uses_the_registered_operation_signature(issue_board) -> None:  # noqa: ANN001
    request = api.translate_post(
        "/api/issues", {"title": "A title", "description": "Details", "media": []}
    )
    assert request.op_name == "issue.file"
    params = request.params
    fields = get_operation("issue.file").Params.__dataclass_fields__
    if "title" in fields:
        assert params == {"title": "A title", "description": "Details", "media": ()}
    else:
        assert params == {"text": "A title\n\nDetails", "media": ()}


def test_comment_translation_stays_on_the_registered_operation_boundary() -> None:
    request = api.translate_post("/api/issues/LAT-I1/comment", {"body": "Top-level note"})
    assert request.op_name == "issue.comment"
    assert request.params == {"issue": "LAT-I1", "text": "Top-level note"}

    with pytest.raises(api.ApiError, match="top-level"):
        api.translate_post(
            "/api/issues/LAT-I1/comment", {"body": "Reply", "parent_id": "comment-id"}
        )


def test_issue_ref_validation_and_by_length_are_stable(issue_board) -> None:  # noqa: ANN001
    _board, lattice_dir, config = issue_board
    enable_issues(lattice_dir, config)
    invalid = api.route_get(lattice_dir, "/api/issues/not-an-issue")
    assert invalid.status == 400
    assert invalid.envelope["error"]["code"] == "INVALID_ID"

    long = api.route_get(lattice_dir, "/api/issues", urlencode({"by": "x" * 257}))
    assert long.status == 400
    assert long.envelope["error"]["code"] == "VALIDATION_ERROR"
