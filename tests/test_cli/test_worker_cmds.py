"""Tests for lattice worker CLI commands.

Tests definition loading, list, ps, complete, fail commands, and error paths.
Spawning (worker run) is only partially testable — actual subprocess/git operations
are not exercised here, but error paths and dedup logic are.
"""

from __future__ import annotations

import json
from pathlib import Path

from lattice.storage.fs import LATTICE_DIR


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _create_task(invoke) -> str:
    """Create a task and return its ULID."""
    r = invoke("create", "Test task for workers", "--actor", "human:test", "--json")
    assert r.exit_code == 0, f"create failed: {r.output}"
    return json.loads(r.output)["data"]["id"]


def _read_snapshot(initialized_root: Path, task_id: str) -> dict:
    path = initialized_root / LATTICE_DIR / "tasks" / f"{task_id}.json"
    return json.loads(path.read_text())


def _read_events(initialized_root: Path, task_id: str) -> list[dict]:
    path = initialized_root / LATTICE_DIR / "events" / f"{task_id}.jsonl"
    lines = path.read_text().strip().split("\n")
    return [json.loads(line) for line in lines if line]


def _inject_process_started(initialized_root: Path, task_id: str, process_type: str = "CodeReviewLite") -> str:
    """Inject a process_started event into a task and return the started_event_id.

    This simulates what ``worker run`` does, without needing git or subprocess.
    """
    from lattice.core.events import create_event, serialize_event
    from lattice.core.tasks import apply_event_to_snapshot, serialize_snapshot
    from lattice.storage.fs import atomic_write, jsonl_append
    from lattice.storage.locks import multi_lock

    lattice_dir = initialized_root / LATTICE_DIR
    event = create_event(
        type="process_started",
        task_id=task_id,
        actor="agent:review-bot",
        data={
            "process_type": process_type,
            "commit_sha": "abc12345",
            "timeout_minutes": 10,
        },
    )

    lock_keys = sorted([f"events_{task_id}", f"tasks_{task_id}"])
    with multi_lock(lattice_dir / "locks", lock_keys):
        snapshot_path = lattice_dir / "tasks" / f"{task_id}.json"
        snapshot = json.loads(snapshot_path.read_text())
        updated = apply_event_to_snapshot(snapshot, event)
        event_path = lattice_dir / "events" / f"{task_id}.jsonl"
        jsonl_append(event_path, serialize_event(event))
        atomic_write(snapshot_path, serialize_snapshot(updated))

    return event["id"]


def _setup_workers_dir(initialized_root: Path) -> Path:
    """Create a workers/ directory with a test definition."""
    workers_dir = initialized_root / "workers"
    workers_dir.mkdir(exist_ok=True)
    defn = {
        "name": "TestWorker",
        "description": "A test worker",
        "actor": "agent:test-worker",
        "engine": "claude",
        "prompt_file": "prompts/workers/test.md",
        "worktree": False,
        "timeout_minutes": 5,
    }
    (workers_dir / "test-worker.json").write_text(json.dumps(defn, indent=2))
    return workers_dir


# ---------------------------------------------------------------------------
# Worker definition loading (unit tests)
# ---------------------------------------------------------------------------


class TestWorkerDefLoading:
    """Test _load_worker_def and _list_worker_defs."""

    def test_load_by_name_case_insensitive(self, initialized_root: Path) -> None:
        from lattice.cli.worker_cmds import _load_worker_def

        workers_dir = _setup_workers_dir(initialized_root)
        result = _load_worker_def(workers_dir, "testworker")
        assert result is not None
        assert result["name"] == "TestWorker"

    def test_load_exact_name(self, initialized_root: Path) -> None:
        from lattice.cli.worker_cmds import _load_worker_def

        workers_dir = _setup_workers_dir(initialized_root)
        result = _load_worker_def(workers_dir, "TestWorker")
        assert result is not None
        assert result["name"] == "TestWorker"

    def test_load_not_found(self, initialized_root: Path) -> None:
        from lattice.cli.worker_cmds import _load_worker_def

        workers_dir = _setup_workers_dir(initialized_root)
        result = _load_worker_def(workers_dir, "NonExistent")
        assert result is None

    def test_list_worker_defs(self, initialized_root: Path) -> None:
        from lattice.cli.worker_cmds import _list_worker_defs

        workers_dir = _setup_workers_dir(initialized_root)
        defs = _list_worker_defs(workers_dir)
        assert len(defs) == 1
        assert defs[0]["name"] == "TestWorker"
        assert "_path" in defs[0]

    def test_list_skips_malformed_json(self, initialized_root: Path) -> None:
        from lattice.cli.worker_cmds import _list_worker_defs

        workers_dir = _setup_workers_dir(initialized_root)
        (workers_dir / "bad.json").write_text("not json{{{")
        defs = _list_worker_defs(workers_dir)
        assert len(defs) == 1  # Only the valid one


