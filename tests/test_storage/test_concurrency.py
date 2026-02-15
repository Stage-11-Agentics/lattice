"""Concurrency tests — verify no data corruption under concurrent agent writes."""

from __future__ import annotations

import json
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest
from click.testing import CliRunner  # used for single-thread CLI setup
from ulid import ULID

from lattice.core.events import create_event, serialize_event
from lattice.core.ids import generate_task_id
from lattice.core.tasks import apply_event_to_snapshot, serialize_snapshot
from lattice.storage.fs import atomic_write, jsonl_append
from lattice.storage.locks import LockTimeout, lattice_lock, multi_lock


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_task_created_event(task_id: str, title: str = "Test task") -> dict:
    """Build a minimal task_created event."""
    return create_event(
        type="task_created",
        task_id=task_id,
        actor="human:test",
        data={
            "title": title,
            "status": "backlog",
            "type": "task",
            "priority": "medium",
        },
    )


def _make_comment_event(task_id: str, body: str) -> dict:
    """Build a comment_added event for an existing task."""
    return create_event(
        type="comment_added",
        task_id=task_id,
        actor="human:test",
        data={"body": body},
    )


def _make_status_event(task_id: str, from_status: str, to_status: str) -> dict:
    """Build a status_changed event."""
    return create_event(
        type="status_changed",
        task_id=task_id,
        actor="human:test",
        data={"from": from_status, "to": to_status},
    )


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestConcurrentJsonlAppends:
    """10 threads each append 20 events — verify all 200 lines present."""

    def test_concurrent_jsonl_appends(self, tmp_path: Path) -> None:
        locks_dir = tmp_path / "locks"
        locks_dir.mkdir()
        log_path = tmp_path / "events.jsonl"
        log_path.touch()

        num_threads = 10
        events_per_thread = 20
        barrier = threading.Barrier(num_threads)
        errors: list[Exception] = []

        task_id = generate_task_id()

        def writer(thread_id: int) -> None:
            try:
                barrier.wait(timeout=5)
                for i in range(events_per_thread):
                    event = _make_comment_event(task_id, f"t{thread_id}-e{i}")
                    line = serialize_event(event)
                    with lattice_lock(locks_dir, "events_log", timeout=10):
                        jsonl_append(log_path, line)
            except Exception as exc:
                errors.append(exc)

        threads = [threading.Thread(target=writer, args=(t,)) for t in range(num_threads)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=30)

        assert not errors, f"Thread errors: {errors}"

        lines = log_path.read_text().strip().split("\n")
        assert len(lines) == num_threads * events_per_thread

        # Every line must be valid JSON with no interleaving
        for line in lines:
            parsed = json.loads(line)
            assert "id" in parsed
            assert "type" in parsed


class TestConcurrentAtomicWrites:
    """10 threads each call atomic_write on the same snapshot path."""

    def test_concurrent_atomic_writes(self, tmp_path: Path) -> None:
        snapshot_path = tmp_path / "task.json"
        num_threads = 10
        barrier = threading.Barrier(num_threads)
        errors: list[Exception] = []

        task_id = generate_task_id()

        def writer(thread_id: int) -> None:
            try:
                barrier.wait(timeout=5)
                event = _make_task_created_event(task_id, f"Title from thread {thread_id}")
                snapshot = apply_event_to_snapshot(None, event)
                content = serialize_snapshot(snapshot)
                atomic_write(snapshot_path, content)
            except Exception as exc:
                errors.append(exc)

        threads = [threading.Thread(target=writer, args=(t,)) for t in range(num_threads)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=30)

        assert not errors, f"Thread errors: {errors}"

        # Final file must be valid JSON (one of the threads' writes)
        content = snapshot_path.read_text()
        parsed = json.loads(content)
        assert parsed["id"] == task_id
        assert parsed["title"].startswith("Title from thread ")


