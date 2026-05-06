"""AC#6: durable proof that spawn_one is callable from non-review contexts.

The agent_spawn primitive was carved out of core.review so future Lattice
commands (ticket summarization, PR description generation, etc.) can reuse
the same spawn machinery without reaching into review-specific helpers.

This test exercises spawn_one against the existing fake_agent fixture from
a non-review caller — i.e., it imports only the public agent_spawn surface,
constructs a SpawnRequest by hand, and verifies the contract (output file,
sentinel, success flag).
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from lattice.core.agent_spawn import (
    SpawnRequest,
    sentinel_path,
    spawn_one,
)
from lattice.storage.agent_spawn import HeadlessBackend


FAKE_AGENT_PATH = Path(__file__).resolve().parent.parent / "fixtures" / "fake_agent.py"


def test_spawn_one_callable_outside_review(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A future ticket-summarization-style caller can drive spawn_one directly."""
    # A non-review caller would do this exact dance — no review.py imports.
    sub = tmp_path / "summarize"
    sub.mkdir()
    prompt = sub / "prompt.md"
    prompt.write_text("Summarize the ticket.", encoding="utf-8")
    output = sub / "summary.md"

    # Patch the agent CLI command to run the fake agent for any agent type.
    fake_cmd_template = f"{sys.executable} {FAKE_AGENT_PATH}"

    def _stub(agent_type: str, prompt_file: str, output_file: str) -> str:
        return (
            f"LATTICE_AGENT_PROMPT={prompt_file} "
            f"LATTICE_AGENT_OUTPUT={output_file} "
            f"LATTICE_AGENT_TYPE={agent_type} "
            f"{fake_cmd_template}"
        )

    monkeypatch.setattr("lattice.core.agent_spawn._agent_cli_command", _stub)
    monkeypatch.setattr("lattice.storage.agent_spawn._agent_cli_command", _stub)

    request = SpawnRequest(
        agent="claude",
        prompt_file=prompt,
        output_file=output,
        label="summarize :: claude",
        timeout_seconds=10,
    )
    result = spawn_one(
        request,
        workspace_label="summarize-LAT-205",
        backend=HeadlessBackend(),
    )

    assert result.success, result.error
    assert "FAKE-AGENT-OUTPUT" in result.output_text
    assert output.exists()
    assert sentinel_path(output).exists()
