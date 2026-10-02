"""The issue log's pure logic (LAT-361): replay, derived state, IDs, text."""

from __future__ import annotations

import pytest

from lattice.core.config import default_config, issues_enabled
from lattice.core.events import create_issue_event
from lattice.core.issues import (
    TaskInfo,
    apply_issue_event,
    default_task_title,
    derive_issue_state,
    format_issue_row,
    format_issue_short_id,
    format_linked_issue_line,
    format_task_link_line,
    id_width,
    issue_view,
    parse_issue_ref,
    promote_description,
    replay_issue,
)

ISS = "iss_01K00000000000000000000000"
ISS2 = "iss_01K00000000000000000000001"
T1 = "task_01K00000000000000000000001"
T2 = "task_01K00000000000000000000002"


def ev(etype: str, data: dict, actor: str | dict = "agent:qa", n: int = 0) -> dict:
    return create_issue_event(
        etype, ISS, actor, data, event_id=f"ev_{n:026d}", ts=f"2026-09-29T09:00:{n:02d}Z"
    )


def filed(**extra: object) -> dict:
    data = {"seq": 3, "short_id": "LAT-I3", "text": "Footer overlaps\nat 400px", **extra}
    return ev("issue_filed", data)


# ---------------------------------------------------------------------------
# IDs: the one table the format lives in (A12)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("code", "seq", "short"),
    [("LAT", 3, "LAT-I3"), (None, 7, "I7"), ("", 1, "I1"), ("AB12", 40, "AB12-I40")],
)
def test_format_issue_short_id(code: str | None, seq: int, short: str) -> None:
    assert format_issue_short_id(code, seq) == short


@pytest.mark.parametrize(
    ("raw", "parsed"),
    [
        ("LAT-I3", ("seq", "LAT", 3)),
        ("lat-i3", ("seq", "LAT", 3)),
        ("I3", ("seq", None, 3)),
        ("i3", ("seq", None, 3)),
        ("AB-CD-I12", ("seq", "AB-CD", 12)),
        (ISS, ("ulid", ISS)),
        (ISS.lower(), ("ulid", ISS)),
        ("LAT-3", None),  # a task short ID, never an issue
        ("LAT-I", None),
        ("I0", None),
        ("iss_nope", None),
        ("task_01K00000000000000000000001", None),
        ("", None),
    ],
)
def test_parse_issue_ref(raw: str, parsed: tuple | None) -> None:
    assert parse_issue_ref(raw) == parsed


def test_issue_ids_are_never_task_short_ids() -> None:
    from lattice.core.ids import extract_short_ids, is_short_id

    assert not is_short_id("LAT-I3")
    assert extract_short_ids("see LAT-I3 and LAT-4") == ["LAT-4"]


# ---------------------------------------------------------------------------
# Replay
# ---------------------------------------------------------------------------


def test_filed_snapshot() -> None:
    snap = apply_issue_event(None, filed(confidence="definite", evidence=["a.png"]))
    assert snap["id"] == ISS
    assert snap["short_id"] == "LAT-I3"
    assert snap["seq"] == 3
    assert snap["confidence"] == "definite"
    assert snap["evidence"] == ["a.png"]
    assert "source" not in snap
    assert snap["links"] == [] and snap["closure"] is None
    assert snap["filed_by"] == "agent:qa"
    assert snap["last_event_id"] == "ev_" + "0" * 26
    assert "state" not in snap


def test_replay_every_event_type() -> None:
    events = [
        filed(),
        ev("issue_linked", {"task_id": T1}, n=1),
        ev("issue_linked", {"task_id": T1}, n=2),  # a repeat changes nothing
        ev("issue_linked", {"task_id": T2}, n=3),
        ev("issue_unlinked", {"task_id": T1}, n=4),
    ]
    snap = replay_issue(events)
    assert [link["task_id"] for link in snap["links"]] == [T2]
    assert snap["links"][0]["linked_at"] == "2026-09-29T09:00:03Z"

    snap = apply_issue_event(snap, ev("issue_dismissed", {"reason": "nope"}, n=5))
    assert snap["closure"] == {
        "kind": "dismissed",
        "reason": "nope",
        "at": "2026-09-29T09:00:05Z",
        "by": "agent:qa",
    }
    snap = apply_issue_event(snap, ev("issue_reopened", {}, n=6))
    assert snap["closure"] is None
    snap = apply_issue_event(snap, ev("issue_marked_duplicate", {"duplicate_of": ISS2}, n=7))
    assert snap["closure"]["kind"] == "duplicate"
    assert snap["closure"]["duplicate_of"] == ISS2
    assert snap["updated_at"] == "2026-09-29T09:00:07Z"


