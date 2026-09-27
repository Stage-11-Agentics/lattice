"""The audit stage runs in a worker process, so server threads cannot slow it (LAT-340).

The stage walks every board file under the project's work lock. In-process,
each file's syscalls wait behind busy request threads for the GIL: on Atlas
under AC-42 a 0.15 s stage took 9 s, writers queued behind the lock and
readers' catch-ups timed out. In the worker it keeps its idle speed.
"""

from __future__ import annotations

import shutil
import subprocess
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

import pytest

from lattice.server import audit
from lattice.server import project as server_project
from lattice.server.audit import GitError, Stager
from lattice.server.config import AuditConfig
from lattice.server.testing import make_root, wait_for
from tests.test_server.audit_helpers import close, direct_project, log_lines
from tests.test_server.faults import request, run

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="git is not installed")

BOARD_FILES = 3000
#: With two spinning threads, one changed file of 3,000: the worker staged in
#: 0.11 s and in-process staging took 5-9 s (laptop, LAT-340).
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


def test_busy_server_threads_do_not_slow_the_stage(tmp_path: Path) -> None:
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
