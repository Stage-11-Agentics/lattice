"""Issue titles, descriptions and comments (LAT-371), without filesystem I/O."""

from __future__ import annotations

import json

import pytest

from lattice.core.comments import format_comment_lines
from lattice.core.events import create_issue_event
from lattice.core.issues import (
    TaskInfo,
    actor_with_origin,
    apply_issue_event,
    check_edit_title,
    issue_comment_events,
    issue_comments,
    issue_title_description,
    issue_view,
    linked_issue_summary,
    normalize_issue_description,
    promote_description,
    replay_issue,
    serialize_issue_snapshot,
    split_title,
)

ISSUE_ID = "iss_01K00000000000000000000000"
TASK_ID = "task_01K00000000000000000000001"


def event(
    kind: str,
    data: dict,
    n: int,
    actor: str | dict = "agent:qa",
    *,
    origin: dict | None = None,
) -> dict:
    value = create_issue_event(
        kind,
        ISSUE_ID,
        actor,
        data,
        event_id=f"ev_{n:026d}",
        ts=f"2026-10-02T10:00:{n:02d}Z",
    )
    if origin is not None:
        value["origin"] = origin
    return value


def old_issue(text: str = "A short title\nMore detail") -> dict:
    return apply_issue_event(
        None,
        event("issue_filed", {"seq": 7, "short_id": "LAT-I7", "text": text}, 1),
    )


@pytest.mark.parametrize(
    ("raw", "limit", "expected_title", "expected_description", "shortened"),
    [
        ("  One line  ", 120, "One line", "", False),
        ("First line\n\nSecond line\n", 120, "First line", "Second line", False),
        (
            "a" * 42 + "; " + "b" * 30 + ". " + "c" * 70,
            120,
            "a" * 42 + "; " + "b" * 30 + ".",
            "a" * 42 + "; " + "b" * 30 + ". " + "c" * 70,
            True,
        ),
        ("a" * 20 + " " + "b" * 105, 120, "a" * 20, "a" * 20 + " " + "b" * 105, True),
        ("x" * 150, 120, "x" * 120, "x" * 150, True),
        (" \n \t ", 120, "", "", False),
        ("x" * 120, 120, "x" * 120, "", False),
    ],
)
def test_split_title(raw, limit, expected_title, expected_description, shortened) -> None:  # noqa: ANN001
    assert split_title(raw, limit) == (expected_title, expected_description, shortened)


def test_split_title_real_lattice_issues_is_a_frozen_read_rule() -> None:
    texts = {
        "LAT-I1": "An auto-fired review that fails on the account's usage limit raises needs-human on the task, though no human decision is needed; the flag reads as a problem with the plan or code",
        "LAT-I2": "The auto-fired code review timed out at 600 s on a 2,700-line diff while the machine's load average was 20 to 47; the timeout does not account for load, and the failure looks the same as a hung reviewer",
        "LAT-I3": "A worktree venv made with uv pip install -e drifts from uv.lock (anyio 4.15.1 against 4.12.1, ruff 0.16.9 against 0.15.1); server tests then fail on an import error unrelated to the change under test",
        "LAT-I4": "lattice list prints its columns unpadded, so rows do not line up when short IDs or statuses differ in length; issue list now pads, so the two read differently",
    }
    expected = {
        "LAT-I1": "An auto-fired review that fails on the account's usage limit raises needs-human on the task, though no human decision is",
        "LAT-I2": "The auto-fired code review timed out at 600 s on a 2,700-line diff while the machine's load average was 20 to 47",
        "LAT-I3": "A worktree venv made with uv pip install -e drifts from uv.lock (anyio 4.15.1 against 4.12.1, ruff 0.16.9 against",
        "LAT-I4": "lattice list prints its columns unpadded, so rows do not line up when short IDs or statuses differ in length",
    }
    for short_id, text in texts.items():
        snap = apply_issue_event(
            None,
            event("issue_filed", {"seq": 1, "short_id": short_id, "text": text}, 1),
        )
        assert issue_title_description(snap) == (expected[short_id], text)


def test_title_and_description_replay_for_new_and_old_issues() -> None:
    legacy = old_issue()
    assert "title" not in legacy and "description" not in legacy
    assert issue_title_description(legacy) == ("A short title", "More detail")

    new = apply_issue_event(
        None,
        event(
            "issue_filed", {"seq": 8, "short_id": "LAT-I8", "title": "T", "description": "D"}, 2
        ),
    )
    assert new["title"] == "T" and new["description"] == "D"
    assert "text" not in new
    assert issue_title_description(new) == ("T", "D")


def test_edit_replay_materializes_an_old_issue_and_applies_new_values() -> None:
    legacy = old_issue()
    changed = apply_issue_event(
        legacy,
        event(
            "issue_edited",
            {
                "from_title": "A short title",
                "title": "A corrected title",
                "from_description": "More detail",
                "description": "More detail",
            },
            2,
        ),
    )
    assert changed["title"] == "A corrected title"
    assert changed["description"] == "More detail"
    assert "text" not in changed

    changed_again = apply_issue_event(
        changed,
        event("issue_edited", {"from_title": "A corrected title", "title": "Final"}, 3),
    )
    assert (changed_again["title"], changed_again["description"]) == ("Final", "More detail")


