"""AC-42 (load, without dashboards): on a 1,000-task board, 20 readers (catch-up
plus ``list``), 5 writers, and 5 followers for 60 s; write latency p95 under
500 ms. The clients' checkouts and caches live on a filesystem separate from the
server root's (EVALUATION AC-42); client read latency is reported, not bounded.
The workload is fixed here; the rig lives in ``tests/torture/load.py``, shared
with H-13b's ``test_with_dashboards``.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from tests.torture.load import (
    LoadRig,
    assert_separate_filesystems,
    p95,
    read_latencies,
    separate_client_filesystem,
)

pytestmark = pytest.mark.torture

TASKS = 1000
SECONDS = 60.0
READERS = 20
WRITERS = 5
FOLLOWERS = 5
P95_LIMIT_SECONDS = 0.5


@pytest.mark.envelope
@pytest.mark.timeout(2400)
def test_readers_writers(tmp_path: Path) -> None:
    with LoadRig.running(tmp_path, tasks=TASKS) as rig:
        assert len(rig.tasks) == TASKS
        followers = rig.start_followers(FOLLOWERS)
        readers = rig.start_readers(READERS)
        latencies = rig.run_writers(WRITERS, seconds=SECONDS)
        reads = rig.stop_readers(readers)
        assert all(proc.poll() is None for proc in followers), "a follower died under load"
        per_writer = [len(w.latencies) for w in rig.writers]
        client_dir = rig.client_dir

    bad = [r for r in reads if "error" in r or "notice" in r]
    assert not bad, bad[:3]  # every read was a real catch-up, never "busy" or offline
    assert {r["cwd"] for r in reads} == {str(path) for _, path, _ in readers}
    assert min(r["count"] for r in reads) >= TASKS
    writes, read_times = sorted(latencies), sorted(read_latencies(reads))
    print(
        f"load: {TASKS} tasks, {SECONDS:.0f}s, clients in {client_dir}; "
        f"writes per writer {per_writer}, p50={writes[len(writes) // 2] * 1000:.0f}ms "
        f"p95={p95(writes) * 1000:.0f}ms max={writes[-1] * 1000:.0f}ms; "
        f"client reads {len(read_times)}, p50={read_times[len(read_times) // 2] * 1000:.0f}ms "
        f"p95={p95(read_times) * 1000:.0f}ms max={read_times[-1] * 1000:.0f}ms (reported, LAT-330)"
    )
    assert p95(latencies) < P95_LIMIT_SECONDS


@pytest.mark.timeout(60)
def test_the_rig_never_measures_clients_on_the_server_filesystem(tmp_path: Path) -> None:
    """The guard behind every load verdict: one filesystem fails, and the separate
    client filesystem is a different device that is gone afterwards."""
    with pytest.raises(AssertionError, match="on one filesystem"):
        assert_separate_filesystems(tmp_path, tmp_path)
    with separate_client_filesystem() as client_dir:
        assert_separate_filesystems(client_dir, tmp_path)
        (client_dir / "cache").mkdir()
    assert not client_dir.exists()
