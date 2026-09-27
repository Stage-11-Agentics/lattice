"""The client's retry policy for one operation call (SPEC §8.6, "Client retries").

A scripted listener answers each attempt; a fake clock makes the waits free.
The kill-and-restart proof against a real server is H-22's (AC-46).
"""

from __future__ import annotations

import json
import socket
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

import pytest

from lattice.core.errors import OpError
from lattice.remote import client, http

OP_ID = "op_01J9Z0000000000000000000CD"
BODY = {"op_id": OP_ID, "params": {"title": "x"}}
OK = (200, {}, {"ok": True, "data": {"result": {"events": []}, "seq": 7, "op_id": OP_ID}})


def _error(status: int, code: str, retry_after: str | None = None) -> tuple:
    headers = {"Retry-After": retry_after} if retry_after else {}
    return (status, headers, {"ok": False, "error": {"code": code, "message": code.lower()}})


@contextmanager
def scripted(answers: list[tuple]) -> Iterator[dict[str, Any]]:
    """Answer attempt *n* with ``answers[n]`` (the last one repeats); ``"hang"``
    reads the request and never answers."""
    seen: dict[str, Any] = {"bodies": []}
    release = threading.Event()

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, format: str, *args: Any) -> None:
            pass

        def do_POST(self) -> None:  # noqa: N802
            length = int(self.headers.get("Content-Length") or 0)
            seen["bodies"].append(json.loads(self.rfile.read(length)))
            answer = answers[min(len(seen["bodies"]) - 1, len(answers) - 1)]
            if answer == "hang":
                release.wait(10)
                return
            status, headers, body = answer
            raw = json.dumps(body).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Lattice-Protocol", "1")
            for name, value in headers.items():
                self.send_header(name, value)
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

    httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    httpd.daemon_threads = True
    thread = threading.Thread(target=httpd.serve_forever, args=(0.02,), daemon=True)
    thread.start()
    seen["url"] = f"http://127.0.0.1:{httpd.server_address[1]}"
    try:
        yield seen
    finally:
        release.set()
        httpd.shutdown()
        httpd.server_close()
        thread.join(timeout=5)


@pytest.fixture()
def clock(monkeypatch: pytest.MonkeyPatch) -> list[float]:
    """A fake clock: each sleep advances it; returns the list of waits."""
    now = [1000.0]
    waits: list[float] = []

    def sleep(seconds: float) -> None:
        waits.append(seconds)
        now[0] += seconds

    monkeypatch.setattr(client, "_now", lambda: now[0])
    monkeypatch.setattr(client, "_sleep", sleep)
    return waits


def _remote(url: str, retry_seconds: float = 30.0) -> http.Remote:
    return http.Remote(alias="team", url=url, token="t", retry_seconds=retry_seconds)


def _post(url: str, retry_seconds: float = 30.0) -> dict:
    return client.post_operation(_remote(url, retry_seconds), "demo", "task.create", dict(BODY))


@pytest.mark.parametrize(
    "transient",
    [
        _error(429, "RATE_LIMITED"),
        _error(503, "BOARD_BUSY"),
        _error(502, "BAD_GATEWAY"),
        _error(504, "GATEWAY_TIMEOUT"),
    ],
)
def test_transient_answers_are_retried_with_the_same_op_id(
    clock: list[float], transient: tuple, capsys: pytest.CaptureFixture
) -> None:
    with scripted([transient, transient, OK]) as server:
        data = _post(server["url"])
    assert data["seq"] == 7
    retrying = [line for line in capsys.readouterr().err.splitlines() if "retrying" in line]
    assert retrying == [
        f"lattice: team: HTTP {transient[0]} {transient[2]['error']['code']}; retrying "
        f"operation {OP_ID} for up to 30 s"
    ]
    assert [b["op_id"] for b in server["bodies"]] == [OP_ID] * 3
    assert clock == [0.5, 1.0]  # backoff from 0.5 s, doubling


def test_retry_after_is_honored(clock: list[float]) -> None:
    with scripted([_error(503, "BOARD_BUSY", retry_after="2"), OK]) as server:
        _post(server["url"])
    assert clock == [2.0]


def test_backoff_caps_at_five_seconds(clock: list[float]) -> None:
    busy = _error(503, "BOARD_BUSY")
    with scripted([busy] * 6 + [OK]) as server:
        _post(server["url"])
    assert clock == [0.5, 1.0, 2.0, 4.0, 5.0, 5.0]


@pytest.mark.parametrize(
    "final",
    [_error(503, "BOARD_UNAVAILABLE"), _error(500, "INTERNAL_ERROR"), _error(409, "CONFLICT")],
)
def test_other_errors_are_not_retried(clock: list[float], final: tuple) -> None:
    with scripted([final]) as server, pytest.raises(OpError) as exc:
        _post(server["url"])
    assert exc.value.code == final[2]["error"]["code"]
    assert len(server["bodies"]) == 1
    assert clock == []


def test_giving_up_after_the_server_answered_is_outcome_unknown(clock: list[float]) -> None:
    with scripted([_error(503, "BOARD_BUSY")]) as server, pytest.raises(OpError) as exc:
        _post(server["url"], retry_seconds=3)
    assert exc.value.code == "OUTCOME_UNKNOWN"
    assert OP_ID in exc.value.message
    assert f"lattice remote op-status {OP_ID}" in exc.value.message
    assert sum(clock) <= 3


def test_a_lost_response_is_outcome_unknown(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(client, "OP_POLICY", http.Policy(1.0, 0.3))
    with scripted(["hang"]) as server, pytest.raises(OpError) as exc:
        _post(server["url"], retry_seconds=0)
    assert exc.value.code == "OUTCOME_UNKNOWN"
    assert len(server["bodies"]) == 1


def test_never_connecting_is_server_unreachable(clock: list[float]) -> None:
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()  # nothing listens here
    with pytest.raises(OpError) as exc:
        _post(f"http://127.0.0.1:{port}", retry_seconds=2)
    assert exc.value.code == "SERVER_UNREACHABLE"
    assert "Nothing was written" in exc.value.message
    assert clock == [0.5, 1.0]