class TestMultiLockDeadlockResistance:
    """Thread A locks [a, b], Thread B locks [b, a] — sorted ordering prevents deadlock."""

    @pytest.mark.timeout(10)
    def test_multi_lock_deadlock_resistance(self, tmp_path: Path) -> None:
        locks_dir = tmp_path / "locks"
        locks_dir.mkdir()

        barrier = threading.Barrier(2)
        results: list[str] = []
        errors: list[Exception] = []

        def locker_a() -> None:
            try:
                barrier.wait(timeout=5)
                with multi_lock(locks_dir, ["a", "b"], timeout=5):
                    results.append("A acquired")
                    time.sleep(0.05)
            except Exception as exc:
                errors.append(exc)

        def locker_b() -> None:
            try:
                barrier.wait(timeout=5)
                # Intentionally reversed order — multi_lock sorts internally
                with multi_lock(locks_dir, ["b", "a"], timeout=5):
                    results.append("B acquired")
                    time.sleep(0.05)
            except Exception as exc:
                errors.append(exc)

        t_a = threading.Thread(target=locker_a)
        t_b = threading.Thread(target=locker_b)
        t_a.start()
        t_b.start()
        t_a.join(timeout=10)
        t_b.join(timeout=10)

        assert not errors, f"Deadlock or error: {errors}"
        assert "A acquired" in results
        assert "B acquired" in results


class TestConcurrentCreateViaCli:
    """Two processes create same task ID — idempotent with same payload."""

    def test_concurrent_create_via_cli(
        self, initialized_root: Path, cli_env: dict[str, str]
    ) -> None:
        task_id = f"task_{ULID()}"

        # Build a subprocess script that invokes Click CLI directly
        # This avoids needing the `lattice` script on PATH
        script = "import sys, json; from lattice.cli.main import cli; sys.exit(cli())"

        # Launch two subprocesses concurrently with the same task ID + payload
        procs = []
        for _ in range(2):
            p = subprocess.Popen(
                [
                    sys.executable,
                    "-c",
                    script,
                    "create",
                    "Concurrent Task",
                    "--id",
                    task_id,
                    "--actor",
                    "human:test",
                    "--json",
                ],
                env=cli_env,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
            procs.append(p)

        outputs = []
        for p in procs:
            stdout, stderr = p.communicate(timeout=15)
            outputs.append((p.returncode, stdout.decode(), stderr.decode()))

        # With same payload both should succeed (idempotent) OR one succeeds
        # and one reports CONFLICT
        ok_count = 0
        for _rc, stdout, _stderr in outputs:
            if stdout.strip():
                parsed = json.loads(stdout)
                if parsed.get("ok"):
                    ok_count += 1
        assert ok_count >= 1, f"At least one create must succeed: {outputs}"

        # Verify the task file is valid
        task_path = initialized_root / ".lattice" / "tasks" / f"{task_id}.json"
        assert task_path.exists()
        task_data = json.loads(task_path.read_text())
        assert task_data["id"] == task_id
        assert task_data["title"] == "Concurrent Task"


class TestConcurrentStatusChanges:
    """Two threads apply status changes to same task — both events recorded."""

    def test_concurrent_status_changes(
        self, initialized_root: Path, cli_env: dict[str, str], create_task
    ) -> None:
        # Create a task and move it to ready
        task = create_task("Status Race Task")
        task_id = task["id"]

        runner = CliRunner()
        from lattice.cli.main import cli

        result = runner.invoke(
            cli,
            ["status", task_id, "ready", "--actor", "human:test"],
            env=cli_env,
        )
        assert result.exit_code == 0

        # Read current snapshot
        lattice_dir = initialized_root / ".lattice"
        snapshot = json.loads((lattice_dir / "tasks" / f"{task_id}.json").read_text())
        assert snapshot["status"] == "ready"

        # Two threads apply different status changes using core APIs + write path
        # (bypassing CLI to avoid CliRunner thread-safety issues)
        barrier = threading.Barrier(2)
        errors: list[Exception] = []

        def change_status(from_status: str, to_status: str) -> None:
            try:
                barrier.wait(timeout=5)
                event = _make_status_event(task_id, from_status, to_status)
                # Read snapshot, apply event, and write — all under lock
                lock_keys = sorted([f"events_{task_id}", f"tasks_{task_id}"])
                with multi_lock(lattice_dir / "locks", lock_keys, timeout=10):
                    current = json.loads((lattice_dir / "tasks" / f"{task_id}.json").read_text())
                    updated = apply_event_to_snapshot(current, event)
                    jsonl_append(
                        lattice_dir / "events" / f"{task_id}.jsonl",
                        serialize_event(event),
                    )
                    atomic_write(
                        lattice_dir / "tasks" / f"{task_id}.json",
                        serialize_snapshot(updated),
                    )
            except Exception as exc:
                errors.append(exc)

        t1 = threading.Thread(target=change_status, args=("ready", "in_progress"))
        t2 = threading.Thread(target=change_status, args=("ready", "blocked"))
        t1.start()
        t2.start()
        t1.join(timeout=15)
        t2.join(timeout=15)

        assert not errors, f"Thread errors: {errors}"

        # Both events should be in the log
        event_path = lattice_dir / "events" / f"{task_id}.jsonl"
        event_lines = event_path.read_text().strip().split("\n")
        events = [json.loads(line) for line in event_lines]

        # task_created + status(ready) + 2 concurrent changes = 4
        assert len(events) == 4

        status_events = [e for e in events if e["type"] == "status_changed"]
        assert len(status_events) == 3  # ready + in_progress + blocked

        # All events are valid
        for ev in events:
            assert "id" in ev
            assert "type" in ev
            assert ev["task_id"] == task_id


class TestLockContentionUnderLoad:
    """10 threads writing events to 5 tasks (2 per task) — no corruption."""

    def test_lock_contention_under_load(self, tmp_path: Path) -> None:
        locks_dir = tmp_path / "locks"
        locks_dir.mkdir()
        events_dir = tmp_path / "events"
        events_dir.mkdir()

        num_tasks = 5
        threads_per_task = 2
        task_ids = [generate_task_id() for _ in range(num_tasks)]

        # Create empty event files
        for tid in task_ids:
            (events_dir / f"{tid}.jsonl").touch()

        barrier = threading.Barrier(num_tasks * threads_per_task)
        errors: list[Exception] = []
        events_per_thread = 10

        def writer(task_id: str, thread_id: int) -> None:
            try:
                barrier.wait(timeout=5)
                for i in range(events_per_thread):
                    event = _make_comment_event(task_id, f"t{thread_id}-e{i}")
                    line = serialize_event(event)
                    lock_key = f"events_{task_id}"
                    with lattice_lock(locks_dir, lock_key, timeout=10):
                        jsonl_append(events_dir / f"{task_id}.jsonl", line)
            except Exception as exc:
                errors.append(exc)

        threads = []
        for idx, tid in enumerate(task_ids):
            for t in range(threads_per_task):
                thread = threading.Thread(
                    target=writer,
                    args=(tid, idx * threads_per_task + t),
                )
                threads.append(thread)

        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=30)

        assert not errors, f"Thread errors: {errors}"

        # Each task file should have exactly threads_per_task * events_per_thread lines
        expected = threads_per_task * events_per_thread
        for tid in task_ids:
            lines = (events_dir / f"{tid}.jsonl").read_text().strip().split("\n")
            assert len(lines) == expected, (
                f"Task {tid}: expected {expected} events, got {len(lines)}"
            )
            for line in lines:
                parsed = json.loads(line)
                assert parsed["task_id"] == tid


