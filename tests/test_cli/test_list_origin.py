"""AC-39 (CLI): ``lattice list --machine/--user/--worktree``.

A task matches when at least one event in its log carries an origin that
satisfies every origin filter given, with user and machine read the way
``show --events`` reads them (authenticated, else reported). Tasks whose
events carry no origin (written before v2) never match.
"""

from __future__ import annotations

import getpass
import json
import socket
import subprocess
from pathlib import Path

import pytest
from click.testing import CliRunner

from lattice.cli.main import cli
from lattice.core.events import create_event
from lattice.core.ids import generate_task_id
from lattice.core.origin import origin_matches
from lattice.storage.operations import mutate_task_events

ACTOR = "agent:o"
ALICE = {
    "reported": {"os_user": "alice", "host": "lap", "worktree": "/srv/wt-auth", "branch": "b"},
    "authenticated": {"token_id": "tok_x", "user": "human:alice", "machine": "alice-laptop"},
}


# -- the matching rule (pure) ------------------------------------------------


def _event(origin: dict | None) -> dict:
    event = {"type": "comment_added", "actor": ACTOR, "data": {}}
    if origin is not None:
        event["origin"] = origin
    return event


def test_authenticated_wins_over_reported() -> None:
    event = _event(ALICE)
    assert origin_matches(event, user="human:alice", machine="alice-laptop")
    assert not origin_matches(event, user="alice")
    assert not origin_matches(event, machine="lap")


def test_reported_used_without_authenticated() -> None:
    event = _event({"reported": {"os_user": "bob", "host": "box", "worktree": "/w"}})
    assert origin_matches(event, user="bob", machine="box", worktrees=frozenset({"/w"}))
    assert not origin_matches(event, worktrees=frozenset({"/other"}))


def test_every_filter_must_hold_on_the_same_event() -> None:
    event = _event(ALICE)
    assert not origin_matches(event, user="human:alice", machine="other")


def test_legacy_and_browser_events() -> None:
    assert not origin_matches(_event(None), user="alice")
    assert not origin_matches(_event(None))
    browser = _event({"reported": {"os_user": "u", "host": "h", "source": "browser"}})
    assert origin_matches(browser, user="u")
    assert not origin_matches(browser, worktrees=frozenset({""}))


# -- the command ---------------------------------------------------------------


