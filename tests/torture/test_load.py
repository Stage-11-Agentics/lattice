"""AC-42 (load): on a 1,000-task board, 20 readers (catch-up plus ``list``),
5 writers, and 5 followers for 60 s; write latency p95 under 500 ms, without
dashboards (``test_readers_writers``, H-15) and with 5 hosted-dashboard viewers
added (``test_with_dashboards``, H-13b). The clients' checkouts and caches live
on a filesystem separate from the server root's (EVALUATION AC-42); client read
latency is reported, not bounded. The workloads are fixed here; the rig lives in
``tests/torture/load.py``.
"""

from __future__ import annotations

import subprocess
import sys
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

import pytest

from tests.torture import harness, load
from tests.torture.load import (
    LoadRig,
    viewer_report,
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
VIEWERS = 5
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


@pytest.mark.envelope
@pytest.mark.timeout(2400)
def test_with_dashboards(tmp_path: Path) -> None:
    """The same load plus 5 hosted-dashboard viewers, each refetching the page's
    panels once per journal entry it receives (no coalescing); every viewer
    refetches exactly as many times as it received entries."""
    with LoadRig.running(tmp_path, tasks=TASKS) as rig:
        followers = rig.start_followers(FOLLOWERS)
        readers = rig.start_readers(READERS)
        viewers = rig.start_viewers(VIEWERS)
        latencies = rig.run_writers(WRITERS, seconds=SECONDS)
        reads = rig.stop_readers(readers)
        views = rig.stop_viewers(viewers)
        assert all(proc.poll() is None for proc in followers), "a follower died under load"

    bad = [r for r in reads if "error" in r or "notice" in r]
    assert not bad, bad[:3]
    assert min(r["count"] for r in reads) >= TASKS
    writes, read_times = sorted(latencies), sorted(read_latencies(reads))
    print(
        f"load with {VIEWERS} viewers: {len(writes)} writes, "
        f"p50={writes[len(writes) // 2] * 1000:.0f}ms p95={p95(writes) * 1000:.0f}ms "
        f"max={writes[-1] * 1000:.0f}ms; client reads p95={p95(read_times) * 1000:.0f}ms "
        f"(reported); viewers (received, refetched) "
        f"{[(v['received'], v['refetched']) for v in views]}"
    )
    for view in views:
        assert view["received"] > 0 and view["refetched"] == view["received"], views
    assert p95(latencies) < P95_LIMIT_SECONDS


@pytest.mark.timeout(60)
def test_a_viewer_that_dies_without_its_report_fails_the_run(tmp_path: Path) -> None:
    """However late a viewer dies (even just before the window closes), the run
    fails: a viewer must exit 0 with its ``done`` report."""
    out = tmp_path / "viewer.json"
    died = subprocess.Popen([sys.executable, "-c", "raise SystemExit(3)"])
    with pytest.raises(AssertionError):
        viewer_report(died, out, timeout=30)
    out.write_text('{"crashed": "viewer stream closed", "received": 4, "refetched": 3}\n')
    crashed = subprocess.Popen([sys.executable, "-c", "raise SystemExit(1)"])
    with pytest.raises(AssertionError):
        viewer_report(crashed, out, timeout=30)
    out.write_text('{"done": true, "received": 4, "refetched": 4}\n')
    finished = subprocess.Popen([sys.executable, "-c", "pass"])
    assert viewer_report(finished, out, timeout=30)["refetched"] == 4


class FakeMount:
    """A mount provider for the policy tests: a plain directory under ``tmp_path``
    (so on the server's filesystem), recording when it is entered and left."""

    def __init__(self, base: Path) -> None:
        self.point = base / "fake-mount"
        self.entered = self.exited = 0

    @contextmanager
    def __call__(self) -> Iterator[Path]:
        self.point.mkdir(exist_ok=True)
        self.entered += 1
        try:
            yield self.point
        finally:
            self.exited += 1


@pytest.fixture()
def no_allocator(monkeypatch: pytest.MonkeyPatch) -> None:
    """The per-PR guard tests never allocate: no ``hdiutil`` or ``diskutil`` (every
    allocation command goes through ``load._allocator_command``) and no
    ``/dev/shm`` (``load.platform_mount``). Other subprocesses, such as the
    ``git init`` of ``project create``'s audit repo, run normally."""

    def refuse(*args: object, **kwargs: object) -> None:
        raise AssertionError(f"the per-PR guard test tried to allocate: {args}")

    monkeypatch.setattr(load, "_allocator_command", refuse)
    monkeypatch.setattr(load, "platform_mount", refuse)


@pytest.mark.timeout(60)
def test_the_device_policy_refuses_one_filesystem(tmp_path: Path, no_allocator: None) -> None:
    with pytest.raises(AssertionError, match="on one filesystem"):
        assert_separate_filesystems(tmp_path, tmp_path)
    devices = {tmp_path / "client": 1, tmp_path / "server": 1}
    with pytest.raises(AssertionError, match="on one filesystem"):
        assert_separate_filesystems(tmp_path / "client", tmp_path / "server", device=devices.get)
    devices[tmp_path / "server"] = 2
    assert_separate_filesystems(tmp_path / "client", tmp_path / "server", device=devices.get)


@pytest.mark.timeout(60)
def test_the_client_directory_is_removed_however_the_block_ends(
    tmp_path: Path, no_allocator: None
) -> None:
    mount = FakeMount(tmp_path)
    with separate_client_filesystem(mount) as client_dir:
        assert client_dir.parent == mount.point
        (client_dir / "cache").mkdir()
    assert not client_dir.exists() and mount.exited == 1
    with pytest.raises(RuntimeError), separate_client_filesystem(mount) as client_dir:
        (client_dir / "cache").mkdir()
        raise RuntimeError("the load run failed")
    assert not client_dir.exists() and mount.exited == 2


@pytest.mark.timeout(60)
def test_the_rig_never_measures_clients_on_the_server_filesystem(
    tmp_path: Path, no_allocator: None
) -> None:
    """With clients on the server root's filesystem, the rig fails before any
    server or client exists, and leaves nothing behind."""
    mount = FakeMount(tmp_path)
    with pytest.raises(AssertionError, match="on one filesystem"):
        LoadRig.build(tmp_path / "work", tasks=1, mount=mount)
    assert harness.SERVERS == [] and harness.CHILDREN == []
    assert mount.entered == mount.exited == 1
    assert list(mount.point.iterdir()) == []
