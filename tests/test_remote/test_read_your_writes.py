"""AC-6: a hosted write is visible to the very next read, and its client-local
effects (auto-review) run on the client (SPEC §3.4, §9.5)."""

from __future__ import annotations

import json
import os
import shlex
import sys
from pathlib import Path

import pytest

from lattice.remote import cache
from tests.test_remote.hosted import (
    TOKEN_ENV,
    HostedEnv,
    SpawnRecorder,
    events_of,
    make_repo,
    run_cli,
    walk_to,
)


def _attached(env: HostedEnv, tmp_path: Path) -> Path:
    repo = make_repo(tmp_path / "repo")
    result = run_cli(repo, "remote", "attach", "team", "demo")
    assert result.exit_code == 0, result.output
    return repo


def test_write_then_show_sees_it_without_a_follower(hosted_env: HostedEnv, tmp_path: Path) -> None:
    repo = _attached(hosted_env, tmp_path)
    created = run_cli(repo, "create", "Visible at once", "--actor", "human:alice", "--json")
    assert created.exit_code == 0, created.output
    short_id = json.loads(created.stdout)["data"]["short_id"]
    shown = run_cli(repo, "show", short_id, "--json")
    assert shown.exit_code == 0, shown.output
    assert json.loads(shown.stdout)["data"]["title"] == "Visible at once"
    commented = run_cli(repo, "comment", short_id, "second write", "--actor", "human:alice")
    assert commented.exit_code == 0, commented.output
    shown = run_cli(repo, "show", short_id, "--json")
    assert json.loads(shown.stdout)["data"]["comment_count"] == 1
    # The cache is the server's board, byte for byte, after each write's sync.
    assert not (repo / ".lattice" / "cache" / "follower.json").exists()


def test_hosted_review_transition_spawns_and_records_auto_review(
    hosted_env: HostedEnv, tmp_path: Path, spawns: SpawnRecorder
) -> None:
    """On a fresh cache (runtime directories created by its first sync), a hosted
    ``status`` to ``review`` fires the review on this client and records it."""
    from lattice.server import admin

    admin.set_project_config(
        hosted_env.server_root,
        "demo",
        {"auto_code_review_on_transition": "true", "auto_plan_review_on_transition": "false"},
    )
    repo = make_repo(tmp_path / "clone")
    hosted_env.bind(repo)
    assert not (repo / ".lattice").exists()
    created = run_cli(repo, "create", "Reviewed task", "--actor", "agent:dev", "--json")
    assert created.exit_code == 0, created.output
    for runtime in cache.RUNTIME_DIRS:
        assert (repo / ".lattice" / runtime).is_dir()
    walk_to(repo, "DEM-1", "in_planning", "planned", "in_progress")
    assert spawns.calls == []  # planned: plan reviews stay off on this board
    moved = run_cli(repo, "status", "DEM-1", "review", "--actor", "agent:dev", "--json")
    assert moved.exit_code == 0, moved.output
    data = json.loads(moved.stdout)["data"]
    assert data["auto_review"]["fired"] is True
    assert spawns.review_types == ["code-review"]
    # The spawned `lattice code-review` keeps the remote's credentials: it writes
    # through the binding like any CLI call.
    assert spawns.calls[0]["cwd"] == str(repo)
    recorded = [e for e in events_of(hosted_env, "DEM-1") if e["type"] == "auto_review_spawned"]
    assert len(recorded) == 1
    assert recorded[0]["actor"] == "agent:lattice-auto-review"
    assert recorded[0]["origin"]["authenticated"]["user"] == "human:alice"
    # The status op and the record are two operations with two op_ids.
    status_event = next(
        e
        for e in events_of(hosted_env, "DEM-1")
        if e["type"] == "status_changed" and e["data"]["to"] == "review"
    )
    assert status_event["origin"]["op_id"] != recorded[0]["origin"]["op_id"]
    # The record is visible in this checkout's cache at once.
    shown = run_cli(repo, "show", "DEM-1", "--json", "--full")
    assert shown.exit_code == 0, shown.output


