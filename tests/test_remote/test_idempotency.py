"""AC-46 (H-22, client part): retries through the real client (SPEC §8.6, §9.5).

Every case runs the CLI in a bound checkout against an in-process server,
through :class:`Dropper`: an HTTP proxy that forwards an operation request,
lets the server commit it, and then drops the response (closes the
connection without answering), optionally after an intervening write by
another client. ``HostedBoard.execute`` retries with the same ``op_id``; the
server replays the stored result. The command must succeed, apply once, and
render what it would have rendered.

The kill-and-restart case (a real server process killed right after a
commit) is in ``tests/torture/test_client_restart.py`` (marker torture).
"""

from __future__ import annotations

import http.client
import json
import threading
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import pytest

from lattice.remote import acked
from lattice.server import admin, tokens
from tests.test_remote.hosted import (
    PROJECT,
    HostedEnv,
    SpawnRecorder,
    make_repo,
    run_cli,
    walk_to,
)

HOP_BY_HOP = {"connection", "keep-alive", "transfer-encoding", "content-length"}


class Dropper:
    """Forwards everything to the server; drops the response of the next
    ``drops`` operation POSTs after they commit, running ``meanwhile`` first."""

    def __init__(self, target: str) -> None:
        self.target = target.removeprefix("http://")
        self.drops = 0
        self.dropped: list[str] = []
        self.meanwhile: Callable[[], None] | None = None
        self.url = ""

    def forward(self, handler: BaseHTTPRequestHandler) -> None:
        length = int(handler.headers.get("Content-Length") or 0)
        body = handler.rfile.read(length) if length else None
        headers = {k: v for k, v in handler.headers.items() if k.lower() not in HOP_BY_HOP}
        conn = http.client.HTTPConnection(self.target, timeout=60)
        conn.request(handler.command, handler.path, body=body, headers=headers)
        response = conn.getresponse()
        data = response.read()
        conn.close()
        is_op = handler.command == "POST" and "/ops/" in handler.path
        if is_op and self.drops > 0:
            self.drops -= 1
            self.dropped.append(json.loads(body or b"{}").get("op_id"))
            if self.meanwhile is not None:
                self.meanwhile()
            handler.close_connection = True
            return  # committed on the server; the client never hears back
        handler.send_response(response.status)
        for name, value in response.getheaders():
            if name.lower() not in HOP_BY_HOP:
                handler.send_header(name, value)
        handler.send_header("Content-Length", str(len(data)))
        handler.end_headers()
        handler.wfile.write(data)


@contextmanager
def dropping_proxy(env: HostedEnv) -> Iterator[Dropper]:
    dropper = Dropper(env.url)

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, format: str, *args: Any) -> None:
            pass

        def do_GET(self) -> None:  # noqa: N802
            dropper.forward(self)

        do_POST = do_GET  # noqa: N815

    httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    httpd.daemon_threads = True
    thread = threading.Thread(target=httpd.serve_forever, args=(0.02,), daemon=True)
    thread.start()
    dropper.url = f"http://127.0.0.1:{httpd.server_address[1]}"
    env.write_remote(url=dropper.url, retry_seconds=10)
    try:
        yield dropper
    finally:
        env.settings.pop("url", None)
        env.write_remote(retry_seconds=1)
        httpd.shutdown()
        httpd.server_close()
        thread.join(timeout=5)


def journal(env: HostedEnv) -> list[dict]:
    raw = (env.board / "hosted" / "journal.jsonl").read_text()
    return [json.loads(line) for line in raw.splitlines()]


def other_client_write(env: HostedEnv) -> Callable[[], None]:
    """An intervening write by another client (its own token)."""
    other = tokens.create_token(
        env.server_root, user="human:bob", machine="m2", projects=[PROJECT]
    )

    def write() -> None:
        assert env.handle is not None
        status, _, body = env.handle.op(
            PROJECT, "task.create", {"title": "meanwhile"}, token=other["token"]
        )
        assert status == 200, body

    return write


