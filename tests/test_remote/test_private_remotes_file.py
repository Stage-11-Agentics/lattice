"""SPEC §9.1: ``remotes.json`` can hold tokens, so every reader (resolution,
``remote add``, ``remote list``, and the secret discovery that hooks and review
agents rely on) accepts only a private regular file: never group- or
world-readable, never a symlink (post-merge review, finding 3)."""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from lattice.core.errors import OpError
from lattice.remote.config import resolve_remote, secret_env_names
from tests.test_remote.hosted import run_cli

ENTRY = {"remotes": {"team": {"url": "https://h.example.com", "token": "literal-tok-3c9e"}}}


@pytest.fixture()
def config_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    home = tmp_path / "config"
    (home / "lattice").mkdir(parents=True)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(home))
    return home / "lattice" / "remotes.json"


def _world_readable(path: Path) -> None:
    path.write_text(json.dumps(ENTRY))
    path.chmod(0o644)


def _symlinked(path: Path) -> None:
    target = path.parent / "real-remotes.json"
    target.write_text(json.dumps(ENTRY))
    target.chmod(0o600)
    os.symlink(target, path)


@pytest.mark.parametrize("make", [_world_readable, _symlinked], ids=["0644", "symlink"])
def test_every_reader_refuses_a_file_that_is_not_private(
    config_home: Path, tmp_path: Path, make
) -> None:  # noqa: ANN001
    make(config_home)
    before = config_home.read_bytes()

    with pytest.raises(OpError) as exc:
        resolve_remote("team")
    assert exc.value.code == "VALIDATION_ERROR"
    with pytest.raises(OpError) as exc:
        secret_env_names({})
    assert exc.value.code == "VALIDATION_ERROR"

    for argv in (
        ["remote", "list", "--json"],
        ["remote", "add", "other", "https://o.example.com", "--token-env", "O_TOK", "--json"],
    ):
        result = run_cli(tmp_path, *argv)
        assert result.exit_code == 1, (argv, result.output)
        error = json.loads(result.stdout)["error"]
        assert error["code"] == "VALIDATION_ERROR", error
        assert "literal-tok-3c9e" not in result.output
    assert config_home.read_bytes() == before
    assert config_home.is_symlink() == (make is _symlinked)


def test_a_private_file_is_read_by_every_reader(config_home: Path, tmp_path: Path) -> None:
    ENTRY_ENV = {
        "remotes": {"team": {"url": "https://h.example.com", "token": {"env": "TEAM_TOK"}}}
    }
    config_home.write_text(json.dumps(ENTRY_ENV))
    config_home.chmod(0o600)
    assert "TEAM_TOK" in secret_env_names({})
    assert json.loads(run_cli(tmp_path, "remote", "list", "--json").stdout)["ok"] is True


def test_hooks_and_review_agents_start_nothing_when_the_file_is_not_private(
    config_home: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:  # noqa: ANN001
    """Fail closed: without a trustworthy file, the token variables cannot be
    named, so no child is started with them in reach."""
    from lattice.core.agent_spawn import SpawnRequest
    from lattice.storage.agent_spawn import HeadlessBackend
    from lattice.storage.hooks import execute_hooks

    _world_readable(config_home)
    sentinel = tmp_path / "hook-ran"
    execute_hooks(
        {"hooks": {"post_event": f"touch {sentinel}"}},
        tmp_path,
        "task_x",
        {"type": "comment_added", "id": "ev_1", "actor": "human:a"},
    )
    assert not sentinel.exists()
    assert "lattice: hooks skipped:" in capsys.readouterr().err

    started = tmp_path / "agent-ran"
    monkeypatch.setattr(
        "lattice.storage.agent_spawn._agent_cli_command",
        lambda agent, prompt, output: f"touch {started}",
    )
    prompt = tmp_path / "prompt.md"
    prompt.write_text("p")
    request = SpawnRequest(
        agent="claude", prompt_file=prompt, output_file=tmp_path / "o.md", label="r"
    )
    (result,) = HeadlessBackend().run([request], workspace_label="t")
    assert not result.success
    assert "chmod 600" in (result.error or "")
    assert not started.exists()
