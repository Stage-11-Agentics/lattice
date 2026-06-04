"""Tests for core review logic: failure tracking, temp cleanup, state helpers."""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path

import pytest

from lattice.core.review import (
    DEFAULT_AGENT_TIMEOUT,
    FAILURE_THRESHOLD,
    _extract_actor_str,
    pid_alive,
    claim_review_state,
    cleanup_temp_files,
    clear_review_state,
    count_agent_failures,
    read_review_state,
    record_agent_failure,
    write_review_state,
)


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

        ok, msg = review_mod.run_triple_review(
            lattice_dir=lattice_dir,
            task_id="task_01ABC",
            review_type="code-review",
            actor="agent:test",
            short_id="LAT-218",
            base="main",
            worktree=tmp_path,
        )
        assert ok is True
        assert "surface:42" in msg
        # Pane prompt contains the trident slash command + routing table.
        assert "/trident-code-review LAT-218" in captured["prompt"]
        assert "pr_open" in captured["prompt"]
        assert "lattice needs-human LAT-218" in captured["prompt"]
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
        # Routing table outcomes
        for outcome in ("pr_open", "in_progress", "in_planning"):
            assert outcome in prompt
        # Complex findings route to the needs-human flag, not a status.
        assert "lattice needs-human LAT-42" in prompt
        assert "agent:trident-pane-LAT-42" in prompt
