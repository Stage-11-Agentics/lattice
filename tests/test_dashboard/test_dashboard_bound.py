"""``lattice dashboard`` on a bound checkout (H-13a: AC-24 bound part, AC-36; SPEC §8.3, §9.6).

A real in-process server, a checkout bound to it, and the dashboard serving
that checkout's cache: a dashboard drag reaches the server through
``HostedBoard.execute`` as the browser actor (``human:alice`` under both token
shapes, whatever actor the request names), with a browser origin; the embedded
follower brings another writer's change into the dashboard without it asking;
and the follower clears its freshness record when the dashboard stops.
"""

from __future__ import annotations

import http.client
import json
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

import pytest

from lattice.boards import HostedBoard, resolve_board
from lattice.dashboard.bound import bound_dashboard
from lattice.dashboard.server import create_server
from lattice.remote import session
from lattice.remote.follower import read_follower
from lattice.server import tokens
from lattice.server.testing import wait_for
from tests.test_remote.hosted import TOKEN_ENV, HostedEnv, events_of, make_repo, run_cli


@contextmanager
def dashboard(repo: Path) -> Iterator[int]:
    """The bound checkout's dashboard on ``127.0.0.1:0``; yields its port."""
    board = resolve_board(repo)
    assert isinstance(board, HostedBoard)
    with bound_dashboard(board, on_notice=lambda line: None) as target:
        server = create_server(repo / ".lattice", "127.0.0.1", 0, board=target)
        thread = threading.Thread(
            target=server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True
        )
        thread.start()
        try:
            yield server.server_address[1]
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)


def request(port: int, method: str, path: str, body: object = None) -> tuple[int, dict]:
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=30)
    headers = {"Host": f"127.0.0.1:{port}", "Origin": f"http://127.0.0.1:{port}"}
    data = None
    if body is not None:
        data = json.dumps(body).encode()
        headers["Content-Type"] = "application/json"
    conn.request(method, path, body=data, headers=headers)
    resp = conn.getresponse()
    payload = json.loads(resp.read().decode())
    conn.close()
    return resp.status, payload


def bound_repo(env: HostedEnv, tmp_path: Path) -> tuple[Path, str]:
    """A bound clone whose cache holds task DEM-1; returns (repo, task id)."""
    repo = env.bind(make_repo(tmp_path / "repo"))
    created = run_cli(repo, "create", "Drag me", "--actor", "human:alice", "--json")
    assert created.exit_code == 0, created.output
    return repo, json.loads(created.output)["data"]["id"]


def use_token(env: HostedEnv, actors: tuple[str, ...]) -> str:
    """Point the remote at a token for ``human:alice`` with *actors*; returns its id."""
    minted = tokens.create_token(
        env.server_root, user="human:alice", machine="laptop", actors=actors, projects=["demo"]
    )
    env.monkeypatch.setenv(TOKEN_ENV, minted["token"])
    session.reset_process_state()
    return minted["record"]["id"]


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


def test_the_embedded_follower_brings_other_writers_changes_in(
    hosted_env: HostedEnv, tmp_path: Path
) -> None:
    repo, task = bound_repo(hosted_env, tmp_path)

    with dashboard(repo) as port:
        assert wait_for(lambda: (read_follower(repo) or {}).get("stream_live_until"), timeout=10)
        # Another writer, straight to the server: the dashboard never asks.
        hosted_env.server_op("task.comment", {"task": task, "text": "from elsewhere"})

        def seen() -> bool:
            _, events = request(port, "GET", f"/api/tasks/{task}/events")
            return any(e["type"] == "comment_added" for e in events["data"])

        assert wait_for(seen, timeout=10)

    record = read_follower(repo) or {}
    assert not record.get("stream_live_until")


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
