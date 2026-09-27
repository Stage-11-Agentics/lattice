"""Review status read from the board, for hosted checkouts (SPEC §3.4).

``review_state/`` is machine-local, so on a hosted checkout a review started on
another machine leaves no record here. What every machine does share is the
task's event log: the ``auto_review_spawned`` event the spawning client records,
and the ``artifact_attached`` event a finished review leaves. This module
derives each review gate's state from those events. Pure logic: callers pass
the events, the clock, and what this machine knows.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone

#: The artifact role a finished review of each gate attaches.
GATE_ROLES = {"code-review": "review", "plan-review": "plan-review"}

FINISHED = "finished"  # an artifact of the gate's role was attached after the spawn
LOCAL = "local"  # spawned on this machine, which holds a review_state record
RUNNING = "running"  # younger than the review timeout, no artifact yet
FAILED = "failed"  # older than the review timeout, no artifact


@dataclass(frozen=True)
class GateState:
    review_type: str
    state: str
    host: str | None
    spawned_at: str | None
    trigger: str | None
    timeout_seconds: int
    #: Position of the spawn event in the task's log.
    index: int

    @property
    def in_flight(self) -> bool:
        return self.state in (LOCAL, RUNNING)

    def message(self) -> str:
        host = self.host or "an unknown host"
        if self.state == FAILED:
            return (
                f"spawned on {host}, no artifact after {self.timeout_seconds} s; treat as failed"
            )
        return f"running on {host} since {self.spawned_at}"


def _parse(ts: object) -> datetime | None:
    if not isinstance(ts, str):
        return None
    try:
        parsed = datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def latest_spawn(events: list[dict], review_type: str) -> tuple[int, dict] | None:
    """The task's latest ``auto_review_spawned`` event for *review_type*, with its index."""
    for index in range(len(events) - 1, -1, -1):
        event = events[index]
        data = event.get("data") or {}
        if event.get("type") == "auto_review_spawned" and data.get("review_type") == review_type:
            return index, event
    return None


def gate_state(
    events: list[dict],
    review_type: str,
    *,
    this_host: str | None,
    has_local_record: bool,
    timeout_seconds: int,
    now: datetime,
) -> GateState | None:
    """The state of one review gate, or ``None`` when no review of it was ever spawned."""
    found = latest_spawn(events, review_type)
    if found is None:
        return None
    index, spawn = found
    data = spawn.get("data") or {}
    role = GATE_ROLES[review_type]
    host = ((spawn.get("origin") or {}).get("reported") or {}).get("host")
    spawned_at = data.get("spawned_at") or spawn.get("ts")
    finished = any(
        e.get("type") == "artifact_attached" and (e.get("data") or {}).get("role") == role
        for e in events[index + 1 :]
    )
    if finished:
        state = FINISHED
    elif host is not None and host == this_host and has_local_record:
        state = LOCAL
    else:
        started = _parse(spawned_at)
        young = started is not None and (now - started).total_seconds() < timeout_seconds
        state = RUNNING if young else FAILED
    return GateState(
        review_type=review_type,
        state=state,
        host=host,
        spawned_at=spawned_at,
        trigger=data.get("trigger_status_event_id"),
        timeout_seconds=timeout_seconds,
        index=index,
    )


def is_own_spawn(events: list[dict], gate: GateState, triggered_by: str | None) -> bool:
    """True when a review started with ``--triggered-by`` *triggered_by* is the
    auto-fired child of *gate*'s spawn, in either order of the child starting and
    the parent recording the spawn: the spawn names the same trigger, or the
    trigger is a status change recorded after the spawn (whose own spawn record the
    parent has not written yet)."""
    if triggered_by is None:
        return False
    if gate.trigger == triggered_by:
        return True
    return any(
        e.get("id") == triggered_by and e.get("type") == "status_changed"
        for e in events[gate.index + 1 :]
    )
