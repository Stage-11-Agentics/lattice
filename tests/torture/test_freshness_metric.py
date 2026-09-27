"""The rehearsals' freshness measurement catches stale reads (default suite: pure).

``freshness`` is what turns "every write visible within 2 s" into a number, so
a bug that made it always 0 would pass every rehearsal vacuously.
"""

from __future__ import annotations

import math

from tests.torture.rehearsal import freshness

WRITES = [
    {"task": "DEM-1", "last_event_id": "ev_a", "t": 10.0},
    {"task": "DEM-1", "last_event_id": "ev_b", "t": 11.0},
    {"task": "DEM-2", "last_event_id": "ev_c", "t": 11.5},
    {"task": "DEM-2", "last_event_id": "ev_c", "t": 11.6},  # idempotent: nothing new
    {"task": "DEM-2", "t": 11.7},  # a step with no snapshot
]


def _poll(cwd: str, t0: float, t: float, **tasks: str) -> dict:
    return {
        "cwd": cwd,
        "t0": t0,
        "t": t,
        "tasks": {k.replace("_", "-"): v for k, v in tasks.items()},
    }


def test_every_read_after_the_ack_saw_it() -> None:
    polls = [
        _poll("a", 9.0, 9.5),  # before any write: missing is fine
        _poll("a", 10.5, 12.0, DEM_1="ev_a"),
        _poll("a", 11.2, 11.3, DEM_1="ev_b", DEM_2="ev_c"),  # overlaps: sees a later state
        _poll("a", 12.0, 12.1, DEM_1="ev_b", DEM_2="ev_c"),
    ]
    assert freshness(WRITES, polls) == {"a": 0.0}


def test_a_read_that_missed_a_write_counts_from_the_ack() -> None:
    polls = [
        _poll("b", 11.9, 12.0, DEM_1="ev_a", DEM_2="ev_c"),  # 0.9 s after ev_b, still ev_a
        _poll("b", 13.4, 13.5, DEM_1="ev_a", DEM_2="ev_c"),  # 2.4 s after ev_b
        _poll("b", 14.0, 14.1, DEM_1="ev_b", DEM_2="ev_c"),
    ]
    assert round(freshness(WRITES, polls)["b"], 6) == 2.4


def test_a_write_never_seen_is_infinitely_stale() -> None:
    polls = [_poll("c", 20.0, 20.1, DEM_1="ev_b")]
    assert math.isinf(freshness(WRITES, polls)["c"])
