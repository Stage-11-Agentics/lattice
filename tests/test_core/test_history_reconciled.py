"""The reducer side of SPEC §11's reconciliation rule (``core/tasks.py``)."""

from __future__ import annotations

import pytest

from lattice.core.events import create_event
from lattice.core.tasks import (
    FromMismatchError,
    apply_event_to_snapshot,
    is_stale_from,
    reconciled_event_ids,
)

TASK = "task_01AAAAAAAAAAAAAAAAAAAAAAAA"


def _created(**data) -> dict:
    snap = apply_event_to_snapshot(
        None, create_event("task_created", TASK, "human:t", {"title": "T", **data})
    )
    return snap


@pytest.mark.parametrize(
    ("etype", "data", "stale"),
    [
        ("status_changed", {"from": "backlog", "to": "planned"}, False),
        ("status_changed", {"from": "review", "to": "planned"}, True),
        ("status_changed", {"to": "planned"}, False),
        ("assignment_changed", {"from": None, "to": "agent:a"}, False),
        ("assignment_changed", {"from": "agent:x", "to": "agent:a"}, True),
        ("field_updated", {"field": "tags", "from": [], "to": ["a"]}, False),
        ("field_updated", {"field": "tags", "from": ["x"], "to": ["a"]}, True),
        ("field_updated", {"field": "custom_fields.k", "from": None, "to": 1}, False),
        ("field_updated", {"field": "custom_fields.k", "from": 0, "to": 1}, True),
        ("comment_added", {"body": "hi", "from": "x"}, False),
    ],
)
def test_is_stale_from_matches_the_reducer(etype: str, data: dict, stale: bool) -> None:
    snap = _created(status="backlog")
    event = create_event(etype, TASK, "human:t", data)
    assert is_stale_from(snap, event) is stale
    if stale:
        with pytest.raises(FromMismatchError):
            apply_event_to_snapshot(snap, event)
    else:
        apply_event_to_snapshot(snap, event)


def test_accepted_stale_event_applies_as_recorded() -> None:
    snap = _created(status="in_progress")
    event = create_event("status_changed", TASK, "human:t", {"from": "review", "to": "backlog"})
    after = apply_event_to_snapshot(snap, event, accept_stale_from=True)
    assert after["status"] == "backlog"
    assert after["reopened_count"] == 1  # the recorded from decides direction
    assert after["last_event_id"] == event["id"]


def test_reconciliation_changes_only_bookkeeping() -> None:
    snap = _created(status="backlog")
    event = create_event(
        "task_history_reconciled", TASK, "human:t", {"event_ids": ["ev_1"], "reason": "r"}
    )
    after = apply_event_to_snapshot(snap, event)
    changed = {k for k in after if after[k] != snap.get(k)}
    assert changed <= {"last_event_id", "updated_at"}
    assert "last_event_id" in changed


@pytest.mark.parametrize("ids", [None, [], "ev_1", [1], [""]])
def test_reconciled_event_ids_rejects_malformed(ids) -> None:
    event = create_event("task_history_reconciled", TASK, "human:t", {"event_ids": ids})
    with pytest.raises(ValueError, match="non-empty list"):
        reconciled_event_ids(event)