def test_comment_replay_count_and_old_snapshot_shape() -> None:
    legacy = old_issue()
    assert "comment_count" not in legacy
    once = apply_issue_event(legacy, event("issue_comment_added", {"body": "hi"}, 2))
    twice = apply_issue_event(once, event("issue_comment_added", {"body": "again"}, 3))
    assert once["comment_count"] == 1
    assert twice["comment_count"] == 2
    assert "text" in twice and "title" not in twice


def test_issue_comments_adapt_events_and_keep_origins() -> None:
    actor = {"name": "Argus-3", "base_name": "Argus", "session": "sess_1", "model": "m"}
    root = event(
        "issue_comment_added",
        {"body": "First\nline"},
        2,
        actor,
        origin={"authenticated": {"user": "atin", "machine": "Atlas"}},
    )
    reply = event(
        "issue_comment_added",
        {"body": "Reply", "parent_id": root["id"]},
        3,
        "human:atin",
        origin={"reported": {"os_user": "atin", "host": "Hyperion"}},
    )
    source = [
        event("issue_filed", {"text": "old"}, 1),
        event("issue_linked", {"task_id": TASK_ID}, 4),
        root,
        reply,
    ]
    adapted = issue_comment_events(source)
    assert [e["type"] for e in adapted] == ["comment_added", "comment_added"]
    assert source[2]["type"] == "issue_comment_added"

    comments = issue_comments(source)
    assert len(comments) == 1
    assert comments[0]["author"] == actor
    assert comments[0]["origin"] == {"user": "atin", "machine": "Atlas"}
    assert comments[0]["replies"][0]["parent_id"] == root["id"]
    assert comments[0]["replies"][0]["origin"] == {"user": "atin", "machine": "Hyperion"}
    assert actor_with_origin("agent:qa", comments[0]["origin"]) == "agent:qa · atin@Atlas"


def test_format_comment_lines_shows_thread_indentation_and_origin() -> None:
    comments = [
        {
            "id": "ev_root",
            "author": {"name": "Argus-3"},
            "created_at": "now",
            "body": "First\nsecond",
            "origin": {"user": "atin", "machine": "Atlas"},
            "replies": [
                {
                    "id": "ev_reply",
                    "author": "human:atin",
                    "created_at": "later",
                    "body": "Reply",
                    "origin": None,
                    "replies": [],
                }
            ],
        }
    ]
    assert format_comment_lines(comments) == [
        "  [ev_root] Argus-3 · atin@Atlas (now)",
        "    First",
        "    second",
        "",
        "    [ev_reply] human:atin (later)",
        "      Reply",
    ]


def test_issue_view_summary_and_promote_use_title_description_and_comment_count() -> None:
    snap = apply_issue_event(
        None,
        event(
            "issue_filed",
            {
                "seq": 7,
                "short_id": "LAT-I7",
                "title": "Footer overlap",
                "description": "At 400px.",
            },
            1,
        ),
    )
    snap = apply_issue_event(snap, event("issue_comment_added", {"body": "Reproduced"}, 2))
    view = issue_view(
        snap, {TASK_ID: TaskInfo("in_progress")}, {"user": "atin", "machine": "Atlas"}
    )
    assert view["title"] == "Footer overlap"
    assert view["description"] == "At 400px."
    assert view["comment_count"] == 1
    assert view["filed_origin"] == {"user": "atin", "machine": "Atlas"}
    assert "text" not in view
    assert linked_issue_summary(view) == {
        "id": ISSUE_ID,
        "short_id": "LAT-I7",
        "state": "open",
        "title": "Footer overlap",
    }
    description = promote_description([snap])
    assert "- LAT-I7 (filed by agent:qa on 2026-10-02): Footer overlap" in description
    assert "  At 400px." in description
    assert "  Comments: 1 (lattice issue show LAT-I7)" in description


def test_description_normalization_and_edit_title_validation() -> None:
    assert normalize_issue_description("  detail  \n") == "  detail"
    assert normalize_issue_description(" \n\t") == ""
    assert check_edit_title("  corrected  ") == "corrected"
    with pytest.raises(ValueError, match="single line"):
        check_edit_title("one\ntwo")
    with pytest.raises(ValueError, match="limit is 120"):
        check_edit_title("x" * 121)


def test_legacy_snapshot_serialization_does_not_gain_new_fields() -> None:
    filed_event = {
        "schema_version": 1,
        "id": "ev_00000000000000000000000001",
        "ts": "2026-10-02T10:00:01Z",
        "type": "issue_filed",
        "issue_id": ISSUE_ID,
        "actor": "agent:qa",
        "data": {"seq": 7, "short_id": "LAT-I7", "text": "Old issue"},
    }
    snap = replay_issue([filed_event])
    expected = {
        "schema_version": 1,
        "id": ISSUE_ID,
        "short_id": "LAT-I7",
        "seq": 7,
        "text": "Old issue",
        "filed_by": "agent:qa",
        "filed_at": "2026-10-02T10:00:01Z",
        "links": [],
        "closure": None,
        "updated_at": "2026-10-02T10:00:01Z",
        "last_event_id": filed_event["id"],
    }
    assert serialize_issue_snapshot(snap) == json.dumps(expected, sort_keys=True, indent=2) + "\n"
