"""Normalization is limited to BUILDPLAN H-0's list: nothing else is rewritten."""

from __future__ import annotations

import json

from tests.parity.record import FROZEN_NOW, Normalizer, _frozen_clock, is_event_log

EVENT = {
    "actor": "agent:a",
    "data": {"type": "user", "ts": "note", "origin": "user-data"},
    "id": "ev_01JCCCCCCCCCCCCCCCCCCCCCCC",
    "origin": {"host": "hyperion", "worktree": "/w"},
    "task_id": "task_01JAAAAAAAAAAAAAAAAAAAAAAA",
    "ts": "2026-06-01T12:00:00Z",
    "type": "x_payload",
}


def _norm() -> Normalizer:
    return Normalizer(["/tmp/board"])


def test_event_log_strips_only_the_top_level_origin() -> None:
    line = json.dumps(EVENT) + "\n"
    (record,) = _norm().file("events/x.jsonl", line.encode(), session_file=False)["jsonl"]
    assert "origin" not in record
    assert record["data"] == {"origin": "user-data", "ts": "note", "type": "user"}


def test_only_event_log_paths_count_as_event_logs() -> None:
    for rel in (
        "events/task_x.jsonl",
        "events/_lifecycle.jsonl",
        "events/res_x.jsonl",
        "archive/events/task_x.jsonl",
    ):
        assert is_event_log(rel), rel
    for rel in (
        "artifacts/payload/art_x.jsonl",
        "notes/trace.jsonl",
        "orchestration/log.jsonl",
        "archive/artifacts/x.jsonl",
        "events/task_x.json",
    ):
        assert not is_event_log(rel), rel


def test_artifact_jsonl_payload_keeps_origin() -> None:
    line = json.dumps({"type": "user", "ts": "note", "origin": "keep"}) + "\n"
    out = _norm().file("artifacts/payload/art_x.jsonl", line.encode(), session_file=False)
    assert out["jsonl"] == [{"origin": "keep", "ts": "note", "type": "user"}]


def test_session_file_strips_top_level_origin_only() -> None:
    session = {"name": "Argus-1", "origin": {"host": "h"}, "meta": {"origin": "kept"}}
    out = _norm().file("sessions/Argus-1.json", json.dumps(session).encode(), session_file=True)
    assert out["json"] == {"meta": {"origin": "kept"}, "name": "Argus-1"}


def test_non_session_json_file_keeps_origin() -> None:
    doc = {"origin": "a config value"}
    out = _norm().file("config.json", json.dumps(doc).encode(), session_file=False)
    assert out["json"] == doc


def test_event_command_output_strips_the_printed_event_origin() -> None:
    out = _norm().output(json.dumps({"ok": True, "data": EVENT}), command="event")
    data = out["json"]["data"]
    assert "origin" not in data
    assert data["data"]["origin"] == "user-data"


def test_event_list_output_strips_each_event() -> None:
    out = _norm().output(json.dumps({"ok": True, "data": [EVENT, EVENT]}), command="archive")
    assert all(
        "origin" not in e and e["data"]["origin"] == "user-data" for e in out["json"]["data"]
    )


def test_other_command_output_keeps_event_shaped_user_data() -> None:
    payload = {"ok": True, "data": {"type": "t", "ts": "x", "origin": "keep me"}}
    out = _norm().output(json.dumps(payload), command="comment")
    assert out["json"]["data"]["origin"] == "keep me"


def test_hook_stdin_strips_top_level_origin_only() -> None:
    line = _norm().sentinel_line("STDIN " + json.dumps(EVENT))
    assert "origin" not in line["stdin"]
    assert line["stdin"]["data"]["origin"] == "user-data"


def test_durations_are_not_normalized() -> None:
    text = "Held by agent:a since 3s ago, expires 10m"
    assert _norm().text(text) == text


def test_ids_timestamps_and_root_are_normalized() -> None:
    norm = _norm()
    text = "/tmp/board task_01JAAAAAAAAAAAAAAAAAAAAAAA 01JAAAAAAAAAAAAAAAAAAAAAAA 2026-06-01T12:00:00Z"
    assert norm.text(text) == "<ROOT> <ID-1> <ID-1> <TS>"


def test_frozen_clock_patches_the_resource_time_sources() -> None:
    from lattice.core import events, resources

    with _frozen_clock():
        assert events.utc_now() == FROZEN_NOW
        assert resources._utc_now() == FROZEN_NOW