class TestLockTimeoutBehavior:
    """Holding a lock in one thread, another thread times out trying to acquire it."""

    def test_lock_timeout_behavior(self, tmp_path: Path) -> None:
        locks_dir = tmp_path / "locks"
        locks_dir.mkdir()

        lock_acquired = threading.Event()
        can_release = threading.Event()
        timeout_raised = threading.Event()
        errors: list[Exception] = []

        def holder() -> None:
            try:
                with lattice_lock(locks_dir, "contested", timeout=10):
                    lock_acquired.set()
                    can_release.wait(timeout=10)
            except Exception as exc:
                errors.append(exc)

        def waiter() -> None:
            try:
                lock_acquired.wait(timeout=5)
                with lattice_lock(locks_dir, "contested", timeout=0.1):
                    pass  # Should not reach here
            except LockTimeout:
                timeout_raised.set()
            except Exception as exc:
                errors.append(exc)

        t_holder = threading.Thread(target=holder)
        t_waiter = threading.Thread(target=waiter)

        t_holder.start()
        t_waiter.start()

        t_waiter.join(timeout=10)
        can_release.set()
        t_holder.join(timeout=10)

        assert not errors, f"Unexpected errors: {errors}"
        assert timeout_raised.is_set(), "LockTimeout was not raised"


