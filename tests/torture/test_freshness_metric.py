"""The rehearsals' freshness measurement (``rehearsal.freshness``) catches late and
missing visibility, so the 2 s assertions in W and B cannot pass vacuously.
Pure: no server, no processes.
"""

from __future__ import annotations

import math

import pytest

from tests.torture.rehearsal import freshness

pytestmark = pytest.mark.torture

WRITES = [
    {"task": "DEM-1", "last_event_id": "ev_a", "t": 10.0},
    {"task": "DEM-1", "last_event_id": "ev_b", "t": 11.0},
    {"task": "DEM-2", "last_event_id": "ev_c", "t": 11.5},
    {"task": "DEM-2", "last_event_id": "ev_c", "t": 11.6},  # idempotent: nothing new
    {"task": "DEM-2", "t": 11.7},  # a step with no snapshot
]


def _poll(cwd: str, t0: float, t: float, **tasks: str) -> dict:
    shown = {k.replace("_", "-"): v for k, v in tasks.items()}
    return {"cwd": cwd, "t0": t0, "t": t, "tasks": shown}


def test_prompt_observations_are_fresh() -> None:
    polls = [
        _poll("a", 9.0, 9.5),
        _poll("a", 9.9, 10.2, DEM_1="ev_a"),  # 0.2 s after ev_a
        _poll("a", 10.9, 11.3, DEM_1="ev_b", DEM_2="ev_c"),  # read began before the ack
        _poll("a", 11.8, 12.1, DEM_1="ev_b", DEM_2="ev_c"),
        _poll("a", 12.9, 13.0, DEM_1="ev_b", DEM_2="ev_c"),
    ]
    measured = freshness(WRITES, polls)["a"]
    assert round(measured.delay, 6) == 0.3  # ev_b: acked 11.0, first seen at 11.3
    assert round(measured.gap, 6) == 1.1  # 10.2 -> 11.3, bracketed by 9.5 and 12.1
    assert measured.reads == 5


def test_a_late_first_observation_counts_the_whole_wait() -> None:
    """The regression that motivated the metric: reads that are rare, but that show
    every write once they happen, used to measure 0 s."""
    polls = [
        _poll("b", 9.0, 9.5),
        _poll("b", 14.9, 15.0, DEM_1="ev_b", DEM_2="ev_c"),  # the first read after 9.5
    ]
    measured = freshness(WRITES, polls)["b"]
    assert round(measured.delay, 6) == 5.0  # ev_a: acked 10.0, first seen at 15.0
    assert measured.gap > 2.0  # and the cadence alone says the bound was not tested


def test_a_stale_read_after_the_ack_delays_the_first_observation() -> None:
    polls = [
        _poll("c", 11.9, 12.0, DEM_1="ev_a", DEM_2="ev_c"),
        _poll("c", 12.9, 13.0, DEM_1="ev_a", DEM_2="ev_c"),  # still ev_a, 2 s after ev_b
        _poll("c", 13.9, 14.0, DEM_1="ev_b", DEM_2="ev_c"),
    ]
    assert round(freshness(WRITES, polls)["c"].delay, 6) == 3.0


def test_a_write_never_seen_is_infinitely_late() -> None:
    polls = [_poll("d", 20.0, 20.1, DEM_1="ev_b")]
    assert math.isinf(freshness(WRITES, polls)["d"].delay)


def test_no_read_after_the_last_write_is_an_unbounded_gap() -> None:
    polls = [_poll("e", 10.9, 11.0, DEM_1="ev_b")]
    assert math.isinf(freshness(WRITES, polls)["e"].gap)


def test_the_gap_is_bracketed_by_real_reads_not_by_the_first_ack() -> None:
    """Round-2 counterexample: reads at 1.0 and 10.1, one write acked at 10.0 and
    seen at 10.1. The delay is 0.1 s, but the cadence gap is 9.1 s: a read at
    the start of the window would not have run for 9 s."""
    writes = [{"task": "DEM-1", "last_event_id": "ev_a", "t": 10.0}]
    polls = [_poll("f", 0.9, 1.0), _poll("f", 10.0, 10.1, DEM_1="ev_a")]
    measured = freshness(writes, polls)["f"]
    assert round(measured.delay, 6) == 0.1
    assert round(measured.gap, 6) == 9.1


def test_no_read_before_the_first_write_is_an_unbounded_gap() -> None:
    writes = [{"task": "DEM-1", "last_event_id": "ev_a", "t": 10.0}]
    polls = [_poll("g", 10.0, 10.1, DEM_1="ev_a"), _poll("g", 10.2, 10.3, DEM_1="ev_a")]
    assert math.isinf(freshness(writes, polls)["g"].gap)
