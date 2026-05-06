"""Integration tests for HeadlessBackend using a fake_agent fixture."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from lattice.core.agent_spawn import (
    SpawnRequest,
    sentinel_path,
    spawn_many,
    spawn_one,
)
from lattice.storage.agent_spawn import HeadlessBackend


FAKE_AGENT_PATH = Path(__file__).resolve().parent.parent / "fixtures" / "fake_agent.py"


def _patch_command(monkeypatch: pytest.MonkeyPatch, behavior: str | None = None) -> None:
    """Override _agent_cli_command to launch the fake agent for every type."""
    fake_cmd_template = f"{sys.executable} {FAKE_AGENT_PATH}"
    if behavior:
        fake_cmd_template = f"LATTICE_FAKE_BEHAVIOR={behavior} {fake_cmd_template}"

    def _stub(agent_type: str, prompt_file: str, output_file: str) -> str:
        # Mimic the production env-var surface so the fake agent sees the
        # same vars the real wrapper would set.
        return (
            f"LATTICE_AGENT_PROMPT={prompt_file} "
            f"LATTICE_AGENT_OUTPUT={output_file} "
            f"LATTICE_AGENT_TYPE={agent_type} "
            f"{fake_cmd_template}"
        )

    monkeypatch.setattr("lattice.core.agent_spawn._agent_cli_command", _stub)
    monkeypatch.setattr("lattice.storage.agent_spawn._agent_cli_command", _stub)


def _make_request(tmp_path: Path, agent: str, *, timeout: int = 10) -> SpawnRequest:
    sub = tmp_path / agent
    sub.mkdir()
    prompt = sub / "prompt.md"
    prompt.write_text("hello agent", encoding="utf-8")
    output = sub / "output.md"
    return SpawnRequest(
        agent=agent,
        prompt_file=prompt,
        output_file=output,
        label=f"test :: {agent}",
        timeout_seconds=timeout,
    )


class TestHeadlessBackendEndToEnd:
    def test_fake_agent_end_to_end(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """spawn_one against fake_agent writes output + sentinel + success."""
        _patch_command(monkeypatch, behavior="ok")
        req = _make_request(tmp_path, "claude")
        result = spawn_one(req, workspace_label="test", backend=HeadlessBackend())
        assert result.success, result.error
        assert "FAKE-AGENT-OUTPUT" in result.output_text
        assert sentinel_path(req.output_file).exists()

    def test_fake_agent_stdout_fallback(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """If the agent prints to stdout instead of the file, it's captured."""
        _patch_command(monkeypatch, behavior="stdout")
        req = _make_request(tmp_path, "claude")
        result = spawn_one(req, workspace_label="test", backend=HeadlessBackend())
        assert result.success, result.error
        assert "FAKE-AGENT-STDOUT-OUTPUT" in result.output_text
        # Wrapper writes it to the file too, for sentinel-contract uniformity.
        assert "FAKE-AGENT-STDOUT-OUTPUT" in req.output_file.read_text(encoding="utf-8")
        assert sentinel_path(req.output_file).exists()

    def test_fake_agent_failure_sets_error_and_sentinel(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _patch_command(monkeypatch, behavior="fail")
        req = _make_request(tmp_path, "claude")
        result = spawn_one(req, workspace_label="test", backend=HeadlessBackend())
        assert not result.success
        assert "exited with code" in result.error or "exit" in result.error
        assert sentinel_path(req.output_file).exists()

    def test_fake_agent_timeout(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        _patch_command(monkeypatch, behavior="sleep:5")
        req = _make_request(tmp_path, "claude", timeout=1)
        result = spawn_one(req, workspace_label="test", backend=HeadlessBackend())
        assert not result.success
        assert "timed out" in result.error
        assert sentinel_path(req.output_file).exists()

    def test_spawn_many_concurrent_fake_agents(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _patch_command(monkeypatch, behavior="ok")
        reqs = [_make_request(tmp_path, a) for a in ("claude", "codex", "gemini")]
        results = spawn_many(reqs, workspace_label="test", backend=HeadlessBackend())
        assert len(results) == 3
        assert all(r.success for r in results), [r.error for r in results]
        for req in reqs:
            assert sentinel_path(req.output_file).exists()
