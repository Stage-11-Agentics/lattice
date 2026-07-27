"""Pure acceptance-criterion contract tests."""

from __future__ import annotations

import pytest

from lattice.core.acceptance_criteria import (
    allocate_criterion_id,
    canonical_criterion_number,
    normalize_criterion_ids,
    normalize_outcome,
    validate_criterion_id,
)
from lattice.core.events import create_event
from lattice.core.tasks import apply_event_to_snapshot, compact_snapshot


def _created_snapshot() -> dict:
    event = create_event(
        "task_created",
        "task_01AAAAAAAAAAAAAAAAAAAAAAAAAA",
        "human:test",
        {"title": "Criteria", "status": "backlog", "priority": "medium", "type": "task"},
        event_id="ev_01AAAAAAAAAAAAAAAAAAAAAAAAAA",
        ts="2026-07-27T12:00:00Z",
    )
    return apply_event_to_snapshot(None, event)


@pytest.mark.parametrize(
    "criterion_id",
    ["AC-1", "login.redirect", "a", "A_B-C.9", "x" * 64],
)
def test_valid_ids(criterion_id: str) -> None:
    assert validate_criterion_id(criterion_id) == criterion_id


@pytest.mark.parametrize("criterion_id", ["", "-bad", "has space", "x" * 65, "ø"])
def test_invalid_ids(criterion_id: str) -> None:
    with pytest.raises(ValueError):
        validate_criterion_id(criterion_id)


def test_canonical_parser_and_max_plus_one_allocation() -> None:
    criteria = [
        {"id": "AC-1"},
        {"id": "AC-9", "retired": True},
        {"id": "AC-0"},
        {"id": "AC-01"},
        {"id": "ac-40"},
        {"id": "custom"},
    ]
    assert canonical_criterion_number("AC-9") == 9
    assert canonical_criterion_number("AC-01") is None
    assert allocate_criterion_id(criteria) == "AC-10"


def test_allocation_exhaustion() -> None:
    with pytest.raises(ValueError, match="exhausted"):
        allocate_criterion_id([{"id": "AC-" + "9" * 61}])


def test_outcome_and_sparse_link_normalization() -> None:
    assert normalize_outcome("  first line\nsecond  ") == "first line\nsecond"
    assert normalize_criterion_ids(["AC-1", "AC-1", "custom"]) == ["AC-1", "custom"]
    with pytest.raises(ValueError):
        normalize_outcome(" \n ")


def test_add_edit_retire_materializes_history_and_counts() -> None:
    snapshot = _created_snapshot()
    added = create_event(
        "acceptance_criterion_added",
        snapshot["id"],
        "agent:add",
        {"criterion_id": "AC-1", "outcome": "Playback resumes.", "revision": 1},
        event_id="ev_01BBBBBBBBBBBBBBBBBBBBBBBBBB",
        ts="2026-07-27T12:01:00Z",
    )
    snapshot = apply_event_to_snapshot(snapshot, added)
    edited = create_event(
        "acceptance_criterion_edited",
        snapshot["id"],
        "agent:edit",
        {
            "criterion_id": "AC-1",
            "from_outcome": "Playback resumes.",
            "outcome": "Playback resumes without restarting the app.",
            "revision": 2,
        },
        event_id="ev_01CCCCCCCCCCCCCCCCCCCCCCCCCC",
        ts="2026-07-27T12:02:00Z",
    )
    snapshot = apply_event_to_snapshot(snapshot, edited)
    retired = create_event(
        "acceptance_criterion_retired",
        snapshot["id"],
        "human:test",
        {"criterion_id": "AC-1", "revision": 2},
        event_id="ev_01DDDDDDDDDDDDDDDDDDDDDDDDDD",
        ts="2026-07-27T12:03:00Z",
    )
    snapshot = apply_event_to_snapshot(snapshot, retired)

    criterion = snapshot["acceptance_criteria"][0]
    assert criterion["revision"] == 2
    assert criterion["retired"] is True
    assert criterion["retired_by"] == "human:test"
    assert [revision["revision"] for revision in criterion["revisions"]] == [1, 2]
    compact = compact_snapshot(snapshot)
    assert compact["acceptance_criteria_count"] == 0
    assert compact["retired_acceptance_criteria_count"] == 1


@pytest.mark.parametrize(
    "event_type,data",
    [
        (
            "acceptance_criterion_added",
            {"criterion_id": "AC-1", "outcome": "Outcome.", "revision": 2},
        ),
        (
            "acceptance_criterion_edited",
            {
                "criterion_id": "AC-404",
                "from_outcome": "x",
                "outcome": "y",
                "revision": 2,
            },
        ),
        (
            "acceptance_criterion_retired",
            {"criterion_id": "AC-404", "revision": 1},
        ),
    ],
)
def test_malformed_sequences_are_rejected(event_type: str, data: dict) -> None:
    with pytest.raises(ValueError):
        apply_event_to_snapshot(
            _created_snapshot(),
            create_event(event_type, _created_snapshot()["id"], "human:test", data),
        )