def test_unknown_issue_event_only_advances_last_event_id() -> None:
    snap = apply_issue_event(None, filed())
    later = apply_issue_event(snap, ev("issue_triaged_by_robot", {"x": 1}, n=9))
    assert later["last_event_id"] == f"ev_{9:026d}"
    assert {k: v for k, v in later.items() if k != "last_event_id"} == {
        k: v for k, v in snap.items() if k != "last_event_id"
    }


def test_apply_does_not_modify_its_input() -> None:
    snap = apply_issue_event(None, filed())
    apply_issue_event(snap, ev("issue_linked", {"task_id": T1}, n=1))
    assert snap["links"] == []


def test_events_before_filing_are_refused() -> None:
    with pytest.raises(ValueError):
        apply_issue_event(None, ev("issue_linked", {"task_id": T1}))
    with pytest.raises(ValueError):
        apply_issue_event(apply_issue_event(None, filed()), filed())


# ---------------------------------------------------------------------------
# Derived state
# ---------------------------------------------------------------------------


def _linked(*task_ids: str) -> dict:
    snap = apply_issue_event(None, filed())
    for i, tid in enumerate(task_ids, 1):
        snap = apply_issue_event(snap, ev("issue_linked", {"task_id": tid}, n=i))
    return snap


@pytest.mark.parametrize(
    ("links", "info", "state"),
    [
        ((), {}, "open"),
        ((T1,), {T1: TaskInfo("backlog")}, "linked"),
        ((T1,), {T1: TaskInfo("done")}, "resolved"),
        ((T1,), {T1: TaskInfo("done", archived=True)}, "resolved"),
        ((T1, T2), {T1: TaskInfo("done"), T2: TaskInfo("in_progress")}, "linked"),
        ((T1, T2), {T1: TaskInfo("done"), T2: TaskInfo("cancelled")}, "resolved"),
        ((T1,), {T1: TaskInfo("cancelled")}, "open"),
        ((T1,), {T1: TaskInfo("in_progress", erased=True)}, "open"),
        ((T1,), {T1: None}, "open"),
        ((T1,), {}, "open"),
    ],
)
def test_derive_issue_state(links: tuple, info: dict, state: str) -> None:
    assert derive_issue_state(_linked(*links), info) == state


def test_closure_wins_over_live_links() -> None:
    snap = _linked(T1)
    snap = apply_issue_event(snap, ev("issue_dismissed", {"reason": "r"}, n=5))
    assert derive_issue_state(snap, {T1: TaskInfo("in_progress")}) == "dismissed"
    snap = apply_issue_event(snap, ev("issue_marked_duplicate", {"duplicate_of": ISS2}, n=6))
    assert derive_issue_state(snap, {T1: TaskInfo("done")}) == "duplicate"


def test_view_and_row() -> None:
    snap = _linked(T1, T2)
    view = issue_view(
        snap,
        {T1: TaskInfo("in_progress", short_id="LAT-9", title="Fix"), T2: None},
    )
    assert view["state"] == "linked"
    assert view["tasks"][0]["short_id"] == "LAT-9"
    assert view["tasks"][1]["status"] is None
    assert format_issue_row(view) == (
        f"LAT-I3  linked     -         Footer overlaps -> LAT-9 (in_progress), {T2} (missing)"
    )


def test_rows_line_up() -> None:
    """ID pads to the widest ID given, state to `duplicate`, confidence to `definite`."""
    short = {"short_id": "I9", "state": "open", "confidence": None, "title": "a", "tasks": []}
    long = {"short_id": "I10", "state": "duplicate", "confidence": "definite", "title": "b"}
    width = id_width([short, long])
    assert width == 3
    rows = [format_issue_row(v, width) for v in (short, long)]
    assert rows == ["I9   open       -         a", "I10  duplicate  definite  b"]
    assert rows[0].index("a") == rows[1].index("b")
    lines = [
        format_linked_issue_line({"short_id": "I9", "state": "open", "title": "a"}, 3),
        format_linked_issue_line({"short_id": "I10", "state": "resolved", "title": "b"}, 3),
    ]
    assert lines == ["I9   open       a", "I10  resolved   b"]
    entries = [
        {"short_id": "LAT-9", "status": "done", "title": "T"},
        {"short_id": "LAT-10", "status": "in_progress", "erased": True},
    ]
    assert [format_task_link_line(e, id_width(entries), 6) for e in entries] == [
        'LAT-9   done    "T"',
        "LAT-10  erased",
    ]


# ---------------------------------------------------------------------------
# Config and promote text
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("config", "on"),
    [
        (dict(default_config()), False),
        ({"issues": {"enabled": True}}, True),
        ({"issues": {"enabled": False}}, False),
        ({"issues": {"enabled": "yes"}}, False),
        ({"issues": True}, False),
    ],
)
def test_issues_enabled(config: dict, on: bool) -> None:
    assert issues_enabled(config) is on


