"""Tests for core review logic: failure tracking, temp cleanup, state helpers."""

from __future__ import annotations

import json
import os
import subprocess
import tempfile
import threading
from pathlib import Path

import pytest

from lattice.core import review as review_mod
from lattice.core.review import (
    DEFAULT_AGENT_TIMEOUT,
    DEFAULT_MAX_DIFF_LINES,
    FAILURE_THRESHOLD,
    _extract_actor_str,
    pid_alive,
    cap_diff,
    claim_review_state,
    cleanup_temp_files,
    clear_review_state,
    count_agent_failures,
    create_failure_diagnostic_task,
    read_review_state,
    record_agent_failure,
    write_review_state,
)
from lattice.storage.review_state import write_review_state_file


@pytest.fixture
def lattice_dir(tmp_path: Path) -> Path:
    """Create a minimal .lattice directory."""
    ld = tmp_path / ".lattice"
    ld.mkdir()
    return ld


# ---------------------------------------------------------------------------
# Review state helpers
# ---------------------------------------------------------------------------


class TestReviewState:
    def test_write_read_clear(self, lattice_dir: Path) -> None:
        state = {"task_id": "t1", "mode": "single", "agents": []}
        write_review_state(lattice_dir, state)
        loaded = read_review_state(lattice_dir, "t1")
        assert loaded is not None
        assert loaded["task_id"] == "t1"
        assert loaded["mode"] == "single"

        clear_review_state(lattice_dir, "t1")
        assert read_review_state(lattice_dir, "t1") is None

    def test_read_nonexistent(self, lattice_dir: Path) -> None:
        assert read_review_state(lattice_dir, "nonexistent") is None

    def test_clear_nonexistent(self, lattice_dir: Path) -> None:
        # Should not raise
        clear_review_state(lattice_dir, "nonexistent")

    def test_storage_writer_rejects_paths_outside_lattice_review_state(
        self, tmp_path: Path, lattice_dir: Path
    ) -> None:
        outside = tmp_path / "outside"
        with pytest.raises(ValueError, match=".lattice directory"):
            write_review_state_file(outside, "t1", "{}\n")

        with pytest.raises(ValueError, match="one path component"):
            write_review_state_file(lattice_dir, "../outside", "{}\n")
        assert not (tmp_path / "outside.json").exists()

    def test_concurrent_writes_use_independent_atomic_temps(
        self, lattice_dir: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        barrier = threading.Barrier(2)
        original_replace = os.replace

        def rendezvous_replace(source, target):
            barrier.wait(timeout=5)
            return original_replace(source, target)

        monkeypatch.setattr(os, "replace", rendezvous_replace)
        failures: list[Exception] = []

        def write_state(number: int) -> None:
            try:
                write_review_state(
                    lattice_dir, {"task_id": "same-task", "writer": number, "agents": []}
                )
            except Exception as exc:
                failures.append(exc)

        threads = [threading.Thread(target=write_state, args=(number,)) for number in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=5)

        assert all(not thread.is_alive() for thread in threads)
        assert failures == []
        assert read_review_state(lattice_dir, "same-task")["writer"] in {0, 1}


# ---------------------------------------------------------------------------
# PID liveness check (LAT-211)
# ---------------------------------------------------------------------------


class TestPidAlive:
    def test_self_pid_is_alive(self) -> None:
        assert pid_alive(os.getpid()) is True

    def test_known_dead_pid_is_not_alive(self) -> None:
        # 2**31-1 is far above typical PID range; never live in practice.
        assert pid_alive(2**31 - 1) is False

    def test_zero_pid_is_not_alive(self) -> None:
        assert pid_alive(0) is False

    def test_negative_pid_is_not_alive(self) -> None:
        assert pid_alive(-1) is False


# ---------------------------------------------------------------------------
# claim_review_state (LAT-211)
# ---------------------------------------------------------------------------


class TestClaimReviewState:
    def test_claims_when_no_existing_state(self, lattice_dir: Path) -> None:
        ok, state = claim_review_state(
            lattice_dir,
            "t1",
            mode="single",
            review_type="code-review",
            started_by_pid=os.getpid(),
            auto_fired=False,
        )
        assert ok is True
        assert state is not None
        assert state["task_id"] == "t1"
        assert state["started_by_pid"] == os.getpid()
        assert state["auto_fired"] is False
        # Round-trip through disk.
        loaded = read_review_state(lattice_dir, "t1")
        assert loaded is not None
        assert loaded["started_by_pid"] == os.getpid()
        assert loaded["auto_fired"] is False
        assert loaded["agents"] == []

    def test_refuses_when_live_other_pid_holds(self, lattice_dir: Path) -> None:
        # Seed a record held by a different live pid (parent of test process).
        ppid = os.getppid()
        if ppid == os.getpid() or ppid <= 1:
            pytest.skip("Cannot exercise live-other-pid path: no usable parent pid.")
        write_review_state(
            lattice_dir,
            {
                "task_id": "t1",
                "mode": "single",
                "review_type": "code-review",
                "started_at": "2026-05-06T00:00:00Z",
                "started_by_pid": ppid,
                "auto_fired": False,
                "agents": [],
            },
        )
        ok, existing = claim_review_state(
            lattice_dir,
            "t1",
            mode="single",
            review_type="code-review",
            started_by_pid=os.getpid(),
            auto_fired=False,
        )
        assert ok is False
        assert existing is not None
        assert existing["started_by_pid"] == ppid
        # On-disk record still belongs to the live holder.
        loaded = read_review_state(lattice_dir, "t1")
        assert loaded is not None
        assert loaded["started_by_pid"] == ppid

    def test_reclaims_when_holder_pid_is_dead(self, lattice_dir: Path) -> None:
        write_review_state(
            lattice_dir,
            {
                "task_id": "t1",
                "mode": "single",
                "review_type": "code-review",
                "started_at": "2026-05-06T00:00:00Z",
                "started_by_pid": 2**31 - 1,
                "auto_fired": True,
                "agents": [{"name": "claude", "status": "running"}],
            },
        )
        ok, state = claim_review_state(
            lattice_dir,
            "t1",
            mode="single",
            review_type="code-review",
            started_by_pid=os.getpid(),
            auto_fired=False,
        )
        assert ok is True
        assert state is not None
        assert state["started_by_pid"] == os.getpid()
        assert state["auto_fired"] is False
        # ``agents`` is reset to an empty list — orchestrator fills in.
        assert state["agents"] == []

    def test_reclaims_when_existing_state_has_no_pid(self, lattice_dir: Path) -> None:
        # Legacy/manual state without ``started_by_pid``.
        write_review_state(
            lattice_dir,
            {
                "task_id": "t1",
                "mode": "single",
                "review_type": "code-review",
                "started_at": "2026-05-06T00:00:00Z",
                "agents": [],
            },
        )
        ok, state = claim_review_state(
            lattice_dir,
            "t1",
            mode="single",
            review_type="code-review",
            started_by_pid=os.getpid(),
            auto_fired=False,
        )
        assert ok is True
        assert state is not None
        assert state["started_by_pid"] == os.getpid()

    def test_claim_passes_when_holder_is_self(self, lattice_dir: Path) -> None:
        # Same-PID re-claim is a no-op-ish overwrite (defensive).
        write_review_state(
            lattice_dir,
            {
                "task_id": "t1",
                "mode": "single",
                "review_type": "code-review",
                "started_at": "2026-05-06T00:00:00Z",
                "started_by_pid": os.getpid(),
                "auto_fired": True,
                "agents": [],
            },
        )
        ok, state = claim_review_state(
            lattice_dir,
            "t1",
            mode="single",
            review_type="code-review",
            started_by_pid=os.getpid(),
            auto_fired=True,
        )
        assert ok is True
        assert state is not None
        assert state["started_by_pid"] == os.getpid()
        assert state["auto_fired"] is True


# ---------------------------------------------------------------------------
# Persistent failure tracking
# ---------------------------------------------------------------------------


class TestFailureTracking:
    def test_record_and_count(self, lattice_dir: Path) -> None:
        count = record_agent_failure(lattice_dir, "codex", "task_abc")
        assert count == 1
        count = record_agent_failure(lattice_dir, "codex", "task_def")
        assert count == 2
        assert count_agent_failures(lattice_dir, "codex") == 2
        # Different agent should have 0
        assert count_agent_failures(lattice_dir, "claude") == 0

    def test_count_empty(self, lattice_dir: Path) -> None:
        assert count_agent_failures(lattice_dir, "gemini") == 0

    def test_record_failure_fsyncs_jsonl_append(
        self, lattice_dir: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        fsynced: list[int] = []
        monkeypatch.setattr(review_mod.os, "fsync", fsynced.append)
        record_agent_failure(lattice_dir, "claude", "task-1")
        assert len(fsynced) == 1

    def test_threshold_constant(self) -> None:
        assert FAILURE_THRESHOLD == 2

    def test_failures_persisted_as_jsonl(self, lattice_dir: Path) -> None:
        record_agent_failure(lattice_dir, "claude", "t1")
        record_agent_failure(lattice_dir, "codex", "t2")
        path = lattice_dir / "review_state" / "failures.jsonl"
        assert path.exists()
        lines = path.read_text(encoding="utf-8").strip().splitlines()
        assert len(lines) == 2
        entry = json.loads(lines[0])
        assert entry["agent"] == "claude"
        assert entry["task_id"] == "t1"
        assert "timestamp" in entry

    def test_record_failure_with_detail_persists_diagnostics(self, lattice_dir: Path) -> None:
        record_agent_failure(
            lattice_dir,
            "claude",
            "t1",
            detail={
                "error": "timed out after 600s",
                "review_type": "code-review",
                "returncode": None,
                "duration_seconds": 600.1,
                "command": "env -u CLAUDECODE claude ...",
                "prompt_chars": 21591,
                "stderr_tail": "",
            },
        )
        entry = json.loads((lattice_dir / "review_state" / "failures.jsonl").read_text().strip())
        # Core fields always present and authoritative.
        assert entry["agent"] == "claude"
        assert entry["task_id"] == "t1"
        assert "timestamp" in entry
        # Diagnostic detail carried through.
        assert entry["error"] == "timed out after 600s"
        assert entry["review_type"] == "code-review"
        assert entry["duration_seconds"] == 600.1
        assert entry["command"].startswith("env -u CLAUDECODE")
        assert entry["prompt_chars"] == 21591
        # None/empty detail values are dropped to keep the line compact.
        assert "returncode" not in entry
        assert "stderr_tail" not in entry

    def test_core_fields_win_over_detail(self, lattice_dir: Path) -> None:
        record_agent_failure(
            lattice_dir, "claude", "real", detail={"agent": "spoof", "task_id": "spoof"}
        )
        entry = json.loads((lattice_dir / "review_state" / "failures.jsonl").read_text().strip())
        assert entry["agent"] == "claude"
        assert entry["task_id"] == "real"


class TestDiffCap:
    def test_under_cap_unchanged(self) -> None:
        diff = "\n".join(f"line {i}" for i in range(10))
        capped, was_capped, original = cap_diff(diff, max_lines=100)
        assert capped == diff
        assert was_capped is False
        assert original == 10

    def test_over_cap_truncated_with_marker(self) -> None:
        diff = "\n".join(f"line {i}" for i in range(500))
        capped, was_capped, original = cap_diff(diff, max_lines=100)
        assert was_capped is True
        assert original == 500
        assert "diff truncated by Lattice" in capped
        assert "showing first 100 of 500" in capped
        # Only the first 100 source lines survive (plus the marker block).
        assert "line 99" in capped
        assert "line 100\n" not in capped

    def test_zero_disables_cap(self) -> None:
        diff = "\n".join(f"line {i}" for i in range(500))
        capped, was_capped, original = cap_diff(diff, max_lines=0)
        assert capped == diff
        assert was_capped is False
        assert original == 500

    def test_default_constant_is_generous(self) -> None:
        # A real large change still gets fully reviewed; the cap only guards
        # against pathological diffs.
        assert DEFAULT_MAX_DIFF_LINES >= 2000


class TestEscalationDedup:
    """create_failure_diagnostic_task must not file a duplicate when one is open."""

    def test_skips_when_open_diagnostic_exists(
        self, lattice_dir: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        existing = {
            "data": {
                "tasks": [
                    {"title": "Investigate claude review failures — failed 3 times"},
                ]
            }
        }

        def fake_run(cmd, *args, **kwargs):  # noqa: ANN001
            import subprocess

            # The dedup probe lists the needs-human queue.
            assert cmd[:3] == ["lattice", "list", "--needs-human"]
            return subprocess.CompletedProcess(cmd, 0, json.dumps(existing), "")

        monkeypatch.setattr(review_mod.subprocess, "run", fake_run)
        result = create_failure_diagnostic_task(lattice_dir, "claude", 4, "agent:x")
        assert result is None  # deduped — no new ticket

    def test_creates_when_no_open_diagnostic(
        self, lattice_dir: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        calls: list[list[str]] = []

        def fake_run(cmd, *args, **kwargs):  # noqa: ANN001
            import subprocess

            calls.append(cmd)
            if cmd[:2] == ["lattice", "list"]:
                return subprocess.CompletedProcess(cmd, 0, json.dumps({"data": {"tasks": []}}), "")
            if cmd[:2] == ["lattice", "create"]:
                return subprocess.CompletedProcess(cmd, 0, "LAT-999\n", "")
            return subprocess.CompletedProcess(cmd, 0, "", "")

        monkeypatch.setattr(review_mod.subprocess, "run", fake_run)
        result = create_failure_diagnostic_task(lattice_dir, "claude", 2, "agent:x")
        assert result == "LAT-999"
        # The created title carries the stable dedup-able prefix.
        create_call = next(c for c in calls if c[:2] == ["lattice", "create"])
        assert create_call[2].startswith("Investigate claude review failures")

    def test_created_diagnostic_describes_recent_failures(
        self, lattice_dir: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        calls: list[list[str]] = []

        def fake_run(cmd, *args, **kwargs):  # noqa: ANN001
            calls.append(cmd)
            if cmd[:2] == ["lattice", "list"]:
                return subprocess.CompletedProcess(cmd, 0, json.dumps({"data": {"tasks": []}}), "")
            if cmd[:2] == ["lattice", "create"]:
                return subprocess.CompletedProcess(cmd, 0, "LAT-999\n", "")
            return subprocess.CompletedProcess(cmd, 0, "", "")

        monkeypatch.setattr(review_mod.subprocess, "run", fake_run)
        monkeypatch.setattr(
            review_mod, "_failure_task_title", lambda _lattice_dir, task_id: f"Title for {task_id}"
        )
        record_agent_failure(
            lattice_dir,
            "claude",
            "task-timeout",
            detail={
                "error": "Agent 'claude' timed out after 600s",
                "review_type": "code-review",
                "duration_seconds": 600.1,
                "prompt_chars": 21591,
                "daemon_log_path": "/board/.lattice/.daemon/auto-code-review-task-timeout.log",
            },
        )
        record_agent_failure(
            lattice_dir,
            "codex",
            "other-agent-task",
            detail={"error": "unrelated"},
        )
        record_agent_failure(
            lattice_dir,
            "claude",
            "task-exit",
            detail={
                "error": "Agent 'claude': exited with code 1",
                "review_type": "plan-review",
                "returncode": 1,
                "stderr_tail": "weekly account limit reached",
            },
        )

        result = create_failure_diagnostic_task(lattice_dir, "claude", 2, "agent:x")
        assert result == "LAT-999"
        create_call = next(c for c in calls if c[:2] == ["lattice", "create"])
        description = create_call[create_call.index("--description") + 1]
        assert "task-timeout" in description
        assert "Title for task-timeout" in description
        assert "timed out after 600s" in description
        assert "/board/.lattice/.daemon/auto-code-review-task-timeout.log" in description
        assert "task-exit" in description
        assert "Title for task-exit" in description
        assert "exited with code 1" in description
        assert ".lattice/.daemon/auto-plan-review-task-exit.log" in description
        assert "weekly account limit reached" in description
        assert "other-agent-task" not in description

    def test_manual_failure_does_not_borrow_auto_review_log(self, lattice_dir: Path) -> None:
        from lattice.core.review import _failure_daemon_log

        task_id = "task-with-prior-auto-review"
        events_path = lattice_dir / "events" / f"{task_id}.jsonl"
        events_path.parent.mkdir(parents=True, exist_ok=True)
        events_path.write_text(
            json.dumps(
                {
                    "type": "auto_review_spawned",
                    "data": {
                        "review_type": "code-review",
                        "log_path": ".lattice/.daemon/auto-code-review-old.log",
                        "spawned_at": "2026-10-01T12:00:00Z",
                    },
                }
            )
            + "\n",
            encoding="utf-8",
        )
        # The failure is explicitly manual even though the task has historical
        # auto-review events; do not attach unrelated daemon diagnostics.
        failure = {
            "task_id": task_id,
            "review_type": "code-review",
            "auto_fired": False,
        }
        assert _failure_daemon_log(lattice_dir, failure) == (
            "(not applicable; review was not auto-fired)"
        )


# ---------------------------------------------------------------------------
# Temp file cleanup
# ---------------------------------------------------------------------------


class TestTempCleanup:
    def test_cleanup_removes_matching_files(self) -> None:
        # Create temp files matching the pattern
        f1 = tempfile.NamedTemporaryFile(prefix="lattice-review-", suffix=".md", delete=False)
        f1.close()
        p1 = Path(f1.name)
        assert p1.exists()

        removed = cleanup_temp_files()
        assert removed >= 1
        assert not p1.exists()

    def test_cleanup_with_no_files(self) -> None:
        # Should not raise, returns 0
        removed = cleanup_temp_files()
        assert removed >= 0


# ---------------------------------------------------------------------------
# Actor extraction
# ---------------------------------------------------------------------------


class TestExtractActorStr:
    def test_string_actor(self) -> None:
        assert _extract_actor_str("agent:claude") == "agent:claude"

    def test_dict_actor_with_name(self) -> None:
        assert _extract_actor_str({"name": "agent:opus"}) == "agent:opus"

    def test_dict_actor_with_base_name(self) -> None:
        assert _extract_actor_str({"base_name": "system:bot"}) == "system:bot"

    def test_fallback(self) -> None:
        assert _extract_actor_str(42) == "system:lattice"


# ---------------------------------------------------------------------------
# Config default
# ---------------------------------------------------------------------------


class TestConfigTimeout:
    def test_default_timeout_in_config(self) -> None:
        from lattice.core.config import default_config

        cfg = default_config()
        assert cfg["review_timeout_seconds"] == 600

    def test_default_agent_timeout_constant(self) -> None:
        assert DEFAULT_AGENT_TIMEOUT == 600


# ---------------------------------------------------------------------------
# Single-mode reviews must always be headless (LAT-218)
# ---------------------------------------------------------------------------


class TestSingleReviewBackend:
    def test_single_review_is_always_headless(
        self, lattice_dir: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """``run_single_review`` always passes a ``HeadlessBackend`` to ``spawn_one``.

        Pre-LAT-218 the call site honored ``headless`` / ``backend_force``
        params and could route to the c11 or terminal backend. Post-LAT-218
        the params are gone and every call to ``run_single_review`` is
        guaranteed headless — no surface, no window.
        """
        from lattice.core import review as review_mod
        from lattice.core.agent_spawn import SpawnResult
        from lattice.storage.agent_spawn import HeadlessBackend

        captured: dict = {}

        def _fake_spawn_one(request, **kwargs):
            captured["kwargs"] = kwargs
            return SpawnResult(
                agent=request.agent,
                success=True,
                output_text="ok",
                error="",
                backend="headless",
                duration_seconds=0.0,
            )

        monkeypatch.setattr(review_mod, "spawn_one", _fake_spawn_one)

        success, _msg, _text = review_mod.run_single_review(
            lattice_dir=lattice_dir,
            task_id="t1",
            review_type="code-review",
            prompt_content="noop",
            actor="agent:test",
            timeout=5,
        )

        assert success is True
        assert isinstance(captured["kwargs"].get("backend"), HeadlessBackend)


class TestSingleReviewFailureObservability:
    """LAT-243: a failed single-mode review must leave a durable, observable record
    instead of clearing state and vanishing into "no review found"."""

    @staticmethod
    def _fail_spawn(*, error="produced no output", returncode=1, duration=300.0, stderr="boom"):
        from lattice.core.agent_spawn import SpawnResult

        def _fake(request, **kwargs):
            return SpawnResult(
                agent=request.agent,
                success=False,
                output_text="",
                error=error,
                backend="headless",
                duration_seconds=duration,
                returncode=returncode,
                stderr_tail=stderr,
            )

        return _fake

    def test_failure_leaves_durable_failed_state(self, lattice_dir, monkeypatch):
        from lattice.core import review as review_mod

        monkeypatch.setattr(review_mod, "spawn_one", self._fail_spawn())
        # Isolate state behavior from the failures.jsonl / diagnostic-task plumbing.
        monkeypatch.setattr(review_mod, "_handle_agent_failure", lambda *a, **k: 1)

        success, _msg, text = review_mod.run_single_review(
            lattice_dir=lattice_dir,
            task_id="t1",
            review_type="code-review",
            prompt_content="noop",
            actor="agent:test",
            timeout=5,
        )

        assert success is False
        assert text is None
        state = review_mod.read_review_state(lattice_dir, "t1")
        assert state is not None, "a failed review must NOT clear review_state"
        assert state["status"] == "failed"
        assert state["agents"][0]["status"] == "failed"
        assert "finished_at" in state
        assert state["detail"]["returncode"] == 1

    def test_success_still_clears_state(self, lattice_dir, monkeypatch):
        from lattice.core import review as review_mod
        from lattice.core.agent_spawn import SpawnResult

        def _ok(request, **kwargs):
            return SpawnResult(
                agent=request.agent,
                success=True,
                output_text="LGTM",
                error="",
                backend="headless",
                duration_seconds=1.0,
            )

        monkeypatch.setattr(review_mod, "spawn_one", _ok)

        success, _msg, text = review_mod.run_single_review(
            lattice_dir=lattice_dir,
            task_id="t2",
            review_type="code-review",
            prompt_content="noop",
            actor="agent:test",
            timeout=5,
        )

        assert success is True
        assert text == "LGTM"
        assert review_mod.read_review_state(lattice_dir, "t2") is None

    def test_failed_state_does_not_block_next_claim(self, lattice_dir, monkeypatch):
        from lattice.core import review as review_mod

        monkeypatch.setattr(review_mod, "spawn_one", self._fail_spawn())
        monkeypatch.setattr(review_mod, "_handle_agent_failure", lambda *a, **k: 1)

        review_mod.run_single_review(
            lattice_dir=lattice_dir,
            task_id="t3",
            review_type="code-review",
            prompt_content="noop",
            actor="agent:test",
            timeout=5,
        )
        assert review_mod.read_review_state(lattice_dir, "t3")["status"] == "failed"

        # In production the `lattice code-review` subprocess that wrote this
        # record has exited by now, so its started_by_pid is dead. Simulate that
        # (the test runs in-process, so the writer pid is still us) and confirm
        # the next review can reclaim the slot.
        monkeypatch.setattr(review_mod, "pid_alive", lambda pid: False)
        ok, _existing = review_mod.claim_review_state(
            lattice_dir,
            "t3",
            mode="single",
            review_type="code-review",
            started_by_pid=12_345,
            auto_fired=True,
        )
        assert ok is True

    def test_last_failure_for_task(self, lattice_dir):
        from lattice.core import review as review_mod

        assert review_mod.last_failure_for_task(lattice_dir, "tX") is None
        review_mod.record_agent_failure(
            lattice_dir, "claude", "tX", detail={"error": "first", "returncode": 1}
        )
        review_mod.record_agent_failure(
            lattice_dir, "claude", "tX", detail={"error": "second", "returncode": 2}
        )
        review_mod.record_agent_failure(
            lattice_dir, "claude", "other", detail={"error": "unrelated"}
        )
        latest = review_mod.last_failure_for_task(lattice_dir, "tX")
        assert latest is not None
        assert latest["error"] == "second"  # most recent match wins
        assert latest["task_id"] == "tX"


# ---------------------------------------------------------------------------
# Triple-mode reviews (LAT-218) — fire-and-forget c11 pane spawn
# ---------------------------------------------------------------------------


class TestTripleReviewSpawn:
    def test_outside_c11_returns_clean_error(
        self, lattice_dir: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from lattice.core import review as review_mod

        monkeypatch.setattr("lattice.cli.c11_bridge.c11_available", lambda: False)

        ok, msg = review_mod.run_triple_review(
            lattice_dir=lattice_dir,
            task_id="t1",
            review_type="code-review",
            actor="agent:test",
        )
        assert ok is False
        assert "triple mode requires c11" in msg

    def test_spawns_pane_and_writes_state(
        self, lattice_dir: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        from lattice.core import review as review_mod

        monkeypatch.setattr("lattice.cli.c11_bridge.c11_available", lambda: True)

        captured: dict = {}

        def _fake_spawn(prompt_text, **kwargs):
            captured["prompt"] = prompt_text
            captured["kwargs"] = kwargs
            return True, "surface:42"

        monkeypatch.setattr(
            "lattice.integrations.c11.spawn_one_in_current_workspace",
            _fake_spawn,
        )
        warning = "Configured review_integration_branches entry 'v3' did not resolve."

        ok, msg = review_mod.run_triple_review(
            lattice_dir=lattice_dir,
            task_id="task_01ABC",
            review_type="code-review",
            actor="agent:test",
            short_id="LAT-218",
            base="main",
            worktree=tmp_path,
            warning=warning,
        )
        assert ok is True
        assert "surface:42" in msg
        # Pane prompt contains the trident slash command + routing table.
        assert "/trident-code-review LAT-218" in captured["prompt"]
        assert "pr_open" in captured["prompt"]
        assert "lattice needs-human LAT-218" in captured["prompt"]
        assert f"Lattice-Review-Warning: {warning}" in captured["prompt"]
        # review_state marker landed.
        state = review_mod.read_review_state(lattice_dir, "task_01ABC")
        assert state is not None
        assert state["mode"] == "triple"
        assert state["pane_ref"] == "surface:42"
        assert state["started_by_actor"] == "agent:test"

    def test_fire_and_forget_returns_quickly(
        self, lattice_dir: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import time as _time

        from lattice.core import review as review_mod

        monkeypatch.setattr("lattice.cli.c11_bridge.c11_available", lambda: True)
        monkeypatch.setattr(
            "lattice.integrations.c11.spawn_one_in_current_workspace",
            lambda _p, **_k: (True, "surface:1"),
        )
        start = _time.monotonic()
        review_mod.run_triple_review(
            lattice_dir=lattice_dir,
            task_id="t1",
            review_type="plan-review",
            actor="agent:test",
            short_id="LAT-1",
        )
        elapsed = _time.monotonic() - start
        assert elapsed < 1.0, f"run_triple_review should return immediately, took {elapsed:.3f}s"

    def test_handoff_prompt_includes_routing_table(self) -> None:
        from lattice.core.review import build_trident_handoff_prompt

        prompt = build_trident_handoff_prompt(
            "LAT-42",
            "plan-review",
            worktree=Path("/tmp/wt"),
            base_branch="main",
        )
        assert "/trident-plan-review LAT-42" in prompt
        assert "Review Verdict Routing" in prompt
        assert "lattice artifact show <artifact-id>" in prompt
        assert "lattice show LAT-42" in prompt
        assert ".lattice/artifacts/payload/" not in prompt
        # Routing table outcomes — PASS routes to in_validation (LAT-233);
        # the PR opens only after validation evidence is recorded.
        for outcome in ("in_validation", "in_progress", "in_planning"):
            assert outcome in prompt
        assert "| PASS, fixes done                   | in_validation" in prompt
        # Complex findings route to the needs-human flag, not a status (LAT-232).
        assert "lattice needs-human LAT-42" in prompt
        assert "agent:trident-pane-LAT-42" in prompt

    def test_handoff_prompt_names_the_resolved_range(self) -> None:
        """The pane shares the caller's cwd, whose HEAD is usually not the branch
        under review — so the range has to be stated, not inferred."""
        from lattice.core.review import build_trident_handoff_prompt

        prompt = build_trident_handoff_prompt(
            "LAT-42",
            "code-review",
            worktree=Path("/tmp/board"),
            base_branch="origin/main",
            head_ref="fix/LAT-42-thing",
            head_sha="c" * 40,
        )
        assert "- Base ref: `origin/main`" in prompt
        assert f"- Head ref: `fix/LAT-42-thing ({'c' * 40})`" in prompt
        assert "Diff exactly `origin/main...fix/LAT-42-thing`" in prompt

    def test_code_handoff_carries_base_rule_and_truncation_metadata(self) -> None:
        from lattice.core.review import build_trident_handoff_prompt

        diff_content = "+first line\n[diff truncated by Lattice: showing first 1 of 2 lines]\n"
        prompt = build_trident_handoff_prompt(
            "LAT-42",
            "code-review",
            worktree=Path("/tmp/wt"),
            base_branch="origin/v2",
            base_selection_rule="board_config",
            base_sha="a" * 40,
            head_ref="feat/LAT-42",
            head_sha="b" * 40,
            diff_content=diff_content,
            raw_diff_lines=2,
            raw_diff_chars=len(diff_content),
            truncated=True,
        )

        assert "Lattice-Reviewed-Base-Selection: board_config" in prompt
        assert f"Lattice-Reviewed-Base: origin/v2 ({'a' * 40})" in prompt
        assert (
            f"Lattice-Reviewed-Diff: raw-lines=2, raw-chars={len(diff_content)}, truncated=true"
        ) in prompt
        assert "diff truncated by Lattice" in prompt
        assert "Do not recompute a broader range" in prompt

    def test_handoff_prompt_without_a_head_falls_back_to_head_symbol(self) -> None:
        from lattice.core.review import build_trident_handoff_prompt

        prompt = build_trident_handoff_prompt(
            "LAT-42",
            "plan-review",
            worktree=Path("/tmp/board"),
            base_branch=None,
        )
        assert "- Head ref: `HEAD`" in prompt
        assert "Diff exactly `main...HEAD`" in prompt

    def test_handoff_prompt_uses_program_name_for_artifact_show(self) -> None:
        from lattice.core.review import build_trident_handoff_prompt

        prompt = build_trident_handoff_prompt(
            "LAT-42",
            "code-review",
            worktree=Path("/tmp/board"),
            base_branch="main",
            program="lattice-dev",
        )
        assert "lattice-dev artifact show <artifact-id>" in prompt
        assert "newest attached\nartifact with role `review`" in prompt


# ---------------------------------------------------------------------------
# resolve_diff against a real git worktree (LAT-253 / ACE-317)
#
# These use real git so they bite: they reproduce the worktree-per-ticket model
# where .lattice/ lives in the main checkout (HEAD == main) while the ticket's
# code lives on a feature branch in a *sibling* worktree. A resolution that
# anchors on the ambient HEAD sees an empty diff; a ref-based one sees the real
# branch changes. On pre-fix code, the --base path accepted that empty diff as
# success — a PASS on zero lines.
# ---------------------------------------------------------------------------


def _git(cwd: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args],
        cwd=str(cwd),
        capture_output=True,
        text=True,
        check=True,
    ).stdout


def _non_default_remote_base_repo(tmp_path: Path, feature_branch: str = "feat/LAT-367-review"):
    """A feature cut from origin/v2 while origin/HEAD still names diverged main."""
    repo = tmp_path / "non-default-base"
    repo.mkdir()
    _git(repo, "init", "-b", "main")
    _git(repo, "config", "user.email", "t@t.com")
    _git(repo, "config", "user.name", "Tester")
    (repo / "README.md").write_text("root\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-m", "root")
    root_sha = _git(repo, "rev-parse", "HEAD").strip()

    _git(repo, "checkout", "-b", "v2")
    (repo / "v2.txt").write_text("v2 base\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-m", "v2 integration work")
    v2_sha = _git(repo, "rev-parse", "HEAD").strip()
    _git(repo, "update-ref", "refs/remotes/origin/v2", v2_sha)

    _git(repo, "checkout", "main")
    (repo / "main.txt").write_text("main-only work\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-m", "main integration work")
    main_sha = _git(repo, "rev-parse", "HEAD").strip()
    _git(repo, "update-ref", "refs/remotes/origin/main", main_sha)
    _git(repo, "symbolic-ref", "refs/remotes/origin/HEAD", "refs/remotes/origin/main")

    feature = feature_branch
    _git(repo, "checkout", "-b", feature, "origin/v2")
    (repo / "feature.txt").write_text("ticket change\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-m", "LAT-367: feature change")
    _git(repo, "update-ref", f"refs/remotes/origin/{feature}", "HEAD")
    _git(repo, "branch", "-D", "v2")

    lattice_dir = repo / ".lattice"
    lattice_dir.mkdir()
    return repo, lattice_dir, feature, root_sha, v2_sha, main_sha


def _equal_distance_remote_candidates_repo(tmp_path: Path):
    """Remote integration refs and the default all fork at one base as the head does."""
    repo = tmp_path / "equal-distance"
    repo.mkdir()
    _git(repo, "init", "-b", "main")
    _git(repo, "config", "user.email", "t@t.com")
    _git(repo, "config", "user.name", "Tester")
    (repo / "README.md").write_text("root\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-m", "root")
    root_sha = _git(repo, "rev-parse", "HEAD").strip()

    _git(repo, "checkout", "-b", "v2")
    (repo / "v2.txt").write_text("v2\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-m", "v2 integration")
    _git(repo, "update-ref", "refs/remotes/origin/v2", "HEAD")

    _git(repo, "checkout", "-b", "qa", root_sha)
    (repo / "qa.txt").write_text("qa\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-m", "qa integration")
    _git(repo, "update-ref", "refs/remotes/origin/qa", "HEAD")

    _git(repo, "checkout", "main")
    (repo / "main.txt").write_text("main\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-m", "main integration")
    _git(repo, "update-ref", "refs/remotes/origin/main", "HEAD")
    _git(repo, "symbolic-ref", "refs/remotes/origin/HEAD", "refs/remotes/origin/main")

    feature = "feat/LAT-99-tie"
    _git(repo, "checkout", "-b", feature, root_sha)
    (repo / "feature.txt").write_text("ticket change\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-m", "LAT-99: feature")

    lattice_dir = repo / ".lattice"
    lattice_dir.mkdir()
    return repo, lattice_dir, feature


@pytest.fixture
def simple_worktree_repo(tmp_path: Path):
    """A main checkout (on ``main``) plus a sibling worktree on a feature branch.

    Returns ``(main_checkout, lattice_dir, feature_branch)``. The ticket's change
    lives only on the feature branch — ``main``'s tree does not contain it.
    """
    main = tmp_path / "main"
    main.mkdir()
    _git(main, "init", "-b", "main")
    _git(main, "config", "user.email", "t@t.com")
    _git(main, "config", "user.name", "Tester")
    (main / "file.txt").write_text("base\n")
    _git(main, "add", "-A")
    _git(main, "commit", "-m", "init")
    main_sha = _git(main, "rev-parse", "HEAD").strip()
    _git(main, "update-ref", "refs/remotes/origin/main", main_sha)
    _git(main, "symbolic-ref", "refs/remotes/origin/HEAD", "refs/remotes/origin/main")

    feature = "feat/ACE-317-thing"
    wt = tmp_path / "wt-feature"
    _git(main, "worktree", "add", "-b", feature, str(wt), "main")
    (wt / "file.txt").write_text("base\nticket change\n")
    _git(wt, "add", "-A")
    _git(wt, "commit", "-m", "ACE-317: the ticket change")

    lattice_dir = main / ".lattice"
    lattice_dir.mkdir()
    # Sanity: HEAD-anchored diff from the main checkout sees nothing.
    assert _git(main, "diff", "main...HEAD") == ""
    return main, lattice_dir, feature


class TestResolveDiffWorktree:
    def test_linked_branch_resolves_nonempty_from_main_checkout(self, simple_worktree_repo):
        """The load-bearing case: branch-linked worktree ticket, reviewed from
        the main checkout whose HEAD is main. Must see the real diff."""
        main, lattice_dir, feature = simple_worktree_repo
        snapshot = {"branch_links": [{"branch": feature}], "short_id": "ACE-317"}
        res = review_mod.resolve_diff(lattice_dir, "task_01", snapshot)
        assert res.success is True
        assert "ticket change" in res.diff
        assert res.head_ref == feature
        assert res.source == "linked_branch"

    def test_explicit_base_main_does_not_return_empty(self, simple_worktree_repo):
        """Pre-fix trap: ``--base main`` diffed ``main...HEAD`` (empty) and
        accepted it. Now it must resolve the linked branch's real diff."""
        main, lattice_dir, feature = simple_worktree_repo
        snapshot = {"branch_links": [{"branch": feature}], "short_id": "ACE-317"}
        res = review_mod.resolve_diff(lattice_dir, "task_01", snapshot, base="main")
        assert res.success is True
        assert res.diff.strip() != ""
        assert "ticket change" in res.diff

    def test_no_branch_link_uses_ambient_head_and_says_so(self, simple_worktree_repo):
        """No branch link: fall back to the ambient HEAD and *say so* — never
        scan ``git log --all`` for something that looks like this ticket. Here
        HEAD is main, so the honest answer is an empty-range failure."""
        main, lattice_dir, feature = simple_worktree_repo
        snapshot = {"short_id": "ACE-317"}  # no branch_links
        res = review_mod.resolve_diff(lattice_dir, "task_01", snapshot)
        assert res.source == "head"
        assert res.head_ref == "HEAD"
        assert res.success is False
        assert "empty" in (res.error or "").lower()

    def test_explicit_head_ref(self, simple_worktree_repo):
        """An explicit --head names the branch under review directly."""
        main, lattice_dir, feature = simple_worktree_repo
        res = review_mod.resolve_diff(lattice_dir, "task_01", {}, head=feature)
        assert res.success is True
        assert "ticket change" in res.diff
        assert res.source == "explicit"

    def test_no_changes_errors_never_passes_empty(self, tmp_path):
        """A repo with a branch identical to main resolves to an empty diff and
        must ERROR — never a silent empty success."""
        main = tmp_path / "main"
        main.mkdir()
        _git(main, "init", "-b", "main")
        _git(main, "config", "user.email", "t@t.com")
        _git(main, "config", "user.name", "Tester")
        (main / "f.txt").write_text("x\n")
        _git(main, "add", "-A")
        _git(main, "commit", "-m", "init")
        initial_sha = _git(main, "rev-parse", "HEAD").strip()
        _git(main, "update-ref", "refs/remotes/origin/main", initial_sha)
        _git(main, "symbolic-ref", "refs/remotes/origin/HEAD", "refs/remotes/origin/main")
        _git(main, "branch", "feat/empty")  # identical to main
        lattice_dir = main / ".lattice"
        lattice_dir.mkdir()
        snapshot = {"branch_links": [{"branch": "feat/empty"}], "short_id": "NOPE-1"}
        res = review_mod.resolve_diff(lattice_dir, "task_01", snapshot, base="main")
        assert res.success is False
        assert "empty" in (res.error or "").lower()

    def test_bad_base_ref_named_clearly(self, simple_worktree_repo):
        main, lattice_dir, feature = simple_worktree_repo
        res = review_mod.resolve_diff(lattice_dir, "task_01", {}, base="no-such-ref")
        assert res.success is False
        assert "no-such-ref" in (res.error or "")

    def test_bad_head_ref_named_clearly(self, simple_worktree_repo):
        """An explicit --head that doesn't resolve is named precisely, mirroring
        --base — a typo'd head shouldn't silently fall through to HEAD."""
        main, lattice_dir, feature = simple_worktree_repo
        res = review_mod.resolve_diff(lattice_dir, "task_01", {}, head="no-such-head")
        assert res.success is False
        assert "no-such-head" in (res.error or "")

    def test_non_worktree_head_on_feature_branch(self, tmp_path):
        """Non-worktree case: the feature branch is checked out in the main
        checkout itself (HEAD == feature). Must still resolve."""
        main = tmp_path / "main"
        main.mkdir()
        _git(main, "init", "-b", "main")
        _git(main, "config", "user.email", "t@t.com")
        _git(main, "config", "user.name", "Tester")
        (main / "f.txt").write_text("base\n")
        _git(main, "add", "-A")
        _git(main, "commit", "-m", "init")
        initial_sha = _git(main, "rev-parse", "HEAD").strip()
        _git(main, "update-ref", "refs/remotes/origin/main", initial_sha)
        _git(main, "symbolic-ref", "refs/remotes/origin/HEAD", "refs/remotes/origin/main")
        _git(main, "checkout", "-b", "feat/inline")
        (main / "f.txt").write_text("base\nmore\n")
        _git(main, "add", "-A")
        _git(main, "commit", "-m", "LAT-9: inline change")
        lattice_dir = main / ".lattice"
        lattice_dir.mkdir()
        snapshot = {"branch_links": [{"branch": "feat/inline"}], "short_id": "LAT-9"}
        res = review_mod.resolve_diff(lattice_dir, "task_01", snapshot)
        assert res.success is True
        assert "more" in res.diff

    def test_worktree_param_overrides_root(self, simple_worktree_repo):
        """--worktree points resolution at a specific checkout."""
        main, lattice_dir, feature = simple_worktree_repo
        wt = main.parent / "wt-feature"
        # From the worktree checkout, HEAD is the feature branch, so even the
        # HEAD candidate resolves.
        res = review_mod.resolve_diff(lattice_dir, "task_01", {}, worktree=wt)
        assert res.success is True
        assert "ticket change" in res.diff
        assert res.worktree == wt


class TestReviewBaseSelection:
    def test_infers_nearest_remote_ancestor_instead_of_origin_default(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        repo, lattice_dir, feature, _root_sha, v2_sha, main_sha = _non_default_remote_base_repo(
            tmp_path
        )
        original_run = subprocess.run
        git_commands: list[list[str]] = []

        def recording_run(args, *positional, **kwargs):
            if args and args[0] == "git":
                git_commands.append(list(args[1:]))
            return original_run(args, *positional, **kwargs)

        monkeypatch.setattr(review_mod.subprocess, "run", recording_run)
        res = review_mod.resolve_diff(
            lattice_dir,
            "task_01",
            {"branch_links": [{"branch": feature}]},
            review_integration_branches=["v2"],
        )

        assert res.success is True, res.error
        assert res.base_ref == "origin/v2"
        assert res.base_sha == v2_sha
        assert res.base_selection_rule == "inferred_nearest_merge_base"
        assert main_sha != v2_sha
        assert _git(repo, "rev-parse", f"origin/{feature}").strip() == res.head_sha
        assert "ticket change" in res.diff
        assert "main-only work" not in res.diff
        assert not any(command and command[0] == "fetch" for command in git_commands)
        assert not any(
            command[:2] == ["for-each-ref", "--format=%(refname:short)"]
            and "refs/remotes/" in command
            for command in git_commands
        )

    def test_stacked_sibling_does_not_become_the_base(self, tmp_path: Path) -> None:
        repo, lattice_dir, feature, _root_sha, v2_sha, _main_sha = _non_default_remote_base_repo(
            tmp_path
        )
        _git(repo, "checkout", "-b", "feat/LAT-365-sibling", feature)
        (repo / "sibling.txt").write_text("sibling ticket change\n")
        _git(repo, "add", "sibling.txt")
        _git(repo, "commit", "-m", "LAT-365: sibling change")
        _git(repo, "update-ref", "refs/remotes/origin/feat/LAT-365-sibling", "HEAD")
        _git(repo, "checkout", feature)
        (repo / "fix.txt").write_text("unpublished fix\n")
        _git(repo, "add", "fix.txt")
        _git(repo, "commit", "-m", "LAT-367: local review fix")

        res = review_mod.resolve_diff(
            lattice_dir,
            "task_01",
            {"branch_links": [{"branch": feature}]},
            review_integration_branches=["v2"],
        )

        assert res.success is True, res.error
        assert res.base_ref == "origin/v2"
        assert res.base_sha == v2_sha
        assert "ticket change" in res.diff
        assert "unpublished fix" in res.diff
        assert "sibling ticket change" not in res.diff

    def test_unconfigured_integration_that_merged_feature_is_not_a_candidate(
        self, tmp_path: Path
    ) -> None:
        repo, lattice_dir, feature, _root_sha, v2_sha, _main_sha = _non_default_remote_base_repo(
            tmp_path
        )
        _git(repo, "checkout", "-b", "integration/rc", "origin/v2")
        _git(repo, "merge", "--no-ff", feature, "-m", "merge feature into rc")
        _git(repo, "update-ref", "refs/remotes/origin/integration/rc", "HEAD")
        _git(repo, "checkout", feature)
        (repo / "fix.txt").write_text("unpublished fix\n")
        _git(repo, "add", "fix.txt")
        _git(repo, "commit", "-m", "LAT-367: local review fix")

        res = review_mod.resolve_diff(
            lattice_dir,
            "task_01",
            {"branch_links": [{"branch": feature}]},
            review_integration_branches=["v2"],
        )

        assert res.success is True, res.error
        assert res.base_ref == "origin/v2"
        assert res.base_sha == v2_sha
        assert "ticket change" in res.diff and "unpublished fix" in res.diff
        assert "already merged" not in (res.error or "")

    def test_same_tip_child_is_not_a_candidate(self, tmp_path: Path) -> None:
        repo, lattice_dir, feature, _root_sha, v2_sha, _main_sha = _non_default_remote_base_repo(
            tmp_path
        )
        feature_sha = _git(repo, "rev-parse", feature).strip()
        _git(repo, "update-ref", "refs/remotes/origin/feat/LAT-366-child", feature_sha)
        (repo / "fix.txt").write_text("unpublished fix\n")
        _git(repo, "add", "fix.txt")
        _git(repo, "commit", "-m", "LAT-367: local review fix")

        res = review_mod.resolve_diff(
            lattice_dir,
            "task_01",
            {"branch_links": [{"branch": feature}]},
            review_integration_branches=["v2"],
        )

        assert res.success is True, res.error
        assert res.base_ref == "origin/v2"
        assert res.base_sha == v2_sha
        assert "ticket change" in res.diff and "unpublished fix" in res.diff

    def test_unresolvable_configured_branch_warns_and_refuses_fallback(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _repo, lattice_dir, feature, *_ = _non_default_remote_base_repo(tmp_path)
        monkeypatch.setattr(review_mod, "_open_pr_base_branch", lambda *_args: None)

        res = review_mod.resolve_diff(
            lattice_dir,
            "task_01",
            {"branch_links": [{"branch": feature}]},
            review_integration_branches=["v3"],
        )

        assert res.success is False
        assert res.error_code == "UNRESOLVABLE_REVIEW_INTEGRATION_BRANCHES"
        assert "v3" in (res.warning or "")
        assert "v3" not in (res.error or "")
        assert "refusing to fall back" in (res.error or "")
        assert (res.error or "").endswith(".")
        assert (res.warning or "").count("v3") == 1

    @pytest.mark.parametrize(
        ("remote", "tracking_ref", "expected", "forbidden"),
        [
            (
                False,
                False,
                "Pass --base <ref> or set review_base_branch to a local branch",
                "fetch the intended branch",
            ),
            (
                True,
                False,
                "Fetch the intended branch from a configured remote",
                "Check review_integration_branches and shared history",
            ),
            (
                False,
                True,
                "Check review_integration_branches and shared history",
                "Fetch the intended branch",
            ),
        ],
    )
    def test_unresolvable_integration_remedy_matches_remote_state(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        remote: bool,
        tracking_ref: bool,
        expected: str,
        forbidden: str,
    ) -> None:
        repo = tmp_path / f"unresolvable-{remote}-{tracking_ref}"
        repo.mkdir()
        _git(repo, "init", "-b", "trunk")
        _git(repo, "config", "user.email", "t@t.com")
        _git(repo, "config", "user.name", "Tester")
        (repo / "root.txt").write_text("root\n")
        _git(repo, "add", "-A")
        _git(repo, "commit", "-m", "root")
        if remote:
            _git(repo, "remote", "add", "upstream", "https://example.invalid/repo.git")
        _git(repo, "checkout", "-b", "feat/unresolvable")
        (repo / "change.txt").write_text("change\n")
        _git(repo, "add", "-A")
        _git(repo, "commit", "-m", "feature")
        feature_sha = _git(repo, "rev-parse", "HEAD").strip()
        if tracking_ref:
            _git(repo, "update-ref", "refs/remotes/fork/release/next", feature_sha)
        lattice_dir = repo / ".lattice"
        lattice_dir.mkdir()
        monkeypatch.setattr(review_mod, "_open_pr_base_branch", lambda *_args: None)

        res = review_mod.resolve_diff(
            lattice_dir,
            "task_01",
            {"branch_links": [{"branch": "feat/unresolvable"}]},
            review_integration_branches=["not-fetched"],
        )

        assert res.success is False
        assert res.error_code == "UNRESOLVABLE_REVIEW_INTEGRATION_BRANCHES"
        assert res.configured_remotes_present is remote
        assert res.remote_tracking_refs_present is tracking_ref
        assert expected in (res.error or "")
        assert forbidden.lower() not in (res.error or "").lower()

    def test_missing_configured_entry_warns_while_valid_integration_wins(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _repo, lattice_dir, feature, _root_sha, v2_sha, _main_sha = _non_default_remote_base_repo(
            tmp_path
        )
        monkeypatch.setattr(review_mod, "_open_pr_base_branch", lambda *_args: None)

        res = review_mod.resolve_diff(
            lattice_dir,
            "task_01",
            {"branch_links": [{"branch": feature}]},
            review_integration_branches=["v3", "v2"],
        )

        assert res.success is True, res.error
        assert res.base_ref == "origin/v2"
        assert res.base_sha == v2_sha
        assert "v3" in (res.warning or "")

    def test_stale_remote_warning_names_the_selected_integration_base(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        repo, lattice_dir, feature, _root_sha, _v2_sha, main_sha = _non_default_remote_base_repo(
            tmp_path
        )
        _git(repo, "branch", "v2", main_sha)
        monkeypatch.setattr(review_mod, "_open_pr_base_branch", lambda *_args: None)

        res = review_mod.resolve_diff(
            lattice_dir,
            "task_01",
            {"branch_links": [{"branch": feature}]},
            review_integration_branches=["v2"],
        )

        assert res.success is True, res.error
        assert res.base_ref == "origin/v2"
        assert "Selected review base origin/v2" in (res.warning or "")
        assert "local v2" in (res.warning or "")
        assert "origin/main" not in (res.warning or "")

    @pytest.mark.parametrize(
        "branch",
        [
            "v 2",
            "v2..main",
            "v2~1",
            "v2^",
            "v2:x",
            "v2?x",
            "v2*x",
            "v2[x",
            "v2\\x",
            "@{-1}",
            "-x",
            ".hidden",
            "v2.lock",
            "v2/../main",
            "v2\tmain",
            "v2\x01main",
            " v2 ",
            "v2,branch",
            "é" * 128,
            "x" * 5000,
        ],
    )
    def test_integration_branch_names_follow_git_ref_rules(self, branch: str) -> None:
        branches, error = review_mod._normalize_integration_branches([branch])
        assert branches == []
        assert "review_integration_branches" in (error or "")

    def test_invalid_integration_entry_is_named_with_consistent_guidance(self) -> None:
        branches, error = review_mod._normalize_integration_branches(["v2", "bad\x85name"])
        assert branches == []
        assert "'bad\\x85name'" in (error or "")
        assert "unique, valid Git branch names" in (error or "")

    def test_non_list_integration_config_uses_same_guidance(self) -> None:
        _branches, error = review_mod._normalize_integration_branches("v2")
        assert "unique, valid Git branch names" in (error or "")

    def test_detached_head_uses_only_configured_and_default_candidates(
        self, tmp_path: Path
    ) -> None:
        repo, lattice_dir, feature, _root_sha, v2_sha, _main_sha = _non_default_remote_base_repo(
            tmp_path
        )
        (repo / "fix.txt").write_text("detached local fix\n")
        _git(repo, "add", "fix.txt")
        _git(repo, "commit", "-m", "LAT-367: detached review fix")
        _git(repo, "checkout", "--detach", "HEAD")

        res = review_mod.resolve_diff(
            lattice_dir,
            "task_01",
            {},
            head="HEAD",
            review_integration_branches=["v2"],
        )

        assert res.success is True, res.error
        assert res.base_ref == "origin/v2"
        assert res.base_sha == v2_sha
        assert res.head_ref == "HEAD"
        assert "ticket change" in res.diff and "detached local fix" in res.diff

    def test_lat366_siblings_with_v2_configured_review_the_full_feature(
        self, tmp_path: Path
    ) -> None:
        repo, lattice_dir, feature, _root_sha, v2_sha, main_sha = _non_default_remote_base_repo(
            tmp_path, "feat/LAT-366-issue-media"
        )
        feature_tip = _git(repo, "rev-parse", feature).strip()
        for sibling in (
            "feat/LAT-365-issue-dashboard",
            "feat/LAT-371-issue-title-comments",
        ):
            _git(repo, "update-ref", f"refs/remotes/origin/{sibling}", feature_tip)
        (repo / "fix.txt").write_text("LAT-366 review fix\n")
        _git(repo, "add", "fix.txt")
        _git(repo, "commit", "-m", "LAT-366: follow-up fix")

        res = review_mod.resolve_diff(
            lattice_dir,
            "task_01",
            {"branch_links": [{"branch": feature}]},
            review_integration_branches=["v2"],
        )

        assert res.success is True, res.error
        assert res.base_ref == "origin/v2"
        assert res.base_sha == v2_sha
        assert main_sha != v2_sha
        assert "ticket change" in res.diff and "LAT-366 review fix" in res.diff
        assert "main-only work" not in res.diff

    def test_ties_follow_configured_order_then_remote_default(self, tmp_path: Path) -> None:
        _repo, lattice_dir, feature = _equal_distance_remote_candidates_repo(tmp_path)
        first = review_mod.resolve_diff(
            lattice_dir,
            "task_01",
            {"branch_links": [{"branch": feature}]},
            review_integration_branches=["qa", "v2"],
        )
        second = review_mod.resolve_diff(
            lattice_dir,
            "task_01",
            {"branch_links": [{"branch": feature}]},
            review_integration_branches=["v2", "qa"],
        )
        default = review_mod.resolve_diff(
            lattice_dir,
            "task_01",
            {"branch_links": [{"branch": feature}]},
        )

        assert first.success is True and first.base_ref == "origin/qa"
        assert second.success is True and second.base_ref == "origin/v2"
        assert default.success is True and default.base_ref == "origin/main"

    def test_prefers_integration_branch_advanced_after_feature_fork(self, tmp_path: Path) -> None:
        repo, lattice_dir, feature, _root_sha, v2_sha, main_sha = _non_default_remote_base_repo(
            tmp_path
        )
        _git(repo, "checkout", "-b", "v2", "origin/v2")
        (repo / "later-integration.txt").write_text("landed after feature fork\n")
        _git(repo, "add", "later-integration.txt")
        _git(repo, "commit", "-m", "later v2 change")
        advanced_v2_sha = _git(repo, "rev-parse", "HEAD").strip()
        _git(repo, "update-ref", "refs/remotes/origin/v2", advanced_v2_sha)
        _git(repo, "checkout", feature)

        res = review_mod.resolve_diff(
            lattice_dir,
            "task_01",
            {"branch_links": [{"branch": feature}]},
            review_integration_branches=["v2"],
        )

        assert res.success is True, res.error
        assert res.base_ref == "origin/v2"
        assert res.base_sha == v2_sha
        assert advanced_v2_sha != v2_sha
        assert main_sha != v2_sha
        assert "ticket change" in res.diff
        assert "main-only work" not in res.diff
        assert "landed after feature fork" not in res.diff

    def test_explicit_base_precedes_gh_and_board_config(self, tmp_path: Path, monkeypatch) -> None:
        _repo, lattice_dir, feature, *_ = _non_default_remote_base_repo(tmp_path)
        monkeypatch.setattr(
            review_mod, "_open_pr_base_branch", lambda *_args: pytest.fail("gh must not run")
        )
        res = review_mod.resolve_diff(
            lattice_dir,
            "task_01",
            {"branch_links": [{"branch": feature}]},
            base="origin/main",
            review_base_branch="v2",
            review_integration_branches="not-an-array",
        )
        assert res.success is True, res.error
        assert res.base_ref == "origin/main"
        assert res.base_selection_rule == "explicit"

    def test_explicit_short_name_resolves_remote_ref_without_local_branch(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        _repo, lattice_dir, feature, *_ = _non_default_remote_base_repo(tmp_path)
        monkeypatch.setattr(
            review_mod, "_open_pr_base_branch", lambda *_args: pytest.fail("gh must not run")
        )
        res = review_mod.resolve_diff(
            lattice_dir,
            "task_01",
            {"branch_links": [{"branch": feature}]},
            base="v2",
            review_base_branch="main",
        )
        assert res.success is True, res.error
        assert res.base_ref == "origin/v2"
        assert res.base_selection_rule == "explicit"

    def test_open_pr_base_precedes_board_config(self, tmp_path: Path, monkeypatch) -> None:
        _repo, lattice_dir, feature, *_ = _non_default_remote_base_repo(tmp_path)
        monkeypatch.setattr(review_mod, "_open_pr_base_branch", lambda *_args: "main")
        res = review_mod.resolve_diff(
            lattice_dir,
            "task_01",
            {"branch_links": [{"branch": feature}]},
            review_base_branch="v2",
        )
        assert res.success is True, res.error
        assert res.base_ref == "origin/main"
        assert res.base_selection_rule == "open_pr"

    def test_board_config_base_precedes_inference(self, tmp_path: Path, monkeypatch) -> None:
        _repo, lattice_dir, feature, *_ = _non_default_remote_base_repo(tmp_path)
        monkeypatch.setattr(review_mod, "_open_pr_base_branch", lambda *_args: None)
        res = review_mod.resolve_diff(
            lattice_dir,
            "task_01",
            {"branch_links": [{"branch": feature}]},
            review_base_branch="v2",
        )
        assert res.success is True, res.error
        assert res.base_ref == "origin/v2"
        assert res.base_selection_rule == "board_config"

    def test_board_config_base_accepts_a_commit_sha(self, tmp_path: Path, monkeypatch) -> None:
        _repo, lattice_dir, feature, root_sha, *_ = _non_default_remote_base_repo(tmp_path)
        monkeypatch.setattr(review_mod, "_open_pr_base_branch", lambda *_args: None)

        res = review_mod.resolve_diff(
            lattice_dir,
            "task_01",
            {"branch_links": [{"branch": feature}]},
            review_base_branch=root_sha,
        )

        assert res.success is True, res.error
        assert res.base_ref == root_sha
        assert res.base_selection_rule == "board_config"

    def test_legacy_integration_head_fails_loudly_at_review_time(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _repo, lattice_dir, feature, *_ = _non_default_remote_base_repo(tmp_path)
        monkeypatch.setattr(review_mod, "_open_pr_base_branch", lambda *_args: None)

        res = review_mod.resolve_diff(
            lattice_dir,
            "task_01",
            {"branch_links": [{"branch": feature}]},
            review_integration_branches=["HEAD"],
        )

        assert res.success is False
        assert res.error_code == "INVALID_REVIEW_INTEGRATION_BRANCHES"
        assert "offending entry 'HEAD'" in (res.error or "")
        assert "unique, valid Git branch names" in (res.error or "")

    def test_malformed_local_base_config_fails_without_crashing(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        _repo, lattice_dir, feature, *_ = _non_default_remote_base_repo(tmp_path)
        monkeypatch.setattr(review_mod, "_open_pr_base_branch", lambda *_args: None)
        res = review_mod.resolve_diff(
            lattice_dir,
            "task_01",
            {"branch_links": [{"branch": feature}]},
            review_base_branch=["v2"],
        )
        assert res.success is False
        assert res.error_code == "INVALID_REVIEW_BASE_BRANCH"
        assert "review_base_branch" in (res.error or "")
        assert "['v2']" in (res.error or "")

    @pytest.mark.parametrize("base", ["v2\tmain", "v2\x01main", "v2\x7fmain", "v2\x85main"])
    def test_review_base_config_rejects_raw_control_characters(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, base: str
    ) -> None:
        _repo, lattice_dir, feature, *_ = _non_default_remote_base_repo(tmp_path)
        monkeypatch.setattr(review_mod, "_open_pr_base_branch", lambda *_args: None)

        res = review_mod.resolve_diff(
            lattice_dir,
            "task_01",
            {"branch_links": [{"branch": feature}]},
            review_base_branch=base,
        )

        assert res.success is False
        assert res.error_code == "INVALID_REVIEW_BASE_BRANCH"
        assert repr(base) in (res.error or "")

    def test_no_remote_trunk_error_recommends_local_base(self, tmp_path: Path) -> None:
        repo = tmp_path / "trunk-only"
        repo.mkdir()
        _git(repo, "init", "-b", "trunk")
        _git(repo, "config", "user.email", "t@t.com")
        _git(repo, "config", "user.name", "Tester")
        (repo / "root.txt").write_text("root\n")
        _git(repo, "add", "-A")
        _git(repo, "commit", "-m", "root")
        _git(repo, "checkout", "-b", "feat/trunk-test")
        (repo / "change.txt").write_text("change\n")
        _git(repo, "add", "-A")
        _git(repo, "commit", "-m", "feature")
        lattice_dir = repo / ".lattice"
        lattice_dir.mkdir()

        res = review_mod.resolve_diff(
            lattice_dir,
            "task_01",
            {"branch_links": [{"branch": "feat/trunk-test"}]},
        )

        assert res.success is False
        assert res.error_code == "BASE_INFERENCE_NO_CANDIDATES"
        assert res.configured_remotes_present is False
        assert res.remote_tracking_refs_present is False
        assert "Pass --base <ref>" in (res.error or "")
        assert "review_base_branch" in (res.error or "")
        assert "fetch" not in (res.error or "").lower()
        assert "review_integration_branches" not in (res.error or "")

    def test_configured_unfetched_remote_error_recommends_fetch(self, tmp_path: Path) -> None:
        repo = tmp_path / "unfetched-remote"
        repo.mkdir()
        _git(repo, "init", "-b", "trunk")
        _git(repo, "config", "user.email", "t@t.com")
        _git(repo, "config", "user.name", "Tester")
        (repo / "root.txt").write_text("root\n")
        _git(repo, "add", "-A")
        _git(repo, "commit", "-m", "root")
        _git(repo, "remote", "add", "upstream", "https://example.invalid/repo.git")
        _git(repo, "checkout", "-b", "feat/unfetched")
        (repo / "change.txt").write_text("change\n")
        _git(repo, "add", "-A")
        _git(repo, "commit", "-m", "feature")
        lattice_dir = repo / ".lattice"
        lattice_dir.mkdir()

        res = review_mod.resolve_diff(
            lattice_dir,
            "task_01",
            {"branch_links": [{"branch": "feat/unfetched"}]},
        )

        assert res.success is False
        assert res.error_code == "BASE_INFERENCE_NO_CANDIDATES"
        assert res.configured_remotes_present is True
        assert res.remote_tracking_refs_present is False
        assert "fetch" in (res.error or "").lower()
        assert "review_integration_branches" not in (res.error or "")

    def test_tracking_refs_without_candidate_recommend_integration_config(
        self, tmp_path: Path
    ) -> None:
        repo = tmp_path / "tracking-refs"
        repo.mkdir()
        _git(repo, "init", "-b", "trunk")
        _git(repo, "config", "user.email", "t@t.com")
        _git(repo, "config", "user.name", "Tester")
        (repo / "root.txt").write_text("root\n")
        _git(repo, "add", "-A")
        _git(repo, "commit", "-m", "root")
        _git(repo, "checkout", "-b", "feat/tracking")
        (repo / "change.txt").write_text("change\n")
        _git(repo, "add", "-A")
        _git(repo, "commit", "-m", "feature")
        feature_sha = _git(repo, "rev-parse", "HEAD").strip()
        _git(repo, "update-ref", "refs/remotes/origin/trunk", feature_sha)
        _git(repo, "update-ref", "refs/remotes/fork/release/next", feature_sha)
        # Neither arbitrary ref is a candidate unless named in board config.
        lattice_dir = repo / ".lattice"
        lattice_dir.mkdir()

        res = review_mod.resolve_diff(
            lattice_dir,
            "task_01",
            {"branch_links": [{"branch": "feat/tracking"}]},
        )

        assert res.success is False
        assert res.error_code == "BASE_INFERENCE_NO_CANDIDATES"
        assert res.configured_remotes_present is False
        assert res.remote_tracking_refs_present is True
        assert "review_integration_branches" in (res.error or "")
        assert "shared history" in (res.error or "")

    def test_malformed_local_integration_config_fails_without_crashing(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        _repo, lattice_dir, feature, *_ = _non_default_remote_base_repo(tmp_path)
        monkeypatch.setattr(review_mod, "_open_pr_base_branch", lambda *_args: None)
        res = review_mod.resolve_diff(
            lattice_dir,
            "task_01",
            {"branch_links": [{"branch": feature}]},
            review_integration_branches="v2",
        )
        assert res.success is False
        assert res.error_code == "INVALID_REVIEW_INTEGRATION_BRANCHES"
        assert "review_integration_branches" in (res.error or "")

    def test_duplicate_local_integration_branches_are_rejected(self) -> None:
        branches, error = review_mod._normalize_integration_branches(["v2", "v2"])
        assert branches == []
        assert "unique" in (error or "")

    def test_gh_open_pr_lookup_uses_head_branch_and_requires_open_state(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        repo, lattice_dir, feature, *_ = _non_default_remote_base_repo(tmp_path)
        _git(repo, "remote", "add", "origin", "https://github.com/Stage-11-Agentics/lattice.git")
        monkeypatch.setenv("GH_REPO", "Stage-11-Agentics/lattice")
        monkeypatch.setattr(
            review_mod.shutil, "which", lambda name: "/fake/gh" if name == "gh" else None
        )
        original_run = subprocess.run
        calls: list[list[str]] = []

        def fake_gh(args, *positional, **kwargs):
            if args and args[0] == "/fake/gh":
                calls.append(list(args))
                return subprocess.CompletedProcess(
                    args,
                    0,
                    stdout=json.dumps({"baseRefName": "v2", "state": "OPEN"}),
                    stderr="",
                )
            return original_run(args, *positional, **kwargs)

        monkeypatch.setattr(review_mod.subprocess, "run", fake_gh)
        assert review_mod._open_pr_base_branch(repo, feature) == "v2"
        assert calls == [["/fake/gh", "pr", "view", feature, "--json", "baseRefName,state"]]

        res = review_mod.resolve_diff(
            lattice_dir,
            "task_01",
            {"branch_links": [{"branch": feature}]},
            review_base_branch="main",
        )
        assert res.success is True, res.error
        assert res.base_ref == "origin/v2"
        assert res.base_selection_rule == "open_pr"

        calls.clear()
        monkeypatch.setattr(
            review_mod.subprocess,
            "run",
            lambda args, *positional, **kwargs: (
                subprocess.CompletedProcess(
                    args,
                    0,
                    stdout=json.dumps({"baseRefName": "v2", "state": "CLOSED"}),
                    stderr="",
                )
                if args and args[0] == "/fake/gh"
                else original_run(args, *positional, **kwargs)
            ),
        )
        assert review_mod._open_pr_base_branch(repo, feature) is None


class TestDiffCharCap:
    def test_under_cap_unchanged(self) -> None:
        diff = "\n".join(f"line {i}" for i in range(10))
        capped, was_capped, original = review_mod.cap_diff_chars(diff, max_chars=10_000)
        assert capped == diff
        assert was_capped is False
        assert original == len(diff)

    def test_over_cap_truncates_on_a_line_boundary(self) -> None:
        diff = "\n".join("x" * 100 for _ in range(100))  # ~10k chars, 100 lines
        capped, was_capped, original = review_mod.cap_diff_chars(diff, max_chars=1_000)
        assert was_capped is True
        assert original == len(diff)
        assert "diff truncated by Lattice" in capped
        body = capped.split("\n\n[diff truncated")[0]
        assert len(body) <= 1_000
        # No half-line handed to the reviewer.
        assert all(len(line) == 100 for line in body.splitlines())

    def test_zero_disables_cap(self) -> None:
        diff = "x" * 5_000
        capped, was_capped, _ = review_mod.cap_diff_chars(diff, max_chars=0)
        assert capped == diff
        assert was_capped is False

    def test_line_cap_alone_does_not_bound_a_wide_diff(self) -> None:
        """The reason a character cap exists at all."""
        wide = "\n".join("+" + "x" * 500 for _ in range(5_000))
        line_capped, was_capped, _ = cap_diff(wide, max_lines=5_000)
        assert was_capped is False
        assert len(line_capped) > 2_000_000
        char_capped, was_capped, _ = review_mod.cap_diff_chars(line_capped, max_chars=120_000)
        assert was_capped is True
        assert len(char_capped) < 121_000


class TestAbandonedReviewDetection:
    def test_dead_holder_is_abandoned(self) -> None:
        state = {
            "task_id": "task_01",
            "started_by_pid": 4_000_000,
            "agents": [{"name": "claude", "status": "running"}],
        }
        assert review_mod.is_review_abandoned(state) is True

    def test_live_holder_is_not_abandoned(self) -> None:
        state = {
            "task_id": "task_01",
            "started_by_pid": os.getpid(),
            "agents": [{"name": "claude", "status": "running"}],
        }
        assert review_mod.is_review_abandoned(state) is False

    def test_terminal_status_is_not_abandoned(self) -> None:
        state = {"task_id": "task_01", "started_by_pid": 4_000_000, "status": "failed"}
        assert review_mod.is_review_abandoned(state) is False

    def test_record_without_pid_is_not_abandoned(self) -> None:
        state = {"task_id": "task_01", "agents": [{"name": "claude", "status": "running"}]}
        assert review_mod.is_review_abandoned(state) is False
