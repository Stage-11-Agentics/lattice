"""The task gate bounds lock descriptors without loosening exclusion (LAT-339).

``task_locks`` holds the gate shared plus each task's keys; ``all_task_locks``
holds it exclusively and nothing per task, so a whole-board operation holds a
fixed number of descriptors. At the stock macOS limit (256) the old whole-board
paths, which held two descriptors per task, failed with EMFILE.
"""

from __future__ import annotations

import os
import subprocess
import sys
import textwrap
import threading
from pathlib import Path

import pytest
from ulid import ULID

from lattice.core.events import create_event, serialize_event
from lattice.core.tasks import apply_event_to_snapshot, serialize_snapshot
from lattice.storage.locks import LockTimeout, all_task_locks, task_locks

pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="POSIX descriptor limits")

FD_LIMIT = 256
BOARD_TASKS = 300  # two keys each: 600 descriptors under the old whole-board lock


def _open_fds() -> int:
    return len(os.listdir("/dev/fd"))


def _holder(locks_dir: Path, body: str) -> subprocess.Popen:
    """A child process that takes a lock, prints ``held``, and keeps it until stdin closes."""
    code = textwrap.dedent(
        f"""
        import sys
        from pathlib import Path
        from lattice.storage.locks import all_task_locks, task_locks
        locks_dir = Path({str(locks_dir)!r})
        with {body}:
            print("held", flush=True)
            sys.stdin.read()
        """
    )
    proc = subprocess.Popen(
        [sys.executable, "-c", code],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        text=True,
    )
    assert proc.stdout is not None and proc.stdout.readline().strip() == "held"
    return proc


def _release(proc: subprocess.Popen) -> None:
    assert proc.stdin is not None
    proc.stdin.close()
    proc.wait(timeout=10)


class TestTaskGate:
    def test_all_task_locks_holds_fixed_descriptors(self, tmp_path: Path) -> None:
        before = _open_fds()
        with all_task_locks(tmp_path, ["events__lifecycle", "ids_json"]):
            assert _open_fds() - before <= 3

    def test_different_tasks_run_concurrently(self, tmp_path: Path) -> None:
        held = threading.Event()
        done = threading.Event()

        def hold() -> None:
            with task_locks(tmp_path, ["task_a"]):
                held.set()
                done.wait(5)

        thread = threading.Thread(target=hold)
        thread.start()
        try:
            assert held.wait(5)
            with task_locks(tmp_path, ["task_b"], timeout=0.2):
                pass
        finally:
            done.set()
            thread.join()

    def test_same_task_excludes(self, tmp_path: Path) -> None:
        proc = _holder(tmp_path, 'task_locks(locks_dir, ["task_a"])')
        try:
            with pytest.raises(LockTimeout, match="task_a"):
                with task_locks(tmp_path, ["task_a"], timeout=0.2):
                    pass  # pragma: no cover
        finally:
            _release(proc)
        with task_locks(tmp_path, ["task_a"], timeout=0.2):
            pass

    def test_board_lock_excludes_a_task_holder_in_another_process(self, tmp_path: Path) -> None:
        proc = _holder(tmp_path, 'task_locks(locks_dir, ["task_a"])')
        try:
            with pytest.raises(LockTimeout, match="task_gate"):
                with all_task_locks(tmp_path, timeout=0.2):
                    pass  # pragma: no cover
        finally:
            _release(proc)
        with all_task_locks(tmp_path, timeout=0.2):
            pass

    def test_task_holder_waits_for_board_lock_in_another_process(self, tmp_path: Path) -> None:
        proc = _holder(tmp_path, "all_task_locks(locks_dir)")
        try:
            with pytest.raises(LockTimeout, match="task_gate"):
                with task_locks(tmp_path, ["task_new"], timeout=0.2):
                    pass  # pragma: no cover
        finally:
            _release(proc)
        with task_locks(tmp_path, ["task_new"], timeout=0.2):
            pass

    def test_task_locks_nest_under_board_lock(self, tmp_path: Path) -> None:
        with all_task_locks(tmp_path, timeout=0.2):
            with task_locks(tmp_path, ["task_a"], timeout=0.2):
                with task_locks(tmp_path, ["task_b"], timeout=0.2):
                    pass
        with all_task_locks(tmp_path, timeout=0.2):
            pass

    def test_board_lock_inside_task_locks_raises(self, tmp_path: Path) -> None:
        with task_locks(tmp_path, ["task_a"], timeout=0.2):
            with pytest.raises(RuntimeError, match="cannot be taken"):
                with all_task_locks(tmp_path, timeout=0.2):
                    pass  # pragma: no cover
        with all_task_locks(tmp_path, timeout=0.2):
            pass


def _write_board(lattice_dir: Path, count: int) -> None:
    for i in range(count):
        task_id = f"task_{ULID()}"
        event = create_event(
            type="task_created",
            task_id=task_id,
            actor="human:test",
            data={"title": f"Task {i}", "status": "backlog", "priority": "medium", "type": "task"},
        )
        (lattice_dir / "tasks" / f"{task_id}.json").write_text(
            serialize_snapshot(apply_event_to_snapshot(None, event))
        )
        (lattice_dir / "events" / f"{task_id}.jsonl").write_text(serialize_event(event))
        with (lattice_dir / "events" / "_lifecycle.jsonl").open("a") as lifecycle:
            lifecycle.write(serialize_event(event))


@pytest.mark.parametrize("command", [["rebuild", "--all"], ["doctor"]])
def test_board_operations_under_stock_descriptor_limit(
    initialized_root: Path, command: list[str]
) -> None:
    """``rebuild --all`` and ``doctor`` lock the whole board; at ``ulimit -n 256`` a
    board of 300 tasks fit, where one descriptor per task key did not."""
    _write_board(initialized_root / ".lattice", BOARD_TASKS)
    shim = (
        "import resource\n"
        "_, hard = resource.getrlimit(resource.RLIMIT_NOFILE)\n"
        f"resource.setrlimit(resource.RLIMIT_NOFILE, ({FD_LIMIT}, hard))\n"
        "from lattice.cli.main import cli\n"
        "cli()\n"
    )
    env = {k: v for k, v in os.environ.items() if not k.startswith("LATTICE_")}
    proc = subprocess.run(
        [sys.executable, "-c", shim, *command, "--json"],
        cwd=initialized_root,
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert "Too many open files" not in proc.stderr + proc.stdout
    assert proc.returncode == 0, proc.stdout + proc.stderr
