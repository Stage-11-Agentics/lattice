"""The test client's keep-alive retries only a stale connection (H-13b round 4):
a GET whose reused connection closed before any response byte is sent again
once on a fresh connection; a truncated or malformed response, and any POST,
propagate."""

from __future__ import annotations

import http.client
import socket
import threading
from collections.abc import Callable
from dataclasses import dataclass

import pytest

from tests.test_server.web_client import WebClient

OK = b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\nContent-Type: text/plain\r\n\r\nok"


def _read_request(conn: socket.socket) -> bytes:
    data = b""
    while b"\r\n\r\n" not in data:
        chunk = conn.recv(65536)
        if not chunk:
            return data
        data += chunk
    head, _, rest = data.partition(b"\r\n\r\n")
    for line in head.split(b"\r\n"):
        if line.lower().startswith(b"content-length:"):
            need = int(line.split(b":")[1]) - len(rest)
            while need > 0:
                chunk = conn.recv(need)
                if not chunk:
                    break
                need -= len(chunk)
    return data


class FakeServer:
    """Accepts connections on 127.0.0.1; the n-th runs ``scripts[n](conn)``."""

    def __init__(self, scripts: list[Callable[[socket.socket], None]]) -> None:
        self.sock = socket.socket()
        self.sock.bind(("127.0.0.1", 0))
        self.sock.listen()
        self.port = self.sock.getsockname()[1]
        self.url = f"http://127.0.0.1:{self.port}"
        self.scripts = scripts
        self.accepted = 0
        self.thread = threading.Thread(target=self._serve, daemon=True)
        self.thread.start()

    def _serve(self) -> None:
        for script in self.scripts:
            try:
                conn, _ = self.sock.accept()
            except OSError:
                return
            self.accepted += 1
            try:
                script(conn)
            finally:
                conn.close()

    def close(self) -> None:
        self.sock.close()


def _serve_ok_then(after: Callable[[socket.socket], None]) -> Callable[[socket.socket], None]:
    def script(conn: socket.socket) -> None:
        _read_request(conn)
        conn.sendall(OK)
        after(conn)

    return script


def _close_idle(conn: socket.socket) -> None:
    """Close the kept-alive connection while it is idle (the stale case)."""


def _answer(raw: bytes) -> Callable[[socket.socket], None]:
    def after(conn: socket.socket) -> None:
        _read_request(conn)
        conn.sendall(raw)

    return after


def _ok(conn: socket.socket) -> None:
    _read_request(conn)
    conn.sendall(OK)


@dataclass
class _Server:
    url: str
    port: int


def _client(server: FakeServer) -> WebClient:
    return WebClient(_Server(server.url, server.port), keep_alive=True)  # type: ignore[arg-type]


def _stale_pair() -> FakeServer:
    return FakeServer([_serve_ok_then(_close_idle), _ok])


def test_a_get_on_a_server_closed_idle_connection_retries_once() -> None:
    server = _stale_pair()
    web = _client(server)
    assert web.get("/one").status == 200
    assert web.get("/two").status == 200  # the reused connection was stale
    assert server.accepted == 2
    server.close()


def test_a_post_on_a_stale_connection_is_never_retried() -> None:
    server = _stale_pair()
    web = _client(server)
    assert web.get("/one").status == 200
    with pytest.raises((http.client.RemoteDisconnected, BrokenPipeError, ConnectionResetError)):
        web.post_json("/two", {"x": 1}, origin=None)
    assert server.accepted == 1
    server.close()


@pytest.mark.parametrize(
    ("raw", "error"),
    [
        (b"HTTP/1.1 200 OK\r\nContent-Length: 100\r\n\r\nonly ten b", http.client.IncompleteRead),
        (b"garbage\r\n\r\n", http.client.BadStatusLine),
        (b"HTTP/1.1 200 OK\r\nX: " + b"a" * 70000 + b"\r\n\r\n", http.client.LineTooLong),
    ],
    ids=["truncated", "bad-status-line", "line-too-long"],
)
def test_a_protocol_failure_on_a_reused_connection_propagates(raw: bytes, error) -> None:
    server = FakeServer([_serve_ok_then(_answer(raw)), _ok])
    web = _client(server)
    assert web.get("/one").status == 200
    with pytest.raises(error):
        web.get("/two")
    assert server.accepted == 1  # never sent again on a fresh connection
    server.close()


def test_a_truncated_first_response_propagates() -> None:
    server = FakeServer([_answer(b"HTTP/1.1 200 OK\r\nContent-Length: 100\r\n\r\nshort"), _ok])
    web = _client(server)
    with pytest.raises(http.client.IncompleteRead):
        web.get("/one")
    assert server.accepted == 1
    server.close()


def _reset(conn: socket.socket) -> None:
    """Close with RST (SO_LINGER 0) instead of FIN."""
    import struct

    conn.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, struct.pack("ii", 1, 0))


def test_a_reset_after_the_status_line_propagates() -> None:
    """Round 4 finding 1: a reset during the headers is a protocol failure, not a
    stale connection, however the error is spelled."""

    def after(conn: socket.socket) -> None:
        _read_request(conn)
        conn.sendall(b"HTTP/1.1 200 OK\r\nContent-Le")
        import time

        time.sleep(0.2)  # the status line has reached the client
        _reset(conn)

    server = FakeServer([_serve_ok_then(after), _ok])
    web = _client(server)
    assert web.get("/one").status == 200
    with pytest.raises((ConnectionResetError, http.client.HTTPException)) as caught:
        web.get("/two")
    assert not isinstance(caught.value, http.client.RemoteDisconnected)
    assert server.accepted == 1  # never sent again on a fresh connection
    server.close()


def test_an_idle_connection_reset_before_any_byte_retries_once() -> None:
    def after(conn: socket.socket) -> None:
        _read_request(conn)  # the next request arrives, then the server resets
        _reset(conn)

    server = FakeServer([_serve_ok_then(after), _ok])
    web = _client(server)
    assert web.get("/one").status == 200
    assert web.get("/two").status == 200
    assert server.accepted == 2
    server.close()
