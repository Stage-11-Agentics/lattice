"""AC-20 (User-Agent, SPEC §9.1): every request a client sends says
``User-Agent: lattice/<version>`` (never urllib's ``Python-urllib/*``, which
bot protection such as Cloudflare's Browser Integrity Check refuses), unless
the remote's configured ``headers`` set one, which then wins. The server's
request log records the agent."""

from __future__ import annotations

import json
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import pytest

from lattice.remote import http
from lattice.remote.follower import Follower, follow_target
from lattice.server import tokens
from lattice.server.testing import make_root, running_server
from tests.test_remote.hosted import PROJECT, TOKEN_ENV, HostedEnv, make_repo, run_cli

OP_ID = "op_01J9Z0000000000000000000AB"
UA_VAR = "LATTICE_TEST_UA"


class _Env(HostedEnv):
    """``hosted_env`` with a fast stream heartbeat and every file served through
    the files endpoint (nothing inlined), so one flow touches every endpoint."""

    def start(self) -> None:
        self.handle = self._stack.enter_context(
            running_server(
                self.server_root,
                config={"limits": {"inline_file_bytes": 1}},
                heartbeat_seconds=0.2,
            )
        )
        self.write_remote()


@pytest.fixture()
def env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[_Env]:
    from lattice.remote import session

    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    root = make_root(tmp_path, projects={PROJECT: {"code": "DEM"}})
    minted = tokens.create_token(root, user="human:alice", machine="laptop", projects=[PROJECT])
    monkeypatch.setenv(TOKEN_ENV, minted["token"])
    hosted = _Env(tmp=tmp_path, server_root=root, token=minted["token"], monkeypatch=monkeypatch)
    hosted.start()
    session.reset_process_state()
    try:
        yield hosted
    finally:
        session.reset_process_state()
        hosted.stop()


def _ok(repo: Path, *args: str) -> None:
    result = run_cli(repo, *args)
    assert result.exit_code == 0, result.output


def _requests(env: _Env) -> list[dict]:
    assert env.handle is not None
    return [x for x in env.handle.log_lines if x.get("event") == "request"]


def _follow_once(env: _Env, repo: Path) -> None:
    """Run a follower on *repo*'s binding until a write made (by the CLI) while it
    holds the stream has reached its cache, then stop it and wait for the server to log
    the finished stream request."""
    remote, project = follow_target(repo)
    synced = threading.Event()
    connected = threading.Event()
    follower = Follower(
        repo,
        remote,
        project,
        on_notice=lambda line: connected.set(),
        on_sync=lambda outcome: synced.set() if follower.announced else None,
    )
    thread = threading.Thread(target=follower.run, daemon=True)
    thread.start()
    try:
        assert connected.wait(5), "the follower never connected"
        _ok(repo, "create", "While following", "--actor", "human:alice")
        assert synced.wait(5), "the follower never synced a streamed entry"
    finally:
        follower.stop()
        thread.join(timeout=5)
    assert not thread.is_alive()
    deadline = time.monotonic() + 5
    while not any(r["path"].endswith("/stream") for r in _requests(env)):
        assert time.monotonic() < deadline, "the server never logged the stream request"
        time.sleep(0.05)


def _full_flow(env: _Env, repo: Path) -> set[str]:
    """attach, write, sync, op-status, verify, info, and a follower stream;
    returns the distinct request paths (query stripped) the server logged."""
    _ok(repo, "remote", "attach", "team", "demo")
    _ok(repo, "create", "Say who you are", "--actor", "human:alice")
    _ok(repo, "sync")
    _ok(repo, "remote", "op-status", OP_ID)
    _ok(repo, "remote", "verify")
    _ok(repo, "remote", "status")
    _follow_once(env, repo)
    return {r["path"] for r in _requests(env)}


def _kinds(paths: set[str]) -> set[str]:
    found = set()
    for path in paths:
        for kind in ("/v1/info", "/ops/", "/sync", "/files/", "/stream", "/v1/projects"):
            if kind in path:
                found.add(kind)
    return found


EVERY_KIND = {"/v1/info", "/ops/", "/sync", "/files/", "/stream", "/v1/projects"}


def test_every_request_of_a_full_flow_says_lattice(env: _Env, tmp_path: Path) -> None:
    """Scenarios 1 and 4: each request the server logs carries lattice/<version>."""
    repo = make_repo(tmp_path / "repo")
    paths = _full_flow(env, repo)
    assert _kinds(paths) == EVERY_KIND, sorted(paths)
    op_status = [r for r in _requests(env) if r["path"].endswith(f"/ops/{OP_ID}")]
    assert op_status, "op-status never reached the server"
    expected = http.user_agent()
    assert expected == f"lattice/{http._client_version()}"
    agents = {(r["path"], r["user_agent"]) for r in _requests(env) if r["path"] != "/"}
    wrong = sorted(a for a in agents if a[1] != expected)
    assert not wrong, wrong
    assert not any("Python-urllib" in (r["user_agent"] or "") for r in _requests(env))


