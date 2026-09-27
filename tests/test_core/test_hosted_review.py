"""Review gate state read from a task's events (SPEC §3.4): pure logic."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from lattice.core.hosted_review import (
    FAILED,
    FINISHED,
    LOCAL,
    RUNNING,
    gate_state,
    is_own_spawn,
)

NOW = datetime(2026, 9, 27, 12, 0, tzinfo=timezone.utc)


def _ts(seconds_ago: int) -> str:
    return (NOW - timedelta(seconds=seconds_ago)).strftime("%Y-%m-%dT%H:%M:%SZ")


def spawn(review_type: str, trigger: str, *, host: str = "a", ago: int = 10) -> dict:
    return {
        "id": f"ev_spawn_{trigger}",
        "type": "auto_review_spawned",
        "data": {
            "review_type": review_type,
            "spawned_at": _ts(ago),
            "trigger_status_event_id": trigger,
        },
        "origin": {"reported": {"host": host}},
    }


def status(event_id: str) -> dict:
    return {"id": event_id, "type": "status_changed", "data": {}}


def artifact(role: str) -> dict:
    return {"id": "ev_art", "type": "artifact_attached", "data": {"role": role}}


def state(events: list[dict], review_type: str = "code-review", **kw) -> object:
    kw.setdefault("this_host", "b")
    kw.setdefault("has_local_record", False)
    kw.setdefault("timeout_seconds", 600)
    return gate_state(events, review_type, now=NOW, **kw)


def test_no_spawn_no_state() -> None:
    assert state([status("ev_1")]) is None
    assert state([spawn("plan-review", "ev_1")]) is None


def test_running_then_failed_after_the_timeout() -> None:
    running = state([status("ev_1"), spawn("code-review", "ev_1", ago=599)])
    assert running.state == RUNNING
    assert running.message() == f"running on a since {_ts(599)}"
    failed = state([status("ev_1"), spawn("code-review", "ev_1", ago=600)])
    assert failed.state == FAILED
    assert failed.message() == "spawned on a, no artifact after 600 s; treat as failed"


def test_only_an_artifact_of_the_gate_role_after_the_spawn_finishes_it() -> None:
    events = [artifact("review"), spawn("code-review", "ev_1"), artifact("plan-review")]
    assert state(events).state == RUNNING
    assert state([*events, artifact("review")]).state == FINISHED
    plan = [spawn("plan-review", "ev_1"), artifact("plan-review")]
    assert state(plan, "plan-review").state == FINISHED


def test_this_machines_own_spawn_with_a_record_is_local() -> None:
    events = [spawn("code-review", "ev_1", host="b")]
    assert state(events, has_local_record=True).state == LOCAL
    assert state(events, has_local_record=False).state == RUNNING
    assert state(events, this_host="c", has_local_record=True).state == RUNNING


def test_the_latest_spawn_of_the_gate_counts() -> None:
    events = [
        spawn("code-review", "ev_1", host="a", ago=900),
        spawn("code-review", "ev_2", host="c", ago=5),
        spawn("plan-review", "ev_3", host="d", ago=1),
    ]
    gate = state(events)
    assert (gate.host, gate.trigger, gate.state) == ("c", "ev_2", RUNNING)


def test_own_spawn_in_either_order() -> None:
    events = [status("ev_1"), spawn("code-review", "ev_1"), status("ev_2")]
    gate = state(events)
    assert is_own_spawn(events, gate, "ev_1")  # the parent recorded it first
    assert is_own_spawn(events, gate, "ev_2")  # the child started first
    assert not is_own_spawn(events, gate, None)
    assert not is_own_spawn(events, gate, "ev_0")
    commented = [*events, {"id": "ev_c", "type": "comment_added", "data": {}}]
    assert not is_own_spawn(commented, state(commented), "ev_c")
