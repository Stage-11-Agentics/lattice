"""``lattice dashboard`` on a bound checkout (H-13a: AC-24 bound part, AC-36; SPEC §8.3, §9.6).

A real in-process server, a checkout bound to it, and the dashboard serving
that checkout's cache: a dashboard drag reaches the server through
``HostedBoard.execute`` as the browser actor (``human:alice`` under both token
shapes, whatever actor the request names, read for every write so a
re-scoped token takes effect at once), with a browser origin; a token with no
browser actor, or a server that is down, refuses the write with nothing sent.
Reads and the follower: ``test_dashboard_bound_reads.py``.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from lattice.server import tokens
from tests.test_dashboard.bound_helpers import bound_repo, dashboard, request, use_token
from tests.test_remote.hosted import HostedEnv, events_of, make_repo, run_cli


@pytest.mark.parametrize("actors", [("human:alice",), ("human:alice", "agent:*")])
def test_drag_writes_to_the_server_as_the_browser_actor(
    hosted_env: HostedEnv, tmp_path: Path, actors: tuple[str, ...]
) -> None:
    use_token(hosted_env, actors)
    repo, task = bound_repo(hosted_env, tmp_path)

    with dashboard(repo) as port:
        status, body = request(
            port,
            "POST",
            f"/api/tasks/{task}/status",
            {"status": "in_planning", "actor": "agent:mallory"},
        )
        assert status == 200, body
        assert body["data"]["status"] == "in_planning"

        # The dashboard reads the cache, which the write brought up to date.
        status, tasks = request(port, "GET", "/api/tasks")
        assert status == 200
        assert {t["id"]: t["status"] for t in tasks["data"]}[task] == "in_planning"

    moved = [e for e in events_of(hosted_env, "DEM-1") if e["type"] == "status_changed"]
    (event,) = moved
    assert event["actor"] == "human:alice"
    assert event["origin"]["reported"]["source"] == "browser"
    assert "worktree" not in event["origin"]["reported"]
    assert event["origin"]["authenticated"]["user"] == "human:alice"


def test_a_token_without_a_browser_actor_refuses_writes(
    hosted_env: HostedEnv, tmp_path: Path
) -> None:
    use_token(hosted_env, ("agent:*",))
    repo = hosted_env.bind(make_repo(tmp_path / "repo"))
    hosted_env.server_op("task.create", {"title": "Seed"}, actor="agent:seed")
    assert run_cli(repo, "list").exit_code == 0
    task = json.loads((repo / ".lattice" / "ids.json").read_text())["map"]["DEM-1"]

    with dashboard(repo) as port:
        status, body = request(port, "POST", f"/api/tasks/{task}/comment", {"body": "hi"})

    assert status == 400
    assert body["error"]["code"] == "MISSING_ACTOR"
    assert [e["type"] for e in events_of(hosted_env, "DEM-1")] == ["task_created"]


def test_open_plans_is_local_only_on_a_bound_checkout(
    hosted_env: HostedEnv, tmp_path: Path
) -> None:
    repo, task = bound_repo(hosted_env, tmp_path)
    with dashboard(repo) as port:
        status, body = request(port, "POST", f"/api/tasks/{task}/open-plans", {})
    assert status == 400
    assert body["error"]["code"] == "LOCAL_ONLY"


def test_the_browser_actor_follows_the_token_as_it_is_rescoped(
    hosted_env: HostedEnv, tmp_path: Path
) -> None:
    """The browser actor is read for every write, never cached: re-scoping the
    token while the dashboard runs changes who the next write is attributed to."""
    token_id = use_token(hosted_env, ("agent:owner-3",))
    repo = hosted_env.bind(make_repo(tmp_path / "repo"))
    hosted_env.server_op("task.create", {"title": "Seed"}, actor="agent:owner-3")
    assert run_cli(repo, "list").exit_code == 0
    task = json.loads((repo / ".lattice" / "ids.json").read_text())["map"]["DEM-1"]
    root = hosted_env.server_root

    def comment(text: str) -> tuple[int, dict]:
        return request(port, "POST", f"/api/tasks/{task}/comment", {"body": text})

    def comment_actors() -> list[str]:
        return [e["actor"] for e in events_of(hosted_env, "DEM-1") if e["type"] == "comment_added"]

    with dashboard(repo) as port:
        # Only agent:owner-3 is permitted: the token's default actor writes.
        assert comment("as the seat")[0] == 200
        # human:alice (the token's user) is granted: she is the browser actor now.
        tokens.grant(root, token_id, actors=("human:alice",))
        assert comment("as the user")[0] == 200
        # Nothing browser-capable left: refused, nothing written.
        tokens.grant(root, token_id, actors=("agent:*",))
        tokens.ungrant(root, token_id, actors=("human:alice", "agent:owner-3"))
        status, body = comment("as nobody")
        assert status == 400
        assert body["error"]["code"] == "MISSING_ACTOR"

    assert comment_actors() == ["agent:owner-3", "human:alice"]


def test_an_unreachable_server_refuses_the_write_before_anything_is_sent(
    hosted_env: HostedEnv, tmp_path: Path
) -> None:
    repo, task = bound_repo(hosted_env, tmp_path)
    # Stopped before the dashboard starts: no follower holds a stream the
    # server's shutdown would wait for. The dashboard still serves the cache.
    hosted_env.stop()
    try:
        with dashboard(repo) as port:
            assert request(port, "GET", "/api/tasks")[0] == 200
            status, body = request(port, "POST", f"/api/tasks/{task}/comment", {"body": "x"})
    finally:
        hosted_env.start()
    assert status == 503
    assert body["error"]["code"] == "SERVER_UNREACHABLE"
    assert [e["type"] for e in events_of(hosted_env, "DEM-1")] == ["task_created"]


def test_a_forced_move_on_a_bound_checkout_records_force_and_the_browser_actor(
    hosted_env: HostedEnv, tmp_path: Path
) -> None:
    repo, task = bound_repo(hosted_env, tmp_path)
    with dashboard(repo) as port:
        status, body = request(
            port,
            "POST",
            f"/api/tasks/{task}/status",
            {"status": "in_progress", "force": True, "reason": "straight to work"},
        )
    assert status == 200, body
    (event,) = [e for e in events_of(hosted_env, "DEM-1") if e["type"] == "status_changed"]
    assert event["data"]["force"] is True
    assert event["data"]["reason"] == "straight to work"
    assert event["actor"] == "human:alice"
    assert event["origin"]["reported"]["source"] == "browser"


def test_each_browser_write_looks_at_the_offline_window_afresh(
    hosted_env: HostedEnv, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """SPEC §8.6 "No repeated wait" per write, not per dashboard: a window that
    was open when the dashboard started does not make later writes give up."""
    import time

    from lattice.remote import client, session
    from lattice.remote.binding import Hosted

    repo, task = bound_repo(hosted_env, tmp_path)
    window = repo / ".lattice" / "cache" / "unreachable_until"
    seen: list[bool] = []
    post = client.post_operation

    def spy(*args: object, offline: bool = False, **kwargs: object) -> dict:
        seen.append(offline)
        return post(*args, offline=offline, **kwargs)

    monkeypatch.setattr(client, "post_operation", spy)
    with dashboard(repo) as port:
        window.write_text(f"{time.time() + 60:.3f}\n")
        hosted = Hosted(repo, "team", "demo")
        assert session.window_open_at_start(hosted)  # as a stale memo would hold
        window.unlink()
        status, body = request(port, "POST", f"/api/tasks/{task}/comment", {"body": "hi"})
    assert status == 200, body
    assert seen == [False]