@pytest.fixture()
def repo(hosted_env: HostedEnv, tmp_path: Path) -> Path:
    repo = make_repo(tmp_path / "repo")
    assert run_cli(repo, "remote", "attach", "team", PROJECT).exit_code == 0
    assert run_cli(repo, "create", "First", "--actor", "agent:dev").exit_code == 0
    return repo


def _replayed_once(env: HostedEnv, op_id: str) -> None:
    lines = journal(env)
    assert [x["op_id"] for x in lines].count(op_id) == 1
    assert env.handle is not None
    replays = [
        x
        for x in env.handle.log_lines
        if x.get("event") == "request" and x.get("op_id") == op_id and x.get("replayed")
    ]
    assert replays, "the retry was not answered from the stored result"


CASES: dict[str, Callable[[Path], list[str]]] = {
    "comment": lambda repo: [
        "comment",
        "DEM-1",
        "a lost-response comment",
        "--actor",
        "agent:dev",
    ],
    "session start": lambda repo: [
        "session",
        "start",
        "--model",
        "opus",
        "--framework",
        "claude-code",
        "--name",
        "Worker",
    ],
    "resource acquire": lambda repo: ["resource", "acquire", "db", "--actor", "agent:dev"],
    "no-op status": lambda repo: ["status", "DEM-1", "backlog", "--actor", "agent:dev"],
    "config": lambda repo: ["set-project-code", "DEQ", "--force"],
}


#: Every family plain and --json, except set-project-code, which has no --json.
VARIANTS = [
    pytest.param(family, as_json, id=f"{family}-{'json' if as_json else 'plain'}")
    for family in CASES
    for as_json in (False, True)
    if not (family == "config" and as_json)
]


@pytest.mark.parametrize(("family", "as_json"), VARIANTS)
def test_a_lost_response_is_retried_and_replayed(
    family: str, as_json: bool, hosted_env: HostedEnv, repo: Path
) -> None:
    if family == "resource acquire":
        assert run_cli(repo, "resource", "create", "db", "--actor", "agent:dev").exit_code == 0
    args = CASES[family](repo) + (["--json"] if as_json else [])
    with dropping_proxy(hosted_env) as dropper:
        dropper.drops = 1
        dropper.meanwhile = other_client_write(hosted_env)
        result = run_cli(repo, *args)
    assert result.exit_code == 0, result.output
    (op_id,) = dropper.dropped
    _replayed_once(hosted_env, op_id)
    if as_json:
        assert json.loads(result.stdout)["ok"] is True
    else:
        assert result.stdout.strip()
    # The acknowledged write is recorded once, with the op_id it was retried under.
    ack = [x for x in acked.read(repo / ".lattice" / "cache") if x["op_id"] == op_id]
    assert len(ack) == 1 and ack[0]["project"] == PROJECT


def test_a_replayed_create_renders_exactly_the_committed_result(
    hosted_env: HostedEnv, repo: Path
) -> None:
    with dropping_proxy(hosted_env) as dropper:
        dropper.drops = 1
        dropper.meanwhile = other_client_write(hosted_env)
        result = run_cli(repo, "create", "Lost response", "--actor", "agent:dev", "--json")
    assert result.exit_code == 0, result.output
    (op_id,) = dropper.dropped
    _replayed_once(hosted_env, op_id)
    shown = run_cli(repo, "remote", "op-status", op_id, "--json")
    stored = json.loads(shown.stdout)["data"]["result"]["task"]
    assert json.loads(result.stdout)["data"] == stored  # the stored result, verbatim


def test_a_status_that_records_an_auto_review_uses_two_op_ids(
    hosted_env: HostedEnv, repo: Path, spawns: SpawnRecorder
) -> None:
    admin.set_project_config(
        hosted_env.server_root, PROJECT, {"auto_code_review_on_transition": "true"}
    )
    walk_to(repo, "DEM-1", "in_planning", "planned", "in_progress")
    with dropping_proxy(hosted_env) as dropper:
        dropper.drops = 1
        result = run_cli(repo, "status", "DEM-1", "review", "--actor", "agent:dev", "--json")
    assert result.exit_code == 0, result.output
    (status_op,) = dropper.dropped
    _replayed_once(hosted_env, status_op)
    # The walk's plan review ran earlier; this transition's code review ran once.
    assert spawns.review_types.count("code-review") == 1
    tail = journal(hosted_env)[-2:]
    assert [x["op"] for x in tail] == ["task.status", "task.record_auto_review"]
    assert tail[0]["op_id"] == status_op and tail[1]["op_id"] != status_op


