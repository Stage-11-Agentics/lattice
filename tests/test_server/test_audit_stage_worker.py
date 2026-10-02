"""The audit stage runs in a worker process, so server threads cannot slow it (LAT-340).

The stage walks every board file under the project's work lock. In-process,
each file's syscalls wait behind busy request threads for the GIL: on Atlas
under AC-42 a 0.15 s stage took 9 s, writers queued behind the lock and
readers' catch-ups timed out. In the worker it keeps its idle speed.
"""

from __future__ import annotations

import os
import shutil
import signal
import subprocess
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import pytest

from lattice.server import admin, audit
from lattice.server import project as server_project
from lattice.server.audit import GitError, Stager
from lattice.server.config import AuditConfig
from lattice.server.testing import make_root, running_server, wait_for
from tests.test_server.audit_helpers import close, direct_project, log_lines
from tests.test_server.conftest import create_task, mint
from tests.test_server.faults import request, run

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="git is not installed")

BOARD_FILES = 3000
#: With two spinning threads, one changed file of 3,000: the worker staged in
#: 0.11 s and in-process staging took 5-9 s (laptop, LAT-340). An absolute
#: bound, so it runs in the perf lane on a quiet host (LAT-363).
CONTENDED_LIMIT_SECONDS = 2.0
QUICK = AuditConfig(debounce_seconds=0.05, max_interval_seconds=1)


def _repo(tmp_path: Path, files: int) -> Path:
    directory = tmp_path / "project"
    tasks = directory / ".lattice" / "tasks"
    tasks.mkdir(parents=True)
    for n in range(files):
        (tasks / f"task_{n:05d}.json").write_text(f'{{"n": {n}}}\n')
    subprocess.run(["git", "init", "-q", str(directory)], check=True, timeout=30)
    return directory


@contextmanager
def _busy_threads(n: int) -> Iterator[None]:
    stop = threading.Event()

    def spin() -> None:
        while not stop.is_set():
            sum(range(1000))

    threads = [threading.Thread(target=spin, daemon=True) for _ in range(n)]
    for thread in threads:
        thread.start()
    try:
        yield
    finally:
        stop.set()
        for thread in threads:
            thread.join()


def test_the_worker_stages_what_the_process_would(tmp_path: Path) -> None:
    directory = _repo(tmp_path, 20)
    stager = Stager(directory)
    try:
        tree = stager.stage()
        assert tree == Stager(directory).stage_here()
        (directory / ".lattice" / "tasks" / "task_00003.json").write_text("changed\n")
        (directory / ".lattice" / "tasks" / "task_00004.json").unlink()
        changed = stager.stage()
        assert changed != tree and changed == Stager(directory).stage_here()
    finally:
        stager.close()


def test_one_worker_serves_every_stage_and_a_dead_one_is_replaced(tmp_path: Path) -> None:
    directory = _repo(tmp_path, 5)
    stager = Stager(directory)
    try:
        tree = stager.stage()
        worker = stager._worker
        assert worker is not None
        assert stager.stage() == tree and stager._worker is worker
        worker.kill()
        worker.wait()
        assert stager.stage() == tree
        assert stager._worker is not None and stager._worker is not worker
    finally:
        stager.close()
    assert stager._worker is None


def test_a_git_failure_is_a_git_error_and_keeps_the_worker(tmp_path: Path) -> None:
    directory = tmp_path / "not-a-repo"
    (directory / ".lattice").mkdir(parents=True)
    (directory / ".lattice" / "config.json").write_text("{}\n")
    stager = Stager(directory)
    try:
        with pytest.raises(GitError):
            stager.stage()
        worker = stager._worker
        assert worker is not None and worker.poll() is None
    finally:
        stager.close()


def test_a_worker_that_dies_mid_stage_is_a_git_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    directory = _repo(tmp_path, 5)
    monkeypatch.setattr(audit, "_STAGE_WORKER", "import sys; sys.stdin.readline()")
    stager = Stager(directory)
    with pytest.raises(GitError, match="without a reply"):
        stager.stage()
    assert stager._worker is None


