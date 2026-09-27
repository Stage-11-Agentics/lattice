"""Thin-client mechanics (H-11 part of AC-19/AC-21): a temp ``HOME``, no config
file, remote settings from the environment only, no follower; ``create``,
``status``, and ``comment`` work end to end. The full loop is H-12's."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from tests.test_remote.hosted import HostedEnv, make_repo, run_cli


def test_environment_only_remote(
    hosted_env: HostedEnv, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = tmp_path / "box-home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(home / ".config"))
    monkeypatch.setenv("LATTICE_REMOTE_TEAM_URL", hosted_env.url)
    monkeypatch.setenv("LATTICE_REMOTE_TEAM_TOKEN", hosted_env.token)
    assert not (home / ".config" / "lattice" / "remotes.json").exists()

    repo = make_repo(tmp_path / "box-checkout")
    hosted_env.bind(repo)
    created = run_cli(repo, "create", "From the box", "--json")  # actor defaulted by the server
    assert created.exit_code == 0, created.output
    task = json.loads(created.stdout)["data"]
    assert task["created_by"] == "human:alice"
    assert run_cli(repo, "status", task["short_id"], "in_planning").exit_code == 0
    commented = run_cli(repo, "comment", task["short_id"], "box says hi", "--actor", "agent:box")
    assert commented.exit_code == 0, commented.output
    shown = json.loads(run_cli(repo, "show", task["short_id"], "--json").stdout)["data"]
    assert shown["status"] == "in_planning"
    assert shown["comment_count"] == 1
    assert not (repo / ".lattice" / "cache" / "follower.json").exists()
    assert not (home / ".config" / "lattice").exists()
