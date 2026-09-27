"""Thin clients (AC-19, AC-21; H-12): the whole agent loop from a box with a
temp ``HOME``, no config file, remote settings from the environment only, and
no follower. AC-21 starts from ``git clone`` of a repository holding the
committed binding."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from tests.test_remote.hosted import HostedEnv, events_of, git, make_repo, run_cli


def thin_box(env: HostedEnv, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A fresh HOME with no Lattice config; the remote comes from the environment."""
    home = tmp_path / "box-home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(home / ".config"))
    monkeypatch.setenv("XDG_DATA_HOME", str(home / ".local" / "share"))
    monkeypatch.delenv("LATTICE_TOKEN_TEAM", raising=False)
    monkeypatch.setenv("LATTICE_REMOTE_TEAM_URL", env.url)
    monkeypatch.setenv("LATTICE_REMOTE_TEAM_TOKEN", env.token)
    return home


def agent_loop(env: HostedEnv, checkout: Path, home: Path) -> None:
    """claim → plan write → status → comment → attach → complete, as ``agent:box``."""
    me = ("--actor", "agent:box")
    created = env.server_op("task.create", {"title": "Thin work"}, actor="human:alice")
    short = created["result"]["task"]["short_id"]

    def ok(*args: str, stdin: str | None = None) -> dict:
        result = run_cli(checkout, *args, *me, "--json", input=stdin)
        assert result.exit_code == 0, result.output
        return json.loads(result.stdout)["data"]

    ok("claim", short, "--surface", "surface:1")
    ok("status", short, "in_planning")
    ok("plan", "write", short, "--stdin", stdin="# Plan\n\n- Do it.\n")
    ok("status", short, "planned")
    ok("status", short, "in_progress")
    ok("comment", short, "Working on it from a thin box.")
    evidence = checkout / "evidence.txt"
    evidence.write_text("It works.\n")
    ok("attach", short, str(evidence), "--title", "Evidence")
    done = ok("complete", short, "--review", "Reviewed: fine.")
    assert done["status"] == "done"

    types = [e["type"] for e in events_of(env, short)]
    for expected in (
        "plan_written",
        "comment_added",
        "artifact_attached",
        "status_changed",
    ):
        assert expected in types
    # The final status change, whatever else (a client-side record) lands after it.
    changes = [e for e in events_of(env, short) if e["type"] == "status_changed"]
    assert changes[-1]["data"]["to"] == "done"
    # The cache caught up with every write, the plan included.
    shown = json.loads(run_cli(checkout, "show", short, "--json").stdout)["data"]
    assert shown["status"] == "done"
    plan = run_cli(checkout, "plan", short)
    assert "Do it." in plan.stdout
    # A thin client runs no follower and wrote no config.
    assert not (checkout / ".lattice" / "cache" / "follower.json").exists()
    assert not (home / ".config" / "lattice").exists()


def test_environment_only_thin_client_runs_the_whole_loop(
    hosted_env: HostedEnv, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """AC-19."""
    home = thin_box(hosted_env, tmp_path, monkeypatch)
    checkout = hosted_env.bind(make_repo(tmp_path / "box-checkout"))
    agent_loop(hosted_env, checkout, home)


def test_thin_client_from_a_git_clone_of_the_bound_repository(
    hosted_env: HostedEnv, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """AC-21: the binding arrives with ``git clone``; nothing else is set up."""
    origin = hosted_env.bind(make_repo(tmp_path / "origin"))
    (origin / ".gitignore").write_text("/.lattice/\n")
    git(origin, "add", ".lattice-remote.json", ".gitignore")
    git(origin, "commit", "-q", "-m", "bind the board")

    home = thin_box(hosted_env, tmp_path, monkeypatch)
    clone = tmp_path / "box" / "clone"
    clone.parent.mkdir()
    git(clone.parent, "clone", "-q", str(origin), str(clone))
    assert (clone / ".lattice-remote.json").is_file()
    assert not (clone / ".lattice").exists()
    agent_loop(hosted_env, clone, home)
    # The board never shows up in git.
    assert git(clone, "status", "--porcelain") == "?? evidence.txt"


def test_a_write_as_the_first_command_is_recorded_for_verify(
    hosted_env: HostedEnv, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A fresh checkout's first command is a write, before any sync has made the
    cache: the acknowledged write still lands in ``cache/acked.jsonl`` (SPEC §9.5)
    with no notice, and the first sync then adopts the checkout as a cache."""
    thin_box(hosted_env, tmp_path, monkeypatch)
    checkout = hosted_env.bind(make_repo(tmp_path / "first-write"))
    assert not (checkout / ".lattice").exists()
    created = run_cli(checkout, "create", "First", "--actor", "agent:box", "--json")
    assert created.exit_code == 0, created.output
    assert "could not record" not in created.stderr
    op_ids = [
        json.loads(line)["op_id"]
        for line in (checkout / ".lattice" / "cache" / "acked.jsonl").read_text().splitlines()
    ]
    assert len(op_ids) == 1
    shown = run_cli(checkout, "show", json.loads(created.stdout)["data"]["short_id"], "--json")
    assert shown.exit_code == 0, shown.output
