"""AC-42 (load, without dashboards): on a 1,000-task board, 20 readers (catch-up
plus ``list``), 5 writers, and 5 followers for 60 s; write latency p95 under
500 ms. The rig lives in ``tests/torture/load.py`` (fixture ``load_rig``), shared
with H-13b's ``test_with_dashboards``.
"""

from __future__ import annotations

import pytest

from tests.torture.load import LOAD_SECONDS, LOAD_TASKS, LoadRig, p95

pytestmark = [pytest.mark.torture, pytest.mark.envelope, pytest.mark.timeout(1200)]

READERS = 20
WRITERS = 5
FOLLOWERS = 5
P95_LIMIT_SECONDS = 0.5


def test_readers_writers(load_rig: LoadRig) -> None:
    followers = load_rig.start_followers(FOLLOWERS)
    readers = load_rig.start_readers(READERS)
    latencies = load_rig.run_writers(WRITERS, seconds=LOAD_SECONDS)
    reads = load_rig.stop_readers(readers)

    assert all(proc.poll() is None for proc in followers), "a follower died under load"
    errors = [r for r in reads if "error" in r or "notice" in r]
    assert not errors, errors[:3]  # every read was a real catch-up, never "busy"
    read_times = [r["t"] - r["t0"] for r in reads]
    assert len({r["cwd"] for r in reads}) == READERS, "a reader never completed a read"
    assert min(r["count"] for r in reads) >= LOAD_TASKS
    write_p95 = p95(latencies)
    print(
        f"load: {LOAD_TASKS} tasks, {LOAD_SECONDS:.0f}s, {len(latencies)} writes "
        f"p50={sorted(latencies)[len(latencies) // 2] * 1000:.0f}ms "
        f"p95={write_p95 * 1000:.0f}ms max={max(latencies) * 1000:.0f}ms; "
        f"{len(reads)} reads p95={p95(read_times) * 1000:.0f}ms"
    )
    assert write_p95 < P95_LIMIT_SECONDS