def test_stage_runs_in_the_worker_process(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    directory = _repo(tmp_path, 5)
    marker = directory / ".stage-worker-pid"
    worker_source = "\n".join(
        (
            "import os",
            "from pathlib import Path",
            "from lattice.server.audit import Stager, _stage_worker",
            "_stage_here = Stager.stage_here",
            "def _stage_here_and_mark_worker(self):",
            "    tree = _stage_here(self)",
            "    Path(os.environ['LATTICE_STAGE_WORKER_MARKER']).write_text(",
            "        str(os.getpid())",
            "    )",
            "    return tree",
            "Stager.stage_here = _stage_here_and_mark_worker",
            "_stage_worker()",
            "",
        )
    )
    monkeypatch.setenv("LATTICE_STAGE_WORKER_MARKER", str(marker))
    monkeypatch.setattr(audit, "_STAGE_WORKER", worker_source)
    stager = Stager(directory)
    try:
        stager.stage()
        assert marker.exists(), "stage_here did not mark the worker process"
        assert stager._worker is not None
        worker_pid = int(marker.read_text())
        assert worker_pid == stager._worker.pid
        assert worker_pid != os.getpid()
    finally:
        stager.close()


@pytest.mark.perf
def test_busy_server_threads_do_not_slow_the_stage_on_a_quiet_host(tmp_path: Path) -> None:
    directory = _repo(tmp_path, BOARD_FILES)
    stager = Stager(directory)
    try:
        stager.stage()  # start the worker and fill its cache
        (directory / ".lattice" / "tasks" / "task_00000.json").write_text("changed\n")
        with _busy_threads(2):
            started = time.monotonic()
            stager.stage()
            elapsed = time.monotonic() - started
    finally:
        stager.close()
    assert elapsed < CONTENDED_LIMIT_SECONDS, elapsed


def test_audit_commit_reports_the_lock_it_held(tmp_path: Path) -> None:
    """``audit_commit`` names its lock wait, its stage (the work-lock hold), and its commit."""
    root = make_root(tmp_path, projects={"alpha": {"code": "ALP"}})
    project, stream = direct_project(root, "alpha", QUICK)
    try:
        run(project, request("task.create", {"title": "one"}))
        wait_for(lambda: any(line["event"] == "audit_commit" for line in log_lines(stream)))
    finally:
        close(project)
    line = next(line for line in log_lines(stream) if line["event"] == "audit_commit")
    assert {"lock_wait_ms", "stage_ms", "commit_ms"} <= line.keys(), line


def test_a_slow_work_lock_hold_is_logged(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root = make_root(tmp_path, projects={"alpha": {"code": "ALP"}})
    project, stream = direct_project(root, "alpha", QUICK)
    monkeypatch.setattr(server_project, "SLOW_WORK_LOCK_SECONDS", 0.05)
    try:
        with project.locked():
            time.sleep(0.1)
    finally:
        close(project)
    slow = [line for line in log_lines(stream) if line["event"] == "work_lock_slow"]
    assert slow and slow[0]["held_ms"] >= 100 and slow[0]["thread"], slow


def test_a_file_changed_after_the_prehash_is_hashed_again(tmp_path: Path) -> None:
    """The prehash runs while writes continue; the stage under the lock still
    records every file's bytes as they are at the stage."""
    directory = _repo(tmp_path, 50)
    tasks = directory / ".lattice" / "tasks"
    stager = Stager(directory)
    try:
        stager.stage()
        for n in range(10):
            (tasks / f"task_{n:05d}.json").write_text(f"first {n}\n")
        stager.prehash()
        (tasks / "task_00003.json").write_text("second 3\n")  # same size as "first 3"
        (tasks / "task_00004.json").unlink()
        (tasks / "task_99999.json").write_text("new\n")
        assert stager.stage() == Stager(directory).stage_here()
    finally:
        stager.close()


def test_close_reaps_the_worker_and_starts_no_other(tmp_path: Path) -> None:
    stager = Stager(_repo(tmp_path, 5))
    stager.stage()
    worker = stager._worker
    assert worker is not None
    stager.close()
    assert worker.returncode == 0 and worker.stdout is not None and worker.stdout.closed
    with pytest.raises(GitError, match="closed"):
        stager.stage()
    assert stager._worker is None


def test_close_kills_a_worker_that_does_not_exit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(audit, "_STAGE_WORKER", "import time; time.sleep(60)")
    stager = Stager(_repo(tmp_path, 1))
    worker = stager._worker = stager._start_worker()
    started = time.monotonic()
    stager.close(timeout=0.2)
    assert worker.returncode == -signal.SIGKILL
    assert time.monotonic() - started < 5


def _record_worker_at_shutdown(
    monkeypatch: pytest.MonkeyPatch, project: Any, seen: list[tuple[str, int | None]]
) -> list[subprocess.Popen]:
    """Capture the stager's worker at the final stage, and record its return code
    (``None`` while it runs) when clean_shutdown is written and the lease released."""
    workers: list[subprocess.Popen] = []
    real_stage = Stager.stage
    real_clean = project.write_clean_shutdown
    real_release, real_unload = project.release, project.unload

    def stage(self: Stager) -> str:
        tree = real_stage(self)
        workers.append(self._worker)
        return tree

    def at(name: str, real: Any) -> Any:
        def wrapped(*args: Any, **kwargs: Any) -> Any:
            seen.append((name, workers[0].poll() if workers else "no worker"))
            return real(*args, **kwargs)

        return wrapped

    monkeypatch.setattr(Stager, "stage", stage)
    monkeypatch.setattr(project, "write_clean_shutdown", at("clean_shutdown", real_clean))
    monkeypatch.setattr(project, "release", at("release_lease", real_release))
    monkeypatch.setattr(project, "unload", at("unload", real_unload))
    return workers


def test_server_shutdown_reaps_the_worker_before_clean_shutdown(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The SIGTERM path (``ProjectRegistry.stop``)."""
    config = {"audit": {"debounce_seconds": 60, "max_interval_seconds": 60}}
    root = make_root(tmp_path, projects={"alpha": {"code": "ALP"}}, config=config)
    seen: list[tuple[str, int | None]] = []
    with running_server(root) as server:
        create_task(server, mint(root))
        workers = _record_worker_at_shutdown(monkeypatch, server.project("alpha"), seen)
    assert len(workers) == 1 and workers[0] is not None
    assert seen == [("clean_shutdown", 0), ("release_lease", 0)], seen


def test_unload_reaps_the_worker_before_clean_shutdown(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = {"audit": {"debounce_seconds": 60, "max_interval_seconds": 60}}
    root = make_root(tmp_path, projects={"alpha": {"code": "ALP"}}, config=config)
    seen: list[tuple[str, int | None]] = []
    with running_server(root) as server:
        create_task(server, mint(root))
        workers = _record_worker_at_shutdown(monkeypatch, server.project("alpha"), seen)
        admin.project_lifecycle(root, "alpha", "unload")
        assert len(workers) == 1 and workers[0] is not None
        assert seen == [("clean_shutdown", 0), ("unload", 0), ("release_lease", 0)], seen


def test_abandon_reaps_the_worker(tmp_path: Path) -> None:
    root = make_root(tmp_path, projects={"alpha": {"code": "ALP"}})
    project, stream = direct_project(root, "alpha", QUICK)
    run(project, request("task.create", {"title": "one"}))
    wait_for(lambda: any(line["event"] == "audit_commit" for line in log_lines(stream)))
    worker = project.committer.stager._worker
    assert worker is not None
    project._abandon_audit()
    assert worker.returncode is not None
    with project.work:
        project.release()


def test_a_slow_audit_stage_logs_work_lock_slow(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The committer takes the work lock itself, so it logs its own slow hold."""
    monkeypatch.setattr(server_project, "SLOW_WORK_LOCK_SECONDS", 0.0)
    root = make_root(tmp_path, projects={"alpha": {"code": "ALP"}})
    project, stream = direct_project(root, "alpha", QUICK)
    try:
        run(project, request("task.create", {"title": "one"}))
        wait_for(lambda: any(line["event"] == "audit_commit" for line in log_lines(stream)))
    finally:
        close(project)
    slow = [line for line in log_lines(stream) if line["event"] == "work_lock_slow"]
    assert any(line["thread"].startswith("lattice-audit-") for line in slow), slow