@pytest.fixture()
def board(initialized_root: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict:
    """A board with tasks written from a git worktree, a server, and before v2."""
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", "-b", "feat/x"], cwd=repo, check=True)
    monkeypatch.chdir(repo)
    env = {"LATTICE_ROOT": str(initialized_root)}
    runner = CliRunner()
    lattice_dir = initialized_root / ".lattice"

    def create(title: str) -> str:
        result = runner.invoke(cli, ["create", title, "--actor", ACTOR, "--json"], env=env)
        assert result.exit_code == 0, result.output
        return json.loads(result.output)["data"]["id"]

    def create_legacy(title: str) -> str:
        """A task written before v2: no event carries an origin."""
        task_id = generate_task_id()
        data = {"title": title, "status": "backlog", "priority": "medium", "type": "task"}
        event = create_event("task_created", task_id, "human:old", data)
        mutate_task_events(
            lattice_dir,
            task_id,
            [event],
            source="absent",
            may_emit_lifecycle=True,
            run_hooks=False,
        )
        return task_id

    def append(task_id: str, origin: dict) -> None:
        event = create_event("comment_added", task_id, ACTOR, {"body": "x"})
        event["origin"] = origin
        mutate_task_events(lattice_dir, task_id, [event], run_hooks=False)

    local = create("Local")  # reported origin of this process
    legacy = create_legacy("Legacy")
    served = create_legacy("Served")
    append(served, ALICE)
    split = create_legacy("Split")  # user X on one event, machine M2 on another
    append(split, {"authenticated": {"user": "human:x", "machine": "m1"}})
    append(split, {"authenticated": {"user": "human:y", "machine": "m2"}})
    archived = create_legacy("Archived")
    append(archived, ALICE)
    result = runner.invoke(cli, ["archive", archived, "--actor", ACTOR], env=env)
    assert result.exit_code == 0, result.output

    return {
        "env": env,
        "repo": repo,
        "local": local,
        "legacy": legacy,
        "served": served,
        "split": split,
        "archived": archived,
    }


def _ids(board: dict, *args: str) -> list[str]:
    result = CliRunner().invoke(cli, ["list", "--json", *args], env=board["env"])
    assert result.exit_code == 0, result.output
    return [task["id"] for task in json.loads(result.output)["data"]]


def test_machine_filter(board: dict) -> None:
    assert _ids(board, "--machine", socket.gethostname()) == [board["local"]]
    assert _ids(board, "--machine", "alice-laptop") == [board["served"]]
    assert _ids(board, "--machine", "lap") == []  # reported host loses to authenticated


def test_user_filter(board: dict) -> None:
    assert _ids(board, "--user", getpass.getuser()) == [board["local"]]
    assert _ids(board, "--user", "human:alice") == [board["served"]]
    assert _ids(board, "--user", "alice") == []


def test_worktree_filter(board: dict) -> None:
    assert _ids(board, "--worktree", str(board["repo"].resolve())) == [board["local"]]
    assert _ids(board, "--worktree", ".") == [board["local"]]  # relative to the cwd
    assert _ids(board, "--worktree", "/srv/wt-auth/") == [board["served"]]
    assert _ids(board, "--worktree", "/nowhere") == []


def test_worktree_through_a_symlink(board: dict, tmp_path: Path) -> None:
    link = tmp_path / "link"
    link.symlink_to(board["repo"])
    assert _ids(board, "--worktree", str(link)) == [board["local"]]


def test_filters_hold_on_one_event(board: dict) -> None:
    assert _ids(board, "--user", "human:x", "--machine", "m1") == [board["split"]]
    assert _ids(board, "--user", "human:x", "--machine", "m2") == []
    assert _ids(board, "--user", "human:alice", "--worktree", "/srv/wt-auth") == [board["served"]]


def test_legacy_tasks_never_match(board: dict) -> None:
    everything = _ids(board)
    assert board["legacy"] in everything
    for args in (
        ("--machine", socket.gethostname()),
        ("--user", getpass.getuser()),
        ("--worktree", "."),
        ("--user", ""),
    ):
        assert board["legacy"] not in _ids(board, *args)


def test_and_with_existing_filters(board: dict) -> None:
    runner = CliRunner()
    moved = runner.invoke(
        cli, ["status", board["local"], "in_planning", "--actor", ACTOR], env=board["env"]
    )
    assert moved.exit_code == 0, moved.output
    assert _ids(board, "--user", "human:alice", "--status", "in_planning") == []
    assert _ids(board, "--user", getpass.getuser(), "--status", "in_planning") == [board["local"]]
    assert _ids(board, "--user", getpass.getuser(), "--status", "backlog") == []


def test_archived_only_with_include_archived(board: dict) -> None:
    assert _ids(board, "--user", "human:alice") == [board["served"]]
    assert _ids(board, "--user", "human:alice", "--include-archived") == sorted(
        [board["served"], board["archived"]]
    )


def test_output_shapes_unchanged(board: dict) -> None:
    runner = CliRunner()
    env = board["env"]

    def run(*args: str) -> str:
        result = runner.invoke(cli, ["list", *args], env=env)
        assert result.exit_code == 0, result.output
        return result.output

    for fmt in ((), ("--json",), ("--json", "--compact"), ("--quiet",)):
        full = run(*fmt)
        filtered = run(*fmt, "--user", "human:alice")
        if "--json" in fmt:
            full_data = json.loads(full)["data"]
            data = json.loads(filtered)
            assert set(data) == {"ok", "data"}
            assert data["data"] == [t for t in full_data if t["id"] == board["served"]]
        else:
            lines = filtered.splitlines()
            assert len(lines) == 1 and lines[0] in full.splitlines()
            assert "Served" in lines[0] or fmt == ("--quiet",)


def test_help_states_the_rule() -> None:
    result = CliRunner().invoke(cli, ["list", "--help"])
    assert result.exit_code == 0
    text = " ".join(result.output.split())
    assert "at least one event" in text
    assert "--machine" in text and "--user" in text and "--worktree" in text
    assert "before v2" in text
