"""Shared fixtures for the bound-checkout dashboard tests: a dashboard serving a
checkout bound to the in-process server of ``hosted_env``."""

from __future__ import annotations

import http.client
import json
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from lattice.boards import HostedBoard, resolve_board
from lattice.dashboard.bound import bound_dashboard
from lattice.dashboard.server import create_server
from lattice.remote import session
from lattice.server import tokens
from tests.test_remote.hosted import TOKEN_ENV, HostedEnv, make_repo, run_cli


@contextmanager
def dashboard(repo: Path, **options: object) -> Iterator[int]:
    """The bound checkout's dashboard on ``127.0.0.1:0``; yields its port.
    *options* go to ``bound_dashboard`` (a stand-in follower, a notice sink)."""
    board = resolve_board(repo)
    assert isinstance(board, HostedBoard)
    options.setdefault("on_notice", lambda line: None)
    with bound_dashboard(board, **options) as target:
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