def test_default_task_title_is_the_first_line_capped() -> None:
    snap = apply_issue_event(None, filed())
    assert default_task_title(snap) == "Footer overlaps"
    long = apply_issue_event(None, ev("issue_filed", {"seq": 1, "text": "x" * 300}))
    assert len(default_task_title(long)) == 120


def test_promote_description_names_every_issue() -> None:
    one = apply_issue_event(None, filed(confidence="definite", evidence=["a.png", "b.log"]))
    two_event = create_issue_event(
        "issue_filed",
        ISS2,
        {"name": "Argus-3", "base_name": "Argus", "session": "sess_x"},
        {"seq": 4, "short_id": "LAT-I4", "text": "Signup button dead"},
        ts="2026-09-30T10:00:00Z",
    )
    two = apply_issue_event(None, two_event)
    text = promote_description([one, two])
    assert text.startswith("Made from issue(s):\n")
    assert (
        "- LAT-I3 (definite, filed by agent:qa on 2026-09-29): Footer overlaps\n  at 400px" in text
    )
    assert "  Evidence: a.png, b.log" in text
    assert "- LAT-I4 (filed by Argus-3 on 2026-09-30): Signup button dead" in text


# ---------------------------------------------------------------------------
# Media (LAT-366)
# ---------------------------------------------------------------------------

MED1 = "med_01K00000000000000000000001"
MED2 = "med_01K00000000000000000000002"


def added(media_id: str, n: int, at: int, kind: str = "photo", **extra: object) -> dict:
    data = {
        "media_id": media_id,
        "n": n,
        "kind": kind,
        "content_type": "image/png" if kind == "photo" else "video/mp4",
        "original_name": f"shot{n}.png",
        "size_bytes": 10 * n,
        "sha256": f"{n:064x}",
        **extra,
    }
    return ev("issue_media_added", data, n=at)


def test_media_replay_added_and_removed() -> None:
    snap = replay_issue(
        [
            filed(),
            added(MED1, 1, 2, width=3, height=2),
            added(MED2, 2, 3, kind="video", duration_ms=1400),
            added(MED1, 1, 4),  # merged logs: a duplicate media_id is ignored
            ev("issue_media_removed", {"media_id": MED2, "n": 2, "reason": "key"}, "human:a", 5),
            ev("issue_media_removed", {"media_id": MED2, "reason": "again"}, n=6),
            ev("issue_media_removed", {"media_id": "med_unknown", "reason": "x"}, n=7),
        ]
    )
    assert snap is not None
    first, second = snap["media"]
    assert first == {
        "id": MED1,
        "n": 1,
        "kind": "photo",
        "content_type": "image/png",
        "original_name": "shot1.png",
        "size_bytes": 10,
        "sha256": f"{1:064x}",
        "width": 3,
        "height": 2,
        "added_at": "2026-09-29T09:00:02Z",
        "added_by": "agent:qa",
    }
    assert "original_name" not in second  # m3: a removed item's name leaves the views
    assert second["removed"] == {"at": "2026-09-29T09:00:05Z", "by": "human:a", "reason": "key"}
    assert snap["last_event_id"] == f"ev_{7:026d}"


def test_no_media_key_without_media_events() -> None:
    snap = replay_issue([filed(), ev("issue_media_removed", {"media_id": MED1}, n=2)])
    assert snap is not None and "media" not in snap
    assert issue_view(snap, {})["media"] == []


def test_media_log_replays_on_a_build_without_the_media_types(monkeypatch) -> None:
    """AC-10: LAT-361's replay ignores the unknown types (only last_event_id moves)."""
    from lattice.core import issues

    events = [filed(), added(MED1, 1, 2), ev("issue_linked", {"task_id": T1}, n=3)]
    old = {k: v for k, v in issues._HANDLERS.items() if not k.startswith("issue_media")}
    monkeypatch.setattr(issues, "_HANDLERS", old)
    snap = replay_issue([*events, added(MED2, 2, 4)])
    assert snap is not None
    assert "media" not in snap
    assert snap["links"][0]["task_id"] == T1
    assert snap["last_event_id"] == f"ev_{4:026d}"


def test_issue_view_and_promote_description_carry_media() -> None:
    snap = replay_issue([filed(), added(MED1, 1, 2), added(MED2, 2, 3, kind="video")])
    assert snap is not None
    assert [m["id"] for m in issue_view(snap, {})["media"]] == [MED1, MED2]
    text = promote_description([snap])
    assert "  Media: 1 photo, 1 video (lattice issue media LAT-I3)" in text
    assert "Media:" not in promote_description([replay_issue([filed()])])