# ---------------------------------------------------------------------------
# lattice worker list
# ---------------------------------------------------------------------------


class TestWorkerList:
    """Test the worker list command."""

    def test_no_workers_dir(self, invoke) -> None:
        result = invoke("worker", "list")
        assert result.exit_code == 0
        assert "No workers/ directory" in result.output

    def test_lists_definitions(self, invoke, initialized_root: Path) -> None:
        _setup_workers_dir(initialized_root)
        result = invoke("worker", "list")
        assert result.exit_code == 0
        assert "TestWorker" in result.output
        assert "claude" in result.output


# ---------------------------------------------------------------------------
# lattice worker complete
# ---------------------------------------------------------------------------


class TestWorkerComplete:
    """Test the worker complete command."""

    def test_complete_removes_active_process(self, invoke, initialized_root: Path) -> None:
        task_id = _create_task(invoke)
        started_eid = _inject_process_started(initialized_root, task_id)

        # Verify active
        snap = _read_snapshot(initialized_root, task_id)
        assert len(snap["active_processes"]) == 1

        # Complete it
        result = invoke(
            "worker", "complete", task_id, started_eid,
            "--actor", "agent:review-bot",
        )
        assert result.exit_code == 0
        assert "completed" in result.output.lower()

        # Verify removed
        snap = _read_snapshot(initialized_root, task_id)
        assert snap["active_processes"] == []

    def test_complete_appends_event(self, invoke, initialized_root: Path) -> None:
        task_id = _create_task(invoke)
        started_eid = _inject_process_started(initialized_root, task_id)

        invoke(
            "worker", "complete", task_id, started_eid,
            "--actor", "agent:review-bot",
        )

        events = _read_events(initialized_root, task_id)
        complete_events = [e for e in events if e["type"] == "process_completed"]
        assert len(complete_events) == 1
        assert complete_events[0]["data"]["started_event_id"] == started_eid
        assert complete_events[0]["data"]["process_type"] == "CodeReviewLite"

    def test_complete_with_result_text(self, invoke, initialized_root: Path) -> None:
        task_id = _create_task(invoke)
        started_eid = _inject_process_started(initialized_root, task_id)

        invoke(
            "worker", "complete", task_id, started_eid,
            "--actor", "agent:review-bot",
            "--result", "Review passed — LGTM",
        )

        events = _read_events(initialized_root, task_id)
        complete_events = [e for e in events if e["type"] == "process_completed"]
        assert complete_events[0]["data"]["result"] == "Review passed — LGTM"

    def test_complete_json_output(self, invoke, initialized_root: Path) -> None:
        task_id = _create_task(invoke)
        started_eid = _inject_process_started(initialized_root, task_id)

        result = invoke(
            "worker", "complete", task_id, started_eid,
            "--actor", "agent:review-bot",
            "--json",
        )
        assert result.exit_code == 0
        parsed = json.loads(result.output)
        assert parsed["ok"] is True
        assert "event_id" in parsed["data"]

    def test_complete_not_found(self, invoke, initialized_root: Path) -> None:
        task_id = _create_task(invoke)
        # No process started — complete should fail
        result = invoke(
            "worker", "complete", task_id, "ev_01ZZZZZZZZZZZZZZZZZZZZZZZZ",
            "--actor", "agent:review-bot",
        )
        assert result.exit_code == 1
        assert "not found" in result.stderr.lower() or "No active process" in result.output

    def test_complete_not_found_json(self, invoke, initialized_root: Path) -> None:
        task_id = _create_task(invoke)
        result = invoke(
            "worker", "complete", task_id, "ev_01ZZZZZZZZZZZZZZZZZZZZZZZZ",
            "--actor", "agent:review-bot",
            "--json",
        )
        assert result.exit_code == 1
        parsed = json.loads(result.output)
        assert parsed["ok"] is False
        assert parsed["error"]["code"] == "NOT_FOUND"

    def test_complete_leaves_other_processes(self, invoke, initialized_root: Path) -> None:
        task_id = _create_task(invoke)
        eid_1 = _inject_process_started(initialized_root, task_id, "WorkerA")
        _inject_process_started(initialized_root, task_id, "WorkerB")

        snap = _read_snapshot(initialized_root, task_id)
        assert len(snap["active_processes"]) == 2

        invoke(
            "worker", "complete", task_id, eid_1,
            "--actor", "agent:worker",
        )

        snap = _read_snapshot(initialized_root, task_id)
        assert len(snap["active_processes"]) == 1
        assert snap["active_processes"][0]["process_type"] == "WorkerB"

    def test_complete_invalid_actor(self, invoke, initialized_root: Path) -> None:
        task_id = _create_task(invoke)
        started_eid = _inject_process_started(initialized_root, task_id)

        result = invoke(
            "worker", "complete", task_id, started_eid,
            "--actor", "bad-format",
        )
        assert result.exit_code == 1
        assert "Invalid actor" in result.stderr


