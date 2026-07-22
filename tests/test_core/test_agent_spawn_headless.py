"""Integration tests for HeadlessBackend using a fake_agent fixture."""

from __future__ import annotations

import os
import shlex
import sys
import time
from pathlib import Path

import pytest

from lattice.core.agent_spawn import (
    HOST_SESSION_ENV_VARS,
    SpawnRequest,
    sentinel_path,
    spawn_many,
    spawn_one,
)
from lattice.core.auto_review import classify_transient_review_failure
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

    def test_timeout_records_command_for_diagnostics(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A timed-out spawn carries the resolved command on the result.

        This is the diagnostic the failure record needs — the timeout path
        previously discarded everything but a bare 'timed out' string.
        """
        _patch_command(monkeypatch, behavior="sleep:5")
        req = _make_request(tmp_path, "claude", timeout=1)
        result = spawn_one(req, workspace_label="test", backend=HeadlessBackend())
        assert not result.success
        assert result.command and "fake_agent" in result.command

    def test_failure_records_returncode_and_stderr_tail(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _patch_command(monkeypatch, behavior="fail")
        req = _make_request(tmp_path, "claude")
        result = spawn_one(req, workspace_label="test", backend=HeadlessBackend())
        assert not result.success
        assert result.returncode == 1
        assert "simulated failure" in result.stderr_tail

    def test_stdout_only_failure_tail_reaches_transient_classifier(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _patch_command(monkeypatch, behavior="stdout-limit")
        req = _make_request(tmp_path, "claude")
        result = spawn_one(req, workspace_label="test", backend=HeadlessBackend())
        assert not result.success
        assert "session limit" in result.stderr_tail
        assert (
            classify_transient_review_failure(result.error, result.stderr_tail) == "session_limit"
        )

    def test_timeout_kills_whole_process_group(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """On timeout, a child spawned by the agent is killed too (no orphan).

        Reproduces the real shape: the shell wrapper backgrounds a child (like
        ``claude`` under ``env``) that would otherwise survive a kill aimed
        only at the shell. With start_new_session + killpg the whole group dies.
        """
        pidfile = tmp_path / "child.pid"

        def _stub(agent_type: str, prompt_file: str, output_file: str) -> str:
            child = (
                f"python3 -c 'import os,time;"
                f'open("{pidfile}","w").write(str(os.getpid()));'
                f"time.sleep(30)' &"
            )
            return f"{child} python3 -c 'import time; time.sleep(30)'"

        monkeypatch.setattr("lattice.core.agent_spawn._agent_cli_command", _stub)
        monkeypatch.setattr("lattice.storage.agent_spawn._agent_cli_command", _stub)

        req = _make_request(tmp_path, "claude", timeout=2)
        result = spawn_one(req, workspace_label="test", backend=HeadlessBackend())
        assert not result.success
        assert "timed out" in result.error

        assert pidfile.exists(), "child never started"
        child_pid = int(pidfile.read_text().strip())
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline and _pid_alive(child_pid):
            time.sleep(0.1)
        assert not _pid_alive(child_pid), f"orphan child {child_pid} survived the timeout"


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


class TestHeadlessBackendScrubsHostSessionEnv:
    """Regression (LAT-255): a headless agent spawned from inside a c11 surface
    must NOT inherit that surface's identity. If it did, c11's session-start
    injection would make the headless reviewer rename the operator's own tab.
    """

    def _dump_env_command(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Patch the CLI builder to a stub that dumps the CHILD's view of every
        host-session var into the output file (``VAR=[<value>]`` per line)."""

        def _stub(agent_type: str, prompt_file: str, output_file: str) -> str:
            body = "".join(f'echo "{v}=[${v}]"; ' for v in HOST_SESSION_ENV_VARS)
            return f"sh -c {shlex.quote(body)} > {shlex.quote(output_file)}"

        monkeypatch.setattr("lattice.core.agent_spawn._agent_cli_command", _stub)
        monkeypatch.setattr("lattice.storage.agent_spawn._agent_cli_command", _stub)

    def test_child_does_not_inherit_host_session_vars(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Pollute the parent env as if we were firing from inside a c11 surface.
        for var in HOST_SESSION_ENV_VARS:
            monkeypatch.setenv(var, f"parent-{var}")
        self._dump_env_command(monkeypatch)

        req = _make_request(tmp_path, "claude")
        result = spawn_one(req, workspace_label="test", backend=HeadlessBackend())

        assert result.success, result.error
        dumped = req.output_file.read_text(encoding="utf-8")
        # Every host-session var must read empty in the child. Pre-fix (only
        # CLAUDECODE popped) the C11_*/CMUX_* lines would show ``parent-<VAR>``.
        for var in HOST_SESSION_ENV_VARS:
            assert f"{var}=[]" in dumped, f"child inherited {var}: {dumped!r}"
            assert f"parent-{var}" not in dumped, f"{var} leaked into child: {dumped!r}"