def test_review_agent_environment_holds_no_remote_credentials(
    hosted_env: HostedEnv, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The model process ``core/agent_spawn.py`` starts never sees the token,
    ``LATTICE_REMOTE_*``, or a proxy header variable (SPEC §3.4)."""
    from lattice.core.agent_spawn import SpawnRequest, spawn_one
    from lattice.storage.agent_spawn import HeadlessBackend

    hosted_env.write_remote(headers={"CF-Access-Client-Id": {"env": "PROXY_ID"}})
    monkeypatch.setenv("PROXY_ID", "proxy-id-value")
    monkeypatch.setenv("LATTICE_REMOTE_TEAM_URL", hosted_env.url)
    monkeypatch.setenv("LATTICE_REMOTE_OTHER_HEADERS", json.dumps({"X-Other": "OTHER_SECRET"}))
    monkeypatch.setenv("OTHER_SECRET", "other-secret")
    monkeypatch.setenv("UNRELATED", "kept")
    dump = tmp_path / "env.json"
    script = (
        "import json, os, sys; "
        f"json.dump(dict(os.environ), open({str(dump)!r}, 'w')); "
        "open(os.environ['LATTICE_AGENT_OUTPUT'], 'w').write('ok')"
    )

    def command(agent: str, prompt: str, output: str) -> str:
        return (
            f"LATTICE_AGENT_PROMPT={prompt} LATTICE_AGENT_OUTPUT={output} "
            f"{shlex.quote(sys.executable)} -c {shlex.quote(script)}"
        )

    monkeypatch.setattr("lattice.storage.agent_spawn._agent_cli_command", command)
    prompt = tmp_path / "prompt.md"
    prompt.write_text("review")
    request = SpawnRequest(
        agent="claude",
        prompt_file=prompt,
        output_file=tmp_path / "out.md",
        label="review",
        timeout_seconds=20,
    )
    result = spawn_one(request, workspace_label="t", backend=HeadlessBackend())
    assert result.success, result.error
    seen = json.loads(dump.read_text())
    for name in (TOKEN_ENV, "PROXY_ID", "LATTICE_REMOTE_TEAM_URL", "OTHER_SECRET"):
        assert name not in seen, name
    assert not [k for k in seen if k.startswith("LATTICE_REMOTE_")]
    assert seen["UNRELATED"] == "kept"
    assert os.environ[TOKEN_ENV]  # the parent keeps its own


def test_next_with_name_reads_without_writing_the_cache(
    hosted_env: HostedEnv, tmp_path: Path
) -> None:
    repo = _attached(hosted_env, tmp_path)
    hosted_env.server_op(
        "session.start", {"name": "Argus", "model": "claude-opus", "framework": "claude-code"}
    )
    assert run_cli(repo, "create", "Pick me", "--actor", "human:alice").exit_code == 0
    sessions = repo / ".lattice" / "sessions"
    before = {p: p.read_bytes() for p in sessions.rglob("*") if p.is_file()}
    assert before, "the session was synced into the cache"
    result = run_cli(repo, "next", "--name", "Argus-1", "--json")
    assert result.exit_code == 0, result.output
    after = {p: p.read_bytes() for p in sessions.rglob("*") if p.is_file()}
    assert after == before


def test_a_write_whose_post_write_sync_fails_still_succeeds(
    hosted_env: HostedEnv, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from lattice.remote import cache as cache_mod

    repo = _attached(hosted_env, tmp_path)

    def unreachable(root: Path, *, bulk: bool = False, **_: object) -> cache_mod.SyncOutcome:
        return cache_mod.SyncOutcome("unreachable", 0, "2026-01-01T00:00:00Z", "down")

    with monkeypatch.context() as patched:
        patched.setattr(cache_mod, "catch_up", unreachable)
        result = run_cli(repo, "create", "Committed anyway", "--actor", "human:alice", "--json")
    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout)["data"]["title"] == "Committed anyway"
    assert "lattice: cannot reach team; showing cache as of 2026-01-01T00:00:00Z" in result.stderr
    shown = run_cli(repo, "show", "DEM-1", "--json")
    assert json.loads(shown.stdout)["data"]["title"] == "Committed anyway"


def test_planning_hint_on_a_hosted_checkout_names_plan_write(
    hosted_env: HostedEnv, tmp_path: Path
) -> None:
    repo = _attached(hosted_env, tmp_path)
    assert run_cli(repo, "create", "Plan me", "--actor", "agent:dev").exit_code == 0
    moved = run_cli(repo, "status", "DEM-1", "in_planning", "--actor", "agent:dev")
    assert moved.exit_code == 0, moved.output
    assert "lattice plan write DEM-1 --file <path>" in moved.stdout
    assert "plans/task_" not in moved.stdout
