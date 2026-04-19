"""End-to-end tests for the agent_runner wrapper script."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest


FAKE_AGENT_PATH = Path(__file__).resolve().parent / "fixtures" / "fake_agent.py"


def _run_wrapper(env: dict[str, str], *, mode: str = "agent") -> subprocess.CompletedProcess:
    full_env = os.environ.copy()
    full_env.pop("CLAUDECODE", None)
    full_env.update(env)
    return subprocess.run(
        [sys.executable, "-m", "lattice.agent_runner", "--mode", mode],
        env=full_env,
        capture_output=True,
        text=True,
        timeout=30,
    )


def test_agent_mode_writes_sentinel_and_output(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Wrapper drives a fake agent and writes both output and sentinel."""
    out = tmp_path / "output.md"
    prompt = tmp_path / "prompt.md"
    prompt.write_text("hello", encoding="utf-8")

    # Patch the command builder via a tiny shim — easier than monkeypatching
    # the wrapper subprocess: stub _agent_cli_command at the module level
    # using a sitecustomize-like injection isn't practical here, so we instead
    # use the LATTICE_AGENT_TYPE=claude path with a fake agent invocation by
    # monkeypatching the resolved command via the runner script's importable
    # module. Simpler path: set up env so the production claude command runs
    # the fake_agent script.
    fake_cmd = (
        f"LATTICE_AGENT_PROMPT={prompt} LATTICE_AGENT_OUTPUT={out} "
        f"{sys.executable} {FAKE_AGENT_PATH}"
    )

    env = {
        "LATTICE_AGENT_TYPE": "claude",
        "LATTICE_AGENT_PROMPT": str(prompt),
        "LATTICE_AGENT_OUTPUT": str(out),
        "LATTICE_AGENT_TIMEOUT": "10",
        "LATTICE_AGENT_LABEL": "test :: claude",
        # The wrapper builds its own command from _agent_cli_command. We want
        # to bypass the real claude binary, so we temporarily monkeypatch via
        # a sitecustomize file written into PYTHONSTARTUP-equivalent path.
    }
    # The cleaner path: use Python -c to invoke the runner with patched module.
    runner_inline = (
        "import sys, lattice.core.agent_spawn as a, lattice.storage.agent_spawn as s; "
        f"cmd=lambda agent, p, o: 'LATTICE_AGENT_PROMPT='+p+' LATTICE_AGENT_OUTPUT='+o+' "
        f"{sys.executable} {FAKE_AGENT_PATH}'; "
        "a._agent_cli_command=cmd; s._agent_cli_command=cmd; "
        "from lattice.agent_runner import main; sys.exit(main(['--mode','agent']))"
    )
    full_env = os.environ.copy()
    full_env.pop("CLAUDECODE", None)
    full_env.update(env)
    proc = subprocess.run(
        [sys.executable, "-c", runner_inline],
        env=full_env,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert proc.returncode == 0, f"stderr={proc.stderr!r} stdout={proc.stdout!r}"
    assert out.exists()
    assert "FAKE-AGENT-OUTPUT" in out.read_text(encoding="utf-8")
    assert (out.parent / "output.md.done").exists()


def test_agent_mode_writes_err_on_failure(
    tmp_path: Path,
) -> None:
    out = tmp_path / "output.md"
    prompt = tmp_path / "prompt.md"
    prompt.write_text("hello", encoding="utf-8")

    runner_inline = (
        "import sys, lattice.core.agent_spawn as a, lattice.storage.agent_spawn as s; "
        f"cmd=lambda agent, p, o: 'LATTICE_FAKE_BEHAVIOR=fail LATTICE_AGENT_PROMPT='+p+' "
        f"LATTICE_AGENT_OUTPUT='+o+' {sys.executable} {FAKE_AGENT_PATH}'; "
        "a._agent_cli_command=cmd; s._agent_cli_command=cmd; "
        "from lattice.agent_runner import main; sys.exit(main(['--mode','agent']))"
    )
    env = os.environ.copy()
    env.update({
        "LATTICE_AGENT_TYPE": "claude",
        "LATTICE_AGENT_PROMPT": str(prompt),
        "LATTICE_AGENT_OUTPUT": str(out),
        "LATTICE_AGENT_TIMEOUT": "10",
    })
    env.pop("CLAUDECODE", None)
    proc = subprocess.run(
        [sys.executable, "-c", runner_inline],
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert proc.returncode != 0
    err_path = out.parent / "output.md.err"
    assert err_path.exists()
    assert (out.parent / "output.md.done").exists()


def test_merge_waiter_assembles_inputs_and_invokes_merge(
    tmp_path: Path,
) -> None:
    """Merge-waiter polls upstream sentinels then executes the merge agent."""
    upstream = []
    for agent in ("claude", "codex", "gemini"):
        d = tmp_path / agent
        d.mkdir()
        (d / "output.md").write_text(f"{agent} review body", encoding="utf-8")
        (d / "output.md.done").touch()
        upstream.append(str(d))

    merge_out = tmp_path / "merge" / "output.md"
    merge_out.parent.mkdir()
    merge_prompt = tmp_path / "merge" / "prompt.md"
    merge_prompt.write_text("MERGE PROMPT\n{merge_inputs}\nDONE", encoding="utf-8")

    # Stub the merge agent to be the fake_agent script (writes deterministic output).
    runner_inline = (
        "import sys, lattice.core.agent_spawn as a, lattice.storage.agent_spawn as s; "
        f"cmd=lambda agent, p, o: 'LATTICE_AGENT_PROMPT='+p+' LATTICE_AGENT_OUTPUT='+o+' "
        f"{sys.executable} {FAKE_AGENT_PATH}'; "
        "a._agent_cli_command=cmd; s._agent_cli_command=cmd; "
        "from lattice.agent_runner import main; sys.exit(main(['--mode','merge-waiter']))"
    )
    env = os.environ.copy()
    env.pop("CLAUDECODE", None)
    env.update({
        "LATTICE_MERGE_UPSTREAM_DIRS": ":".join(upstream),
        "LATTICE_MERGE_PROMPT": str(merge_prompt),
        "LATTICE_AGENT_OUTPUT": str(merge_out),
        "LATTICE_AGENT_TIMEOUT": "10",
        "LATTICE_MERGE_AGENT": "claude",
    })
    proc = subprocess.run(
        [sys.executable, "-c", runner_inline],
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert proc.returncode == 0, f"stderr={proc.stderr!r} stdout={proc.stdout!r}"
    assert merge_out.exists()
    # Filled prompt should contain all three reviews.
    filled = (merge_prompt.parent / "merge_prompt_filled.md").read_text(encoding="utf-8")
    assert "claude review body" in filled
    assert "codex review body" in filled
    assert "gemini review body" in filled
