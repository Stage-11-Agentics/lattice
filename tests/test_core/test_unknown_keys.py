"""G-6: events carrying ``origin`` or any unknown top-level key replay to the
same snapshot, in the permissive reducer and in strict replay."""

from __future__ import annotations

import copy
from pathlib import Path

from lattice.core.events import create_event, serialize_event
from lattice.core.tasks import apply_event_to_snapshot
from lattice.storage.fs import ensure_lattice_dirs
from lattice.storage.operations import resolve_task_authority

TASK = "task_01J9ZABCDEFGHJKMNPQRSTVWXY"


def _history() -> list[dict]:
    return [
        create_event(
            "task_created",
            TASK,
            "human:t",
            {"title": "t", "status": "backlog", "priority": "medium", "type": "task"},
        ),
        create_event("status_changed", TASK, "human:t", {"from": "backlog", "to": "in_planning"}),
        create_event("comment_added", TASK, "human:t", {"body": "hi"}),
    ]


def _with_extra_keys(events: list[dict]) -> list[dict]:
    extended = copy.deepcopy(events)
    for event in extended:
        event["origin"] = {"op": "task.x", "op_id": "op_01J9ZABCDEFGHJKMNPQRSTVWXY"}
        event["x_future_field"] = {"anything": [1, 2]}
    return extended


def _replay(events: list[dict]) -> dict:
    snapshot = None
    for event in events:
        snapshot = apply_event_to_snapshot(snapshot, event)
    assert snapshot is not None
    return snapshot


def test_permissive_replay_ignores_unknown_top_level_keys() -> None:
    events = _history()
    assert _replay(_with_extra_keys(events)) == _replay(events)


def test_strict_replay_ignores_unknown_top_level_keys(tmp_path: Path) -> None:
    events = _history()
    snapshots = []
    for name, history in (("plain", events), ("extended", _with_extra_keys(events))):
        root = tmp_path / name
        ensure_lattice_dirs(root)
        ld = root / ".lattice"
        log = ld / "events" / f"{TASK}.jsonl"
        log.write_text("".join(serialize_event(e) for e in history))
        authority = resolve_task_authority(ld, TASK)
        assert authority is not None
        snapshots.append(authority.snapshot)
    assert snapshots[0] == snapshots[1]
