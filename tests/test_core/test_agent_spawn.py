"""Unit tests for the agent_spawn primitive: contract, selector, polling."""

from __future__ import annotations

import time
from collections.abc import Sequence
from pathlib import Path

import pytest

from lattice.core.agent_spawn import (
    Backend,
    BackendUnavailableError,
    ProgressCallback,
    SpawnRequest,
    SpawnResult,
    _agent_cli_command,
    poll_sentinels,
    select_backend,
    sentinel_path,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Remove every env var the selector reads so tests start from a known floor."""
    for name in (
        "C11_SOCKET_PATH",
        "C11_WORKSPACE_ID",
        "C11_SURFACE_ID",
        "CMUX_SOCKET_PATH",
        "CMUX_WORKSPACE_ID",
        "CMUX_SURFACE_ID",
        "LATTICE_SPAWN_BACKEND",
        "CI",
        "CLAUDECODE",
    ):
        monkeypatch.delenv(name, raising=False)


def _make_request(tmp_path: Path, agent: str = "claude", timeout: int = 5) -> SpawnRequest:
    out = tmp_path / agent / "output.md"
    out.parent.mkdir(parents=True, exist_ok=True)
    prompt = tmp_path / agent / "prompt.md"
    prompt.write_text("noop", encoding="utf-8")
    return SpawnRequest(
        agent=agent,
        prompt_file=prompt,
        output_file=out,
        label=f"test :: {agent}",
        timeout_seconds=timeout,
    )


# ---------------------------------------------------------------------------
# _agent_cli_command
# ---------------------------------------------------------------------------


class TestAgentCliCommand:
    def test_claude_command(self) -> None:
        cmd = _agent_cli_command("claude", "/p", "/o")
        assert cmd is not None
        assert "claude" in cmd
        assert "--dangerously-skip-permissions" in cmd
        assert "/p" in cmd and "/o" in cmd

    def test_codex_command(self) -> None:
        cmd = _agent_cli_command("codex", "/p", "/o")
        assert cmd is not None
        assert "codex exec" in cmd
        assert "--full-auto" in cmd

    def test_gemini_command(self) -> None:
        cmd = _agent_cli_command("gemini", "/p", "/o")
        assert cmd is not None
        assert "gemini" in cmd
        assert "--yolo" in cmd

    def test_unknown_returns_none(self) -> None:
        assert _agent_cli_command("mystery", "/p", "/o") is None


# ---------------------------------------------------------------------------
# select_backend()
# ---------------------------------------------------------------------------


class TestSelectBackend:
    def test_ci_forces_headless(self, clean_env: None, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("CI", "1")
        backend = select_backend()
        assert backend.name == "headless"

    def test_ci_true_string(self, clean_env: None, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("CI", "true")
        backend = select_backend()
        assert backend.name == "headless"

    def test_explicit_headless_kwarg_overrides_everything(
        self, clean_env: None, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("C11_SOCKET_PATH", "/tmp/c11.sock")
        backend = select_backend(headless=True)
        assert backend.name == "headless"

    def test_lattice_spawn_backend_headless(
        self, clean_env: None, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("LATTICE_SPAWN_BACKEND", "headless")
        backend = select_backend()
        assert backend.name == "headless"

    def test_invalid_lattice_spawn_backend_raises(
        self, clean_env: None, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("LATTICE_SPAWN_BACKEND", "weird")
        with pytest.raises(BackendUnavailableError):
            select_backend()

    def test_force_c11_unavailable_raises(
        self, clean_env: None, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # C11_SOCKET_PATH unset → c11 unavailable → forced should raise.
        with pytest.raises(BackendUnavailableError):
            select_backend(force="c11")

    def test_force_terminal_unavailable_raises(
        self, clean_env: None, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr("shutil.which", lambda _name: None)
        with pytest.raises(BackendUnavailableError):
            select_backend(force="terminal")

    def test_no_tty_no_c11_falls_back_to_headless(
        self,
        clean_env: None,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # No C11_SOCKET_PATH, no TTY → headless.
        monkeypatch.setattr("sys.stdout.isatty", lambda: False)
        backend = select_backend()
        assert backend.name == "headless"

    def test_progress_fires_for_backend_selected(
        self, clean_env: None, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        events: list[tuple[str, str]] = []
        monkeypatch.setenv("CI", "1")
        select_backend(on_progress=lambda kind, payload: events.append((kind, payload)))
        assert any(kind == "backend_selected" for kind, _ in events)


# ---------------------------------------------------------------------------
# poll_sentinels()
# ---------------------------------------------------------------------------


class TestPollSentinels:
    def test_polling_terminates_on_subset_arrival(self, tmp_path: Path) -> None:
        """The loop must wait for the slow request's deadline without busy-spinning."""
        fast_a = _make_request(tmp_path, "claude", timeout=2)
        fast_b = _make_request(tmp_path, "codex", timeout=2)
        slow = _make_request(tmp_path, "gemini", timeout=1)

        # Land sentinels for the two fast ones immediately.
        for req in (fast_a, fast_b):
            req.output_file.write_text("fast result", encoding="utf-8")
            sentinel_path(req.output_file).touch()

        started = {
            "claude": time.monotonic(),
            "codex": time.monotonic(),
            "gemini": time.monotonic(),
        }
        results = poll_sentinels(
            [fast_a, fast_b, slow],
            backend_name="test",
            started_at=started,
            poll_interval=0.05,
        )
        by_agent = {r.agent: r for r in results}
        assert by_agent["claude"].success is True
        assert by_agent["codex"].success is True
        assert by_agent["gemini"].success is False
        assert "timed out" in by_agent["gemini"].error

    def test_late_sentinel_after_timeout_is_ignored(self, tmp_path: Path) -> None:
        """Once a request times out, a sentinel that arrives later is not re-read."""
        req = _make_request(tmp_path, "claude", timeout=1)
        started = {"claude": time.monotonic()}

        # Run polling with no sentinel — it will time out.
        results = poll_sentinels(
            [req],
            backend_name="test",
            started_at=started,
            poll_interval=0.05,
        )
        assert len(results) == 1
        assert results[0].success is False
        assert "timed out" in results[0].error

        # Late write should NOT mutate the already-recorded result.
        req.output_file.write_text("late result", encoding="utf-8")
        sentinel_path(req.output_file).touch()
        # SpawnResult is frozen — the original result stays as-is.
        assert results[0].success is False

    def test_empty_input_returns_empty(self) -> None:
        assert poll_sentinels([], backend_name="x", started_at={}) == []

    def test_err_sidecar_marks_failure(self, tmp_path: Path) -> None:
        req = _make_request(tmp_path, "claude", timeout=2)
        # Write output AND an .err sidecar — backend writes both when the runner errored.
        req.output_file.write_text("partial output", encoding="utf-8")
        err_path = req.output_file.with_suffix(req.output_file.suffix + ".err")
        err_path.write_text("agent crashed", encoding="utf-8")
        sentinel_path(req.output_file).touch()

        started = {"claude": time.monotonic()}
        results = poll_sentinels(
            [req],
            backend_name="test",
            started_at=started,
            poll_interval=0.05,
        )
        assert results[0].success is False
        assert "agent crashed" in results[0].error

    def test_empty_output_marks_failure(self, tmp_path: Path) -> None:
        req = _make_request(tmp_path, "claude", timeout=2)
        # Sentinel landed but output file is empty.
        sentinel_path(req.output_file).touch()
        # Don't create output file at all.
        started = {"claude": time.monotonic()}
        results = poll_sentinels(
            [req],
            backend_name="test",
            started_at=started,
            poll_interval=0.05,
        )
        assert results[0].success is False
        assert "no output" in results[0].error


# ---------------------------------------------------------------------------
# Spawn result shape stability
# ---------------------------------------------------------------------------


class _FakeBackend(Backend):
    """Records calls; returns canned results."""

    name = "fake"

    def __init__(self, results: list[SpawnResult]) -> None:
        self._results = results
        self.calls: list[tuple[Sequence[SpawnRequest], str]] = []

    def run(
        self,
        requests: Sequence[SpawnRequest],
        *,
        workspace_label: str,
        on_progress: ProgressCallback | None = None,
    ) -> list[SpawnResult]:
        self.calls.append((list(requests), workspace_label))
        return list(self._results)


class TestSpawnEntrypoints:
    def test_spawn_one_unwraps_first_result(self, tmp_path: Path) -> None:
        from lattice.core.agent_spawn import spawn_one

        req = _make_request(tmp_path, "claude")
        backend = _FakeBackend(
            [
                SpawnResult(
                    agent="claude",
                    success=True,
                    output_text="hi",
                    error="",
                    backend="fake",
                    duration_seconds=0.1,
                )
            ]
        )
        result = spawn_one(req, workspace_label="test", backend=backend)
        assert result.success is True
        assert result.output_text == "hi"
        assert backend.calls[0][1] == "test"

    def test_spawn_many_returns_all_results(self, tmp_path: Path) -> None:
        from lattice.core.agent_spawn import spawn_many

        reqs = [_make_request(tmp_path, a) for a in ("claude", "codex")]
        canned = [
            SpawnResult(
                agent=a,
                success=True,
                output_text=f"{a} ok",
                error="",
                backend="fake",
                duration_seconds=0.1,
            )
            for a in ("claude", "codex")
        ]
        backend = _FakeBackend(canned)
        results = spawn_many(reqs, workspace_label="test", backend=backend)
        assert [r.agent for r in results] == ["claude", "codex"]

    def test_spawn_many_empty(self, tmp_path: Path) -> None:
        from lattice.core.agent_spawn import spawn_many

        backend = _FakeBackend([])
        assert spawn_many([], workspace_label="t", backend=backend) == []
