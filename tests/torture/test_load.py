"""AC-42 (load, without dashboards): on a 1,000-task board, 20 readers (catch-up
plus ``list``), 5 writers, and 5 followers for 60 s; write latency p95 under
500 ms. The workload is fixed here; the rig lives in ``tests/torture/load.py``
(fixture ``load_rig``), shared with H-13b's ``test_with_dashboards``.
"""

from __future__ import annotations

import pytest

from tests.torture.load import LoadRig, p95

pytestmark = [pytest.mark.torture, pytest.mark.envelope, pytest.mark.timeout(2400)]

TASKS = 1000  # the load_rig fixture's board
SECONDS = 60.0
READERS = 20
WRITERS = 5
FOLLOWERS = 5
P95_LIMIT_SECONDS = 0.5


def test_readers_writers(load_rig: LoadRig) -> None:
    assert len(load_rig.tasks) == TASKS
    followers = load_rig.start_followers(FOLLOWERS)
    readers = load_rig.start_readers(READERS)
    latencies = load_rig.run_writers(WRITERS, seconds=SECONDS)
    reads = load_rig.stop_readers(readers)

    assert all(proc.poll() is None for proc in followers), "a follower died under load"
    bad = [r for r in reads if "error" in r or "notice" in r]
    assert not bad, bad[:3]  # every read was a real catch-up, never "busy" or offline
    assert {r["cwd"] for r in reads} == {str(path) for _, path, _ in readers}
    assert min(r["count"] for r in reads) >= TASKS
    ordered = sorted(latencies)
    read_times = [r["t"] - r["t0"] for r in reads]
    per_writer = [len(w.latencies) for w in load_rig.writers]
    print(
        f"load: {TASKS} tasks, {SECONDS:.0f}s, writes per writer {per_writer}, "
        f"p50={ordered[len(ordered) // 2] * 1000:.0f}ms p95={p95(latencies) * 1000:.0f}ms "
        f"max={ordered[-1] * 1000:.0f}ms; {len(reads)} reads p95={p95(read_times) * 1000:.0f}ms"
    )
    assert p95(latencies) < P95_LIMIT_SECONDS
