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

    env = {
        "LATTICE_AGENT_TYPE": "claude",
        "LATTICE_AGENT_PROMPT": str(prompt),
        "LATTICE_AGENT_OUTPUT": str(out),
        "LATTICE_AGENT_TIMEOUT": "10",
        "LATTICE_AGENT_LABEL": "test :: claude",
    }
    # Stub _agent_cli_command in-process so the wrapper invokes the fake
    # agent instead of the real claude binary.
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
    env.update(
        {
            "LATTICE_AGENT_TYPE": "claude",
            "LATTICE_AGENT_PROMPT": str(prompt),
            "LATTICE_AGENT_OUTPUT": str(out),
            "LATTICE_AGENT_TIMEOUT": "10",
        }
    )
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


def test_stdout_streams_to_pane(tmp_path: Path) -> None:
    """Regression: agent stdout must stream through the wrapper's stdout.

    Pre-fix, ``_run_agent_mode`` used ``subprocess.run(..., capture_output=True)``
    which buffered the entire subprocess output until exit, then wrote it
    AFTER the ``[agent_runner] finished`` marker. That silenced the cmux /
    terminal pane for the whole run — the visibility win that motivated
    LAT-205 was nullified.

    Post-fix uses Popen + a reader thread that tees each line live. The
    ordering guarantee: the agent's output appears BEFORE the
    ``[agent_runner] finished`` marker in the wrapper's stdout.
    """
    out = tmp_path / "output.md"
    prompt = tmp_path / "prompt.md"
    prompt.write_text("hi", encoding="utf-8")

    runner_inline = (
        "import sys, lattice.core.agent_spawn as a, lattice.storage.agent_spawn as s; "
        f"cmd=lambda agent, p, o: 'LATTICE_FAKE_BEHAVIOR=stdout LATTICE_AGENT_PROMPT='+p+' "
        f"LATTICE_AGENT_OUTPUT='+o+' {sys.executable} {FAKE_AGENT_PATH}'; "
        "a._agent_cli_command=cmd; s._agent_cli_command=cmd; "
        "from lattice.agent_runner import main; sys.exit(main(['--mode','agent']))"
    )
    env = os.environ.copy()
    env.pop("CLAUDECODE", None)
    env.update(
        {
            "LATTICE_AGENT_TYPE": "claude",
            "LATTICE_AGENT_PROMPT": str(prompt),
            "LATTICE_AGENT_OUTPUT": str(out),
            "LATTICE_AGENT_TIMEOUT": "10",
            "LATTICE_AGENT_LABEL": "test :: stream",
        }
    )
    proc = subprocess.run(
        [sys.executable, "-c", runner_inline],
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert proc.returncode == 0, f"stderr={proc.stderr!r} stdout={proc.stdout!r}"
    stdout = proc.stdout
    # The agent line must appear at all (proves stdout is not swallowed).
    assert "FAKE-AGENT-STDOUT-OUTPUT" in stdout, stdout
    # And it must appear BEFORE the finished marker — proving it streamed
    # during the run rather than being dumped after.
    agent_idx = stdout.index("FAKE-AGENT-STDOUT-OUTPUT")
    finished_idx = stdout.index("[agent_runner] finished")
    assert agent_idx < finished_idx, (
        f"agent output dumped after finished marker (agent_idx={agent_idx}, "
        f"finished_idx={finished_idx}); stdout={stdout!r}"
    )
    # Stdout-fallback path should still write the output file.
    assert out.exists()
    assert "FAKE-AGENT-STDOUT-OUTPUT" in out.read_text(encoding="utf-8")


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
    env.update(
        {
            "LATTICE_MERGE_UPSTREAM_DIRS": ":".join(upstream),
            "LATTICE_MERGE_PROMPT": str(merge_prompt),
            "LATTICE_AGENT_OUTPUT": str(merge_out),
            "LATTICE_AGENT_TIMEOUT": "10",
            "LATTICE_MERGE_AGENT": "claude",
        }
    )
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
