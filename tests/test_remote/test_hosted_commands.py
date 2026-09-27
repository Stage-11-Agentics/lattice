"""Converted commands on a hosted checkout (review round 1): archive and
unarchive route to the server, the server defaults a missing actor, and a
write command's plain output is scrubbed like a read's (SPEC §4, §9.5)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from tests.test_remote.hosted import HostedEnv, make_repo, run_cli


@pytest.fixture()
def repo(hosted_env: HostedEnv, tmp_path: Path) -> Path:
    repo = make_repo(tmp_path / "repo")
    assert run_cli(repo, "remote", "attach", "team", "demo").exit_code == 0
    plan = repo / ".plan.md"
    plan.write_text("# Plan\n\n- Work.\n")
    for n, title in enumerate(("One", "Two", "Three"), start=1):
        assert run_cli(repo, "create", title, "--actor", "agent:dev").exit_code == 0
        assert run_cli(repo, "plan", "write", f"DEM-{n}", "--file", str(plan)).exit_code == 0
    return repo


def _placement(env: HostedEnv, short_id: str) -> str:
    ids = json.loads((env.board / "ids.json").read_text())["map"]
    task_id = ids[short_id]
    if (env.board / "archive" / "tasks" / f"{task_id}.json").exists():
        return "archived"
    assert (env.board / "tasks" / f"{task_id}.json").exists()
    return "active"


def _show(repo: Path, short_id: str) -> dict:
    result = run_cli(repo, "show", short_id, "--json")
    assert result.exit_code == 0, result.output
    return json.loads(result.stdout)["data"]


def test_archive_and_unarchive_route_to_the_server(hosted_env: HostedEnv, repo: Path) -> None:
    single = run_cli(repo, "archive", "DEM-1", "--actor", "agent:dev", "--json")
    assert single.exit_code == 0, single.output
    assert _placement(hosted_env, "DEM-1") == "archived"
    multi = run_cli(repo, "archive", "DEM-2", "DEM-3", "--actor", "agent:dev", "--json")
    assert multi.exit_code == 0, multi.output
    assert {_placement(hosted_env, s) for s in ("DEM-2", "DEM-3")} == {"archived"}
    # The cache followed: the moves are visible at once.
    assert json.loads(run_cli(repo, "list", "--json").stdout)["data"] == []

    back = run_cli(repo, "unarchive", "DEM-1", "--actor", "agent:dev")
    assert back.exit_code == 0, back.output
    assert _placement(hosted_env, "DEM-1") == "active"
    both = run_cli(repo, "unarchive", "DEM-2", "DEM-3", "--actor", "agent:dev", "--json")
    assert both.exit_code == 0, both.output
    assert {_placement(hosted_env, s) for s in ("DEM-2", "DEM-3")} == {"active"}
    assert len(json.loads(run_cli(repo, "list", "--json").stdout)["data"]) == 3


def test_the_server_defaults_a_missing_actor(hosted_env: HostedEnv, repo: Path) -> None:
    """``--actor`` is optional on a hosted checkout (SPEC §9.5): no client-side
    ``MISSING_ACTOR`` or ``--claim requires`` refusal."""
    claimed = run_cli(repo, "next", "--claim", "--json")
    assert claimed.exit_code == 0, claimed.output
    data = json.loads(claimed.stdout)["data"]
    assert data["assigned_to"] == "human:alice"
    assert data["status"] == "in_progress"

    for args in (
        ("archive", "DEM-2"),
        ("unarchive", "DEM-2"),
        ("archive", "DEM-2", "DEM-3"),
        ("comment", "DEM-1", "no actor given"),
        ("assign", "DEM-1", "agent:other"),
        ("needs-human", "DEM-1", "Need: a decision"),
        ("update", "DEM-1", "priority=high"),
    ):
        result = run_cli(repo, *args)
        assert result.exit_code == 0, (args, result.output)
    shown = _show(repo, "DEM-1")
    assert shown["assigned_to"] == "agent:other"
    assert shown["priority"] == "high"
    assert shown["comment_count"] == 1


def test_a_hosted_write_scrubs_its_plain_output(hosted_env: HostedEnv, tmp_path: Path) -> None:
    repo = make_repo(tmp_path / "repo")
    assert run_cli(repo, "remote", "attach", "team", "demo").exit_code == 0
    hosted_env.server_op("task.create", {"title": "Wipe \x1b[2J screen"}, actor="human:alice")
    hosted_env.server_op(
        "task.plan_write", {"task": "DEM-1", "file": "# Plan\n\n- Work.\n"}, actor="human:alice"
    )
    claimed = run_cli(repo, "next", "--claim", "--actor", "agent:dev", color=True)
    assert claimed.exit_code == 0, claimed.output
    assert "\x1b" not in claimed.stdout
    assert "Wipe �[2J screen" in claimed.stdout


def test_plan_gate_on_a_hosted_board_names_plan_write(
    hosted_env: HostedEnv, tmp_path: Path
) -> None:
    repo = make_repo(tmp_path / "repo")
    assert run_cli(repo, "remote", "attach", "team", "demo").exit_code == 0
    assert run_cli(repo, "create", "Unplanned", "--actor", "agent:dev").exit_code == 0
    assert run_cli(repo, "status", "DEM-1", "in_planning", "--actor", "agent:dev").exit_code == 0
    planned = run_cli(
        repo, "status", "DEM-1", "planned", "--actor", "agent:dev", "--no-auto-review"
    )
    assert planned.exit_code == 0, planned.output
    moved = run_cli(repo, "status", "DEM-1", "in_progress", "--actor", "agent:dev", "--json")
    assert moved.exit_code == 1
    error = json.loads(moved.stdout)["error"]
    assert error["code"] == "PLAN_REQUIRED"
    assert error["message"].endswith(
        "Write the plan with `lattice plan write DEM-1 --file <path>`."
    )


def test_archive_leaves_name_resolution_to_the_server(hosted_env: HostedEnv, repo: Path) -> None:
    """SPEC §3.7: the writer resolves ``--name``. A session the server has and
    this cache has not yet synced (a live follower means no catch-up) works."""
    import os

    (repo / ".lattice" / "cache" / "follower.json").write_text(
        json.dumps({"pid": os.getpid(), "stream_live_until": "2999-01-01T00:00:00Z"})
    )
    hosted_env.server_op(
        "session.start", {"name": "Argus", "model": "m", "framework": "claude-code"}
    )
    assert not [p for p in (repo / ".lattice" / "sessions").rglob("*") if p.is_file()]
    for args in (("archive", "DEM-1"), ("unarchive", "DEM-1"), ("archive", "DEM-2", "DEM-3")):
        result = run_cli(repo, *args, "--name", "Argus-1", "--json")
        assert result.exit_code == 0, (args, result.output)
    archived = [e for e in _events(hosted_env, "DEM-2") if e["type"] == "task_archived"]
    assert archived and archived[-1]["actor"]["name"] == "Argus-1"


def _events(env: HostedEnv, short_id: str) -> list[dict]:
    ids = json.loads((env.board / "ids.json").read_text())["map"]
    task_id = ids[short_id]
    path = env.board / "archive" / "events" / f"{task_id}.jsonl"
    if not path.exists():
        path = env.board / "events" / f"{task_id}.jsonl"
    return [json.loads(line) for line in path.read_text().splitlines()]