# ---------------------------------------------------------------------------
# lattice worker fail
# ---------------------------------------------------------------------------


class TestWorkerFail:
    """Test the worker fail command."""

    def test_fail_removes_active_process(self, invoke, initialized_root: Path) -> None:
        task_id = _create_task(invoke)
        started_eid = _inject_process_started(initialized_root, task_id)

        result = invoke(
            "worker", "fail", task_id, started_eid,
            "--actor", "agent:review-bot",
            "--error", "Crashed during analysis",
        )
        assert result.exit_code == 0
        assert "failed" in result.output.lower()

        snap = _read_snapshot(initialized_root, task_id)
        assert snap["active_processes"] == []

    def test_fail_appends_event(self, invoke, initialized_root: Path) -> None:
        task_id = _create_task(invoke)
        started_eid = _inject_process_started(initialized_root, task_id)

        invoke(
            "worker", "fail", task_id, started_eid,
            "--actor", "agent:review-bot",
            "--error", "OOM",
        )

        events = _read_events(initialized_root, task_id)
        fail_events = [e for e in events if e["type"] == "process_failed"]
        assert len(fail_events) == 1
        assert fail_events[0]["data"]["started_event_id"] == started_eid
        assert fail_events[0]["data"]["error"] == "OOM"

    def test_fail_json_output(self, invoke, initialized_root: Path) -> None:
        task_id = _create_task(invoke)
        started_eid = _inject_process_started(initialized_root, task_id)

        result = invoke(
            "worker", "fail", task_id, started_eid,
            "--actor", "agent:review-bot",
            "--json",
        )
        assert result.exit_code == 0
        parsed = json.loads(result.output)
        assert parsed["ok"] is True
        assert "event_id" in parsed["data"]

    def test_fail_best_effort_no_matching_process(self, invoke, initialized_root: Path) -> None:
        """Fail should succeed even when the process isn't in active_processes.

        This handles cases where the process_started event was compensated already,
        or when cleanup is needed on an orphaned state.
        """
        task_id = _create_task(invoke)
        # No process started — fail should still succeed (best-effort)
        result = invoke(
            "worker", "fail", task_id, "ev_01ZZZZZZZZZZZZZZZZZZZZZZZZ",
            "--actor", "agent:review-bot",
            "--error", "Cleanup attempt",
        )
        assert result.exit_code == 0
        assert "failed" in result.output.lower()

    def test_fail_invalid_actor(self, invoke, initialized_root: Path) -> None:
        task_id = _create_task(invoke)
        started_eid = _inject_process_started(initialized_root, task_id)

        result = invoke(
            "worker", "fail", task_id, started_eid,
            "--actor", "bad-format",
        )
        assert result.exit_code == 1
        assert "Invalid actor" in result.stderr


# ---------------------------------------------------------------------------
# lattice worker ps
# ---------------------------------------------------------------------------


class TestWorkerPs:
    """Test the worker ps command."""

    def test_ps_no_active(self, invoke) -> None:
        _create_task(invoke)
        result = invoke("worker", "ps")
        assert result.exit_code == 0
        assert "No active worker" in result.output

    def test_ps_shows_active(self, invoke, initialized_root: Path) -> None:
        task_id = _create_task(invoke)
        _inject_process_started(initialized_root, task_id)

        result = invoke("worker", "ps")
        assert result.exit_code == 0
        assert "CodeReviewLite" in result.output

    def test_ps_json_output(self, invoke, initialized_root: Path) -> None:
        task_id = _create_task(invoke)
        _inject_process_started(initialized_root, task_id)

        result = invoke("worker", "ps", "--json")
        assert result.exit_code == 0
        parsed = json.loads(result.output)
        assert parsed["ok"] is True
        assert len(parsed["data"]) == 1
        assert parsed["data"][0]["process_type"] == "CodeReviewLite"
        assert parsed["data"][0]["task_id"] == task_id

    def test_ps_json_empty(self, invoke) -> None:
        _create_task(invoke)
        result = invoke("worker", "ps", "--json")
        assert result.exit_code == 0
        parsed = json.loads(result.output)
        assert parsed["ok"] is True
        assert parsed["data"] == []

    def test_ps_multiple_tasks(self, invoke, initialized_root: Path) -> None:
        t1 = _create_task(invoke)
        t2 = _create_task(invoke)
        _inject_process_started(initialized_root, t1, "WorkerA")
        _inject_process_started(initialized_root, t2, "WorkerB")

        result = invoke("worker", "ps", "--json")
        parsed = json.loads(result.output)
        assert len(parsed["data"]) == 2
        types = {d["process_type"] for d in parsed["data"]}
        assert types == {"WorkerA", "WorkerB"}