def test_outcome_unknown_names_the_op_and_op_status_finds_it(
    hosted_env: HostedEnv, repo: Path
) -> None:
    with dropping_proxy(hosted_env) as dropper:
        dropper.drops = 10**6  # never answers: down past retry_seconds
        hosted_env.write_remote(url=dropper.url, retry_seconds=1)
        result = run_cli(repo, "create", "Unknown", "--actor", "agent:dev", "--json")
    assert result.exit_code == 1
    error = json.loads(result.stdout)["error"]
    assert error["code"] == "OUTCOME_UNKNOWN"
    op_id = dropper.dropped[0]
    assert op_id in error["message"] and "lattice remote op-status" in error["message"]
    assert set(dropper.dropped) == {op_id}  # every retry reused it
    shown = run_cli(repo, "remote", "op-status", op_id, "--json")
    assert json.loads(shown.stdout)["data"]["state"] == "committed"
    assert [x["op_id"] for x in journal(hosted_env)].count(op_id) == 1
    # Not acknowledged, so not recorded for verify.
    assert op_id not in {x["op_id"] for x in acked.read(repo / ".lattice" / "cache")}


def test_remote_verify_confirms_every_acknowledged_write(
    hosted_env: HostedEnv, repo: Path
) -> None:
    assert run_cli(repo, "comment", "DEM-1", "hello", "--actor", "agent:dev").exit_code == 0
    lines = acked.read(repo / ".lattice" / "cache")
    assert len(lines) == 2  # the create and the comment
    assert all(x["epoch"] and isinstance(x["seq"], int) for x in lines)
    plain = run_cli(repo, "remote", "verify")
    assert plain.exit_code == 0, plain.output
    assert "2 acknowledged write(s) checked; the server holds all." in plain.stdout
    as_json = run_cli(repo, "remote", "verify", "--json")
    data = json.loads(as_json.stdout)["data"]
    assert data == {"checked": 2, "confirmed": 2, "dropped": 0, "missing": []}
    assert all("confirmed_at" in x for x in acked.read(repo / ".lattice" / "cache"))


def test_remote_verify_drops_lines_older_than_90_days(
    hosted_env: HostedEnv, repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from datetime import timedelta

    later = acked._now() + timedelta(days=91)
    monkeypatch.setattr(acked, "_now", lambda: later)
    data = json.loads(run_cli(repo, "remote", "verify", "--json").stdout)["data"]
    assert data == {"checked": 0, "confirmed": 0, "dropped": 1, "missing": []}
    assert acked.read(repo / ".lattice" / "cache") == []


def test_remote_verify_unreachable_changes_nothing(hosted_env: HostedEnv, repo: Path) -> None:
    path = repo / ".lattice" / "cache" / acked.ACKED_FILE
    before = path.read_bytes()
    with hosted_env.stopped():
        result = run_cli(repo, "remote", "verify", "--json")
        assert result.exit_code == 1
        assert json.loads(result.stdout)["error"]["code"] == "SERVER_UNREACHABLE"
    assert path.read_bytes() == before


def test_a_torn_acked_line_is_skipped(tmp_path: Path) -> None:
    cache = tmp_path / "cache"
    cache.mkdir()
    acked.record(cache, op_id="op_01J9Z0000000000000000000AA", project="p", epoch="ep_x", seq=1)
    with open(cache / acked.ACKED_FILE, "ab") as fh:
        fh.write(b'{"op_id": "op_torn')
    assert [x["op_id"] for x in acked.read(cache)] == ["op_01J9Z0000000000000000000AA"]
