"""Issue Inbox read and write translation boundaries (LAT-365)."""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path
from urllib.parse import urlencode

import pytest

from lattice.boards import resolve_board
from lattice.core.config import default_config, serialize_config
from lattice.dashboard import api
from lattice.ops import Caller, get_operation
from lattice.ops.task_attach import encode_payload
from lattice.storage.fs import atomic_write, ensure_lattice_dirs
from tests.issue_media_helpers import jpeg, mp4, png


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
    board.execute(
        "issue.comment",
        {"issue": issue["id"], "text": "Top level"},
        Caller(actor="human:atin", origin={"reported": {"os_user": "atin", "host": "Atlas"}}),
    )
    root_id = data(api.route_get(lattice_dir, f"/api/issues/{issue['id']}"))["comments"][0]["id"]
    board.execute(
        "issue.comment",
        {"issue": issue["id"], "text": "Nested reply", "reply_to": root_id},
        Caller(actor="human:atin"),
    )

    rows = data(api.route_get(lattice_dir, "/api/issues", urlencode({"by": "human:atin"})))
    assert len(rows) == 1
    assert rows[0]["id"] == issue["id"]
    assert rows[0]["matched_by"] == "commented"
    assert rows[0]["actor_comment_count"] == 2
    stored = data(api.route_get(lattice_dir, f"/api/issues/{issue['id']}"))["comments"][0]
    assert rows[0]["actor_activity_at"] == stored["replies"][0]["created_at"]
    assert len(rows[0]["actor_comment_origins"]) == 2
    assert rows[0]["actor_comment_origins"][0] == {"user": "atin", "machine": "Atlas"}
    assert rows[0]["comment_count"] == 2

    detail = data(api.route_get(lattice_dir, f"/api/issues/{issue['id']}"))
    assert detail["comments"][0]["replies"][0]["body"] == "Nested reply"
    assert detail["comments"][0]["origin"] == {"user": "atin", "machine": "Atlas"}


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


def test_comment_with_reply_to_is_refused_not_posted_top_level() -> None:
    with pytest.raises(api.ApiError, match="top-level"):
        api.translate_post("/api/issues/LAT-I1/comment", {"body": "Reply", "reply_to": "c1"})


def test_file_text_is_held_to_the_ordinary_write_limit_and_media_count_is_bounded() -> None:
    big = "x" * (api.MAX_REQUEST_BODY_BYTES + 1)
    with pytest.raises(api.ApiError) as too_big:
        api.translate_post("/api/issues", {"title": "t", "description": big})
    assert too_big.value.status == 413
    media = [{"payload": {}}] * (api.MAX_ISSUE_FILE_MEDIA_ITEMS + 1)
    with pytest.raises(api.ApiError, match="At most"):
        api.translate_post("/api/issues", {"title": "t", "media": media})


def test_a_video_with_unknown_duration_is_filed_without_it(issue_board) -> None:  # noqa: ANN001
    board, lattice_dir, config = issue_board
    enable_issues(lattice_dir, config)
    request = api.translate_post(
        "/api/issues",
        {
            "title": "WebM",
            "media": [
                {
                    "payload": encode_payload("rec.mp4", mp4()),
                    "video": {"width": 640, "height": 360, "duration_ms": None},
                }
            ],
        },
    )
    assert request.params["media"][0]["video"] == {"width": 640, "height": 360}
    result = board.execute("issue.file", request.params, Caller(actor="human:atin"))
    assert result.value["media"][0]["width"] == 640


def test_one_unreadable_issue_is_skipped_not_a_500(issue_board) -> None:  # noqa: ANN001
    board, lattice_dir, config = issue_board
    enable_issues(lattice_dir, config)
    good = file_issue(board, "Readable")
    bad = file_issue(board, "Will be damaged")
    (lattice_dir / "issues" / f"{bad['id']}.json").write_text("{not json")
    (lattice_dir / "issues" / "events" / f"{bad['id']}.jsonl").write_text("{not json\n")

    rows = data(api.route_get(lattice_dir, "/api/issues"))
    assert [row["id"] for row in rows] == [good["id"]]
    by = data(api.route_get(lattice_dir, "/api/issues", urlencode({"by": "agent:qa"})))
    assert [row["id"] for row in by] == [good["id"]]


def test_dashboard_video_goes_through_the_cli_media_step(  # noqa: ANN001
    monkeypatch, tmp_path
) -> None:
    from lattice.dashboard import media_prep
    from lattice.integrations import ffmpeg as ffmpeg_mod

    seen = {}

    def fake_prepare(src, content, content_type, sha256):  # noqa: ANN001, ANN202
        seen["name"] = src.name
        return ffmpeg_mod.PreparedVideo(
            b"stripped-" + content[:8],
            content_type,
            video={"width": 2, "height": 2, "duration_ms": 1000},
            frames=[(0, jpeg())],
            converted_from={"content_type": content_type, "size_bytes": 1, "sha256": "a" * 64},
            notes=[("remuxed", "metadata_stripped")],
        )

    monkeypatch.setattr(ffmpeg_mod, "prepare_video", fake_prepare)
    item = {
        "payload": encode_payload("clip.mp4", mp4()),
        "video": {"width": 640, "height": 360, "duration_ms": 5},
        "frames": [{"t_ms": 0, "payload": encode_payload("t0000.000s.jpg", jpeg())}],
    }
    [prepared] = media_prep.prepare_issue_media(
        [item, {"payload": encode_payload("a.png", png())}]
    )[:1]
    assert seen["name"].endswith(".mp4")
    assert prepared["video"] == {"width": 2, "height": 2, "duration_ms": 1000}
    assert prepared["converted_from"]["sha256"] == "a" * 64
    from lattice.ops.task_attach import decode_payload

    assert decode_payload(prepared["payload"])[1].startswith(b"stripped-")


@pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="needs ffmpeg")
def test_dashboard_video_loses_its_location_tags(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.delenv("LATTICE_FFMPEG")  # the suite turns ffmpeg off; this test needs it
    from lattice.dashboard import media_prep
    from lattice.ops.task_attach import decode_payload

    src = tmp_path / "geo.mp4"
    subprocess.run(
        [
            "ffmpeg", "-v", "error", "-f", "lavfi", "-i", "testsrc=duration=1:size=64x48:rate=5",
            "-pix_fmt", "yuv420p", "-metadata", "location=+37.7749-122.4194/",
            "-metadata", "title=home", str(src),
        ],
        check=True,
    )  # fmt: skip
    [prepared] = media_prep.prepare_issue_media(
        [{"payload": encode_payload("geo.mp4", src.read_bytes())}]
    )
    out = tmp_path / "out.mp4"
    out.write_bytes(decode_payload(prepared["payload"])[1])
    tags = subprocess.run(
        ["ffprobe", "-v", "error", "-show_format", str(out)], capture_output=True, text=True
    ).stdout
    assert "location" not in tags.lower() and "37.7749" not in tags
    assert prepared.get("frames")