class TestConcurrentMultiTaskLifecycle:
    """Multiple processes create different tasks via CLI simultaneously."""

    def test_concurrent_multi_task_lifecycle(
        self, initialized_root: Path, cli_env: dict[str, str]
    ) -> None:
        num_tasks = 6
        task_ids = [f"task_{ULID()}" for _ in range(num_tasks)]

        script = "import sys; from lattice.cli.main import cli; sys.exit(cli())"

        # Launch all subprocesses concurrently
        procs = []
        for idx, tid in enumerate(task_ids):
            p = subprocess.Popen(
                [
                    sys.executable,
                    "-c",
                    script,
                    "create",
                    f"Task {idx}",
                    "--id",
                    tid,
                    "--actor",
                    "human:test",
                    "--json",
                ],
                env=cli_env,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
            procs.append((idx, p))

        # Collect results
        for idx, p in procs:
            stdout, stderr = p.communicate(timeout=15)
            assert p.returncode == 0, f"Task {idx} failed (rc={p.returncode}): {stderr.decode()}"
            parsed = json.loads(stdout.decode())
            assert parsed["ok"] is True

        # All task snapshot files should exist and be valid
        for idx, tid in enumerate(task_ids):
            path = initialized_root / ".lattice" / "tasks" / f"{tid}.json"
            assert path.exists(), f"Snapshot missing for {tid}"
            data = json.loads(path.read_text())
            assert data["title"] == f"Task {idx}"

        # Lifecycle log should contain all task_created events
        lifecycle_path = initialized_root / ".lattice" / "events" / "_lifecycle.jsonl"
        lifecycle_lines = lifecycle_path.read_text().strip().split("\n")
        lifecycle_events = [json.loads(line) for line in lifecycle_lines if line]
        lifecycle_task_ids = {e["task_id"] for e in lifecycle_events}
        for tid in task_ids:
            assert tid in lifecycle_task_ids, f"{tid} missing from lifecycle log"


class TestConcurrentAppendAndAtomicWriteIntegrity:
    """Interleaved JSONL appends and atomic snapshot writes on the same task."""

    def test_interleaved_append_and_write(self, tmp_path: Path) -> None:
        locks_dir = tmp_path / "locks"
        locks_dir.mkdir()
        events_dir = tmp_path / "events"
        events_dir.mkdir()
        tasks_dir = tmp_path / "tasks"
        tasks_dir.mkdir()

        task_id = generate_task_id()
        event_path = events_dir / f"{task_id}.jsonl"
        snapshot_path = tasks_dir / f"{task_id}.json"

        # Seed with a task_created event and initial snapshot
        created_event = _make_task_created_event(task_id)
        snapshot = apply_event_to_snapshot(None, created_event)
        event_path.write_text(serialize_event(created_event))
        atomic_write(snapshot_path, serialize_snapshot(snapshot))

        num_threads = 8
        barrier = threading.Barrier(num_threads)
        errors: list[Exception] = []

        def writer(thread_id: int) -> None:
            try:
                barrier.wait(timeout=5)
                for i in range(5):
                    event = _make_comment_event(task_id, f"t{thread_id}-c{i}")
                    line = serialize_event(event)
                    lock_keys = sorted([f"events_{task_id}", f"tasks_{task_id}"])
                    with multi_lock(locks_dir, lock_keys, timeout=10):
                        jsonl_append(event_path, line)
                        # Re-read and update snapshot under lock
                        current = json.loads(snapshot_path.read_text())
                        updated = apply_event_to_snapshot(current, event)
                        atomic_write(snapshot_path, serialize_snapshot(updated))
            except Exception as exc:
                errors.append(exc)

        threads = [threading.Thread(target=writer, args=(t,)) for t in range(num_threads)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=30)

        assert not errors, f"Thread errors: {errors}"

        # Verify event log integrity
        lines = event_path.read_text().strip().split("\n")
        expected_lines = 1 + num_threads * 5  # 1 created + 8*5 comments
        assert len(lines) == expected_lines

        # Verify final snapshot is valid JSON
        final_snapshot = json.loads(snapshot_path.read_text())
        assert final_snapshot["id"] == task_id
        assert final_snapshot["last_event_id"] is not None