# ---------------------------------------------------------------------------
# Full lifecycle: start → complete
# ---------------------------------------------------------------------------


class TestWorkerLifecycle:
    """Integration tests for the full worker lifecycle."""

    def test_start_then_complete(self, invoke, initialized_root: Path) -> None:
        task_id = _create_task(invoke)
        started_eid = _inject_process_started(initialized_root, task_id)

        # PS shows active
        ps_result = invoke("worker", "ps", "--json")
        data = json.loads(ps_result.output)["data"]
        assert len(data) == 1

        # Complete
        invoke(
            "worker", "complete", task_id, started_eid,
            "--actor", "agent:review-bot",
        )

        # PS now empty
        ps_result = invoke("worker", "ps", "--json")
        data = json.loads(ps_result.output)["data"]
        assert len(data) == 0

    def test_start_then_fail(self, invoke, initialized_root: Path) -> None:
        task_id = _create_task(invoke)
        started_eid = _inject_process_started(initialized_root, task_id)

        invoke(
            "worker", "fail", task_id, started_eid,
            "--actor", "agent:review-bot",
            "--error", "Test failure",
        )

        snap = _read_snapshot(initialized_root, task_id)
        assert snap["active_processes"] == []

        events = _read_events(initialized_root, task_id)
        types = [e["type"] for e in events]
        assert "process_started" in types
        assert "process_failed" in types

    def test_double_complete_is_not_found(self, invoke, initialized_root: Path) -> None:
        """Completing the same process twice should fail on the second attempt."""
        task_id = _create_task(invoke)
        started_eid = _inject_process_started(initialized_root, task_id)

        # First complete succeeds
        r1 = invoke(
            "worker", "complete", task_id, started_eid,
            "--actor", "agent:review-bot",
        )
        assert r1.exit_code == 0

        # Second complete fails
        r2 = invoke(
            "worker", "complete", task_id, started_eid,
            "--actor", "agent:review-bot",
        )
        assert r2.exit_code == 1


# ---------------------------------------------------------------------------
# Worker run error paths (no git/subprocess needed)
# ---------------------------------------------------------------------------


class TestWorkerRunErrors:
    """Test error paths in worker run that don't require git or subprocess."""

    def test_run_no_workers_dir(self, invoke) -> None:
        task_id = _create_task(invoke)
        result = invoke(
            "worker", "run", "TestWorker", task_id,
            "--actor", "agent:test",
        )
        assert result.exit_code == 1
        assert "No workers/ directory" in result.stderr

    def test_run_worker_not_found(self, invoke, initialized_root: Path) -> None:
        task_id = _create_task(invoke)
        _setup_workers_dir(initialized_root)
        result = invoke(
            "worker", "run", "NonExistent", task_id,
            "--actor", "agent:test",
        )
        assert result.exit_code == 1
        assert "not found" in result.stderr.lower()
        assert "TestWorker" in result.stderr  # Shows available workers

    def test_run_invalid_actor(self, invoke, initialized_root: Path) -> None:
        task_id = _create_task(invoke)
        _setup_workers_dir(initialized_root)
        result = invoke(
            "worker", "run", "TestWorker", task_id,
            "--actor", "bad-format",
        )
        assert result.exit_code == 1
        assert "Invalid actor" in result.stderr

    def test_run_worker_not_found_json(self, invoke, initialized_root: Path) -> None:
        task_id = _create_task(invoke)
        _setup_workers_dir(initialized_root)
        result = invoke(
            "worker", "run", "NonExistent", task_id,
            "--actor", "agent:test",
            "--json",
        )
        assert result.exit_code == 1
        parsed = json.loads(result.output)
        assert parsed["ok"] is False
        assert parsed["error"]["code"] == "NOT_FOUND"

    def test_run_task_not_found(self, invoke, initialized_root: Path) -> None:
        _setup_workers_dir(initialized_root)
        result = invoke(
            "worker", "run", "TestWorker", "task_01ZZZZZZZZZZZZZZZZZZZZZZZZ",
            "--actor", "agent:test",
        )
        assert result.exit_code == 1
        assert "not found" in result.stderr.lower()
