"""Tests for core review logic: failure tracking, temp cleanup, state helpers."""

from __future__ import annotations

import json
import os
import subprocess
import tempfile
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
        # Routing table outcomes — PASS routes to in_validation (LAT-233);
        # the PR opens only after validation evidence is recorded.
        for outcome in ("in_validation", "in_progress", "in_planning"):
            assert outcome in prompt
        assert "| PASS, fixes done                   | in_validation" in prompt
        # Complex findings route to the needs-human flag, not a status (LAT-232).
        assert "lattice needs-human LAT-42" in prompt
        assert "agent:trident-pane-LAT-42" in prompt


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


@pytest.fixture
def worktree_repo(tmp_path: Path):
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
    def test_linked_branch_resolves_nonempty_from_main_checkout(self, worktree_repo):
        """The load-bearing case: branch-linked worktree ticket, reviewed from
        the main checkout whose HEAD is main. Must see the real diff."""
        main, lattice_dir, feature = worktree_repo
        snapshot = {"branch_links": [{"branch": feature}], "short_id": "ACE-317"}
        success, diff = review_mod.resolve_diff(lattice_dir, "task_01", snapshot)
        assert success is True
        assert "ticket change" in diff

    def test_explicit_base_main_does_not_return_empty(self, worktree_repo):
        """Pre-fix trap: ``--base main`` diffed ``main...HEAD`` (empty) and
        accepted it. Now it must resolve the linked branch's real diff."""
        main, lattice_dir, feature = worktree_repo
        snapshot = {"branch_links": [{"branch": feature}], "short_id": "ACE-317"}
        success, diff = review_mod.resolve_diff(lattice_dir, "task_01", snapshot, base="main")
        assert success is True
        assert diff.strip() != ""
        assert "ticket change" in diff

    def test_no_branch_link_resolves_via_all_history(self, worktree_repo):
        """No branch-link: the short-id history scan must find the commit on the
        unmerged feature branch via ``git log --all`` (not reachable from HEAD)."""
        main, lattice_dir, feature = worktree_repo
        snapshot = {"short_id": "ACE-317"}  # no branch_links
        success, diff = review_mod.resolve_diff(lattice_dir, "task_01", snapshot)
        assert success is True
        assert "ticket change" in diff

    def test_explicit_head_ref(self, worktree_repo):
        """An explicit --head names the branch under review directly."""
        main, lattice_dir, feature = worktree_repo
        success, diff = review_mod.resolve_diff(lattice_dir, "task_01", {}, head=feature)
        assert success is True
        assert "ticket change" in diff

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
        _git(main, "branch", "feat/empty")  # identical to main
        lattice_dir = main / ".lattice"
        lattice_dir.mkdir()
        snapshot = {"branch_links": [{"branch": "feat/empty"}], "short_id": "NOPE-1"}
        success, msg = review_mod.resolve_diff(lattice_dir, "task_01", snapshot)
        assert success is False
        assert "empty" in msg.lower()

    def test_bad_base_ref_named_clearly(self, worktree_repo):
        main, lattice_dir, feature = worktree_repo
        success, msg = review_mod.resolve_diff(lattice_dir, "task_01", {}, base="no-such-ref")
        assert success is False
        assert "no-such-ref" in msg

    def test_bad_head_ref_named_clearly(self, worktree_repo):
        """An explicit --head that doesn't resolve is named precisely, mirroring
        --base — a typo'd head shouldn't silently fall through to HEAD."""
        main, lattice_dir, feature = worktree_repo
        success, msg = review_mod.resolve_diff(lattice_dir, "task_01", {}, head="no-such-head")
        assert success is False
        assert "no-such-head" in msg

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
        _git(main, "checkout", "-b", "feat/inline")
        (main / "f.txt").write_text("base\nmore\n")
        _git(main, "add", "-A")
        _git(main, "commit", "-m", "LAT-9: inline change")
        lattice_dir = main / ".lattice"
        lattice_dir.mkdir()
        snapshot = {"branch_links": [{"branch": "feat/inline"}], "short_id": "LAT-9"}
        success, diff = review_mod.resolve_diff(lattice_dir, "task_01", snapshot)
        assert success is True
        assert "more" in diff

    def test_worktree_param_overrides_root(self, worktree_repo):
        """--worktree points resolution at a specific checkout."""
        main, lattice_dir, feature = worktree_repo
        wt = main.parent / "wt-feature"
        # From the worktree checkout, HEAD is the feature branch, so even the
        # HEAD candidate resolves.
        success, diff = review_mod.resolve_diff(lattice_dir, "task_01", {}, worktree=wt)
        assert success is True
        assert "ticket change" in diff