@pytest.mark.parametrize("name", ["User-Agent", "user-agent"])
def test_a_configured_user_agent_wins_on_every_request(
    env: _Env, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, name: str
) -> None:
    """Scenario 2: the remote's headers set User-Agent (any case); every request,
    the stream included, sends that value and no second User-Agent."""
    monkeypatch.setenv(UA_VAR, "custom")
    env.write_remote(headers={name: {"env": UA_VAR}})
    repo = make_repo(tmp_path / "repo")
    paths = _full_flow(env, repo)
    assert _kinds(paths) == EVERY_KIND, sorted(paths)
    agents = {r["user_agent"] for r in _requests(env) if r["path"] != "/"}
    assert agents == {"custom"}


@contextmanager
def agent_listener() -> Iterator[tuple[str, list[list[str]]]]:
    """A listener that records every ``User-Agent`` value of each request (all
    of them, so a duplicate shows) and answers as a Lattice server would."""
    seen: list[list[str]] = []

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, format: str, *args: Any) -> None:
            pass

        def do_GET(self) -> None:
            seen.append(self.headers.get_all("User-Agent") or [])
            stream = self.path.endswith("/stream")
            body = b"" if stream else b'{"ok": true, "data": {}}'
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream" if stream else "application/json")
            self.send_header("Lattice-Protocol", "1")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    httpd.daemon_threads = True
    thread = threading.Thread(target=httpd.serve_forever, args=(0.02,), daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{httpd.server_address[1]}", seen
    finally:
        httpd.shutdown()
        httpd.server_close()
        thread.join(timeout=5)


def _send_both(remote: http.Remote) -> None:
    http.request(remote, "GET", "/v1/info")
    http.open_stream(remote, "/v1/projects/demo/stream", read_timeout=1).close()


@pytest.mark.parametrize(
    ("headers", "expected"),
    [({}, None), ({"USER-AGENT": "custom"}, "custom"), ({"user-agent": "custom"}, "custom")],
    ids=["default", "configured-upper", "configured-lower"],
)
def test_the_wire_carries_one_user_agent_header(
    headers: dict[str, str], expected: str | None
) -> None:
    """On the wire: exactly one User-Agent per request, on a JSON request and on
    the stream, defaulted or configured."""
    expected = expected or http.user_agent()
    with agent_listener() as (url, seen):
        _send_both(http.Remote(alias="t", url=url, token="tok", headers=headers))
    assert seen == [[expected], [expected]]


@pytest.mark.parametrize(
    "version",
    [
        "1.2.3\r\nX-Injected: yes",
        "1.2.3\nevil",
        "1.2.3\x00\x7f\t",
        "1.2.3 (dev) é",
        "\r\n",
        "",
    ],
)
def test_the_user_agent_is_one_clean_token_whatever_the_version(
    monkeypatch: pytest.MonkeyPatch, version: str
) -> None:
    """Scenario 3: no control character, newline, space, or non-ASCII reaches
    the header, and the request still goes out."""
    monkeypatch.setattr(http, "_client_version", lambda: version)
    agent = http.user_agent()
    assert agent.startswith("lattice/") and len(agent) > len("lattice/")
    assert all(0x21 <= ord(c) <= 0x7E for c in agent), repr(agent)
    remote = http.Remote(alias="t", url="http://127.0.0.1:9", token="tok")
    req = http.build_request(remote, "GET", remote.url + "/v1/info")
    assert req.get_header("User-agent") == agent
    with agent_listener() as (url, seen):
        _send_both(http.Remote(alias="t", url=url, token="tok"))
    assert seen == [[agent], [agent]]


def test_the_version_is_the_one_the_cli_prints(tmp_path: Path) -> None:
    printed = run_cli(tmp_path, "--version").output.strip().rsplit(" ", 1)[-1]
    assert http.user_agent() == f"lattice/{printed}"


def test_local_mode_sends_no_request(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Scenario 5: a local board never reaches the remote transport."""

    def refuse(*args: object, **kwargs: object) -> None:
        raise AssertionError("local mode made a remote request")

    monkeypatch.setattr(http, "request", refuse)
    monkeypatch.setattr(http, "open_stream", refuse)
    monkeypatch.setattr(http, "build_request", refuse)
    monkeypatch.setenv("LATTICE_NO_UPDATE_CHECK", "1")
    board = tmp_path / "local"
    board.mkdir()
    _ok(
        board,
        "init",
        "--project-code",
        "LOC",
        "--actor",
        "human:alice",
        "--no-setup-claude",
        "--no-setup-agents",
        "--no-seed",
    )
    _ok(board, "create", "Local only", "--actor", "human:alice")
    shown = run_cli(board, "show", "LOC-1", "--json")
    assert shown.exit_code == 0, shown.output
    assert json.loads(shown.stdout)["data"]["title"] == "Local only"
