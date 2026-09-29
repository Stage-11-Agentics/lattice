"""AC-20 (sync, files, and the stream): the client transport never follows a
redirect, never hands credentials to another listener, and accepts only a Lattice
server's answer."""

from __future__ import annotations

import pytest

import socket
import time
from pathlib import Path

from lattice.core.errors import OpError
from lattice.remote import cache, http, stream
from lattice.remote.http import Remote
from tests.test_remote.conftest import bind
from tests.test_remote.follower_support import following
from tests.test_remote.proxies import fixed_answer, recording_listener, scripted_tcp
from tests.test_remote.stream_stub import StubSyncer, wait_for
from tests.test_remote.stub_sync_server import StubServer

TOKEN = "transport-test-bearer-secret"
PROXY_HEADERS = {
    "CF-Access-Client-Id": "proxy-id-value",
    "CF-Access-Client-Secret": "proxy-secret",
}

SYNC = "/v1/projects/demo/sync?since=0"
FILES = "/v1/projects/demo/files/tasks/x.json?sha256=00"


def _remote(url: str) -> Remote:
    return Remote(alias="team", url=url, token=TOKEN, headers=PROXY_HEADERS)


def _credential_leaked(requests: list[dict]) -> bool:
    for request in requests:
        headers = {k.lower(): v for k, v in request["headers"].items()}
        if TOKEN in headers.get("authorization", ""):
            return True
        if any(value in headers.values() for value in PROXY_HEADERS.values()):
            return True
    return False


@pytest.mark.parametrize("path,expect", [(SYNC, "json"), (FILES, "bytes")])
@pytest.mark.parametrize("status", [301, 302, 303, 307, 308])
def test_redirect_is_refused_and_no_credential_reaches_the_target(
    path: str, expect: str, status: int
) -> None:
    with recording_listener() as second:
        location = second.url + "/login?next=" + path
        with fixed_answer(status, {"Location": location}) as proxy:
            with pytest.raises(OpError) as err:
                http.request(_remote(proxy.url), "GET", path, expect=expect)
            assert len(proxy.requests) == 1
        assert second.requests == []
    assert err.value.code == "PROXY_REJECTED"
    assert f"HTTP {status}" in err.value.message
    assert "127.0.0.1" in err.value.message
    assert err.value.details == {"status": status, "location_host": "127.0.0.1"}


def test_the_first_hop_receives_the_credentials() -> None:
    """The credentials go to the configured server itself (then never further)."""
    with (
        fixed_answer(302, {"Location": "http://127.0.0.1:9/elsewhere"}) as proxy,
        pytest.raises(OpError),
    ):
        http.request(_remote(proxy.url), "GET", SYNC)
    assert _credential_leaked(proxy.requests)


@pytest.mark.parametrize("path,expect", [(SYNC, "json"), (FILES, "bytes")])
def test_an_html_login_page_is_refused(path: str, expect: str) -> None:
    page = b"<html><body>Sign in to continue</body></html>"
    with (
        fixed_answer(200, {"Content-Type": "text/html; charset=utf-8"}, page) as proxy,
        pytest.raises(OpError) as err,
    ):
        http.request(_remote(proxy.url), "GET", path, expect=expect)
    assert err.value.code == "PROXY_REJECTED"
    assert "HTTP 200" in err.value.message and "text/html" in err.value.message


def test_json_without_the_protocol_header_is_refused() -> None:
    body = b'{"ok": true, "data": {"epoch": "x"}}'
    with (
        fixed_answer(200, {"Content-Type": "application/json"}, body) as proxy,
        pytest.raises(OpError) as err,
    ):
        http.request(_remote(proxy.url), "GET", SYNC)
    assert err.value.code == "PROXY_REJECTED"


def test_a_protocol_header_with_a_non_envelope_body_is_refused() -> None:
    headers = {"Content-Type": "application/json", "Lattice-Protocol": "1"}
    with fixed_answer(200, headers, b'{"hello": 1}') as proxy, pytest.raises(OpError) as err:
        http.request(_remote(proxy.url), "GET", SYNC)
    assert err.value.code == "PROXY_REJECTED"


def test_another_protocol_is_a_mismatch() -> None:
    headers = {"Content-Type": "application/json", "Lattice-Protocol": "2"}
    with (
        fixed_answer(200, headers, b'{"ok": true, "data": {}}') as proxy,
        pytest.raises(OpError) as err,
    ):
        http.request(_remote(proxy.url), "GET", SYNC)
    assert err.value.code == "PROTOCOL_MISMATCH"


def test_an_error_envelope_becomes_a_server_error() -> None:
    headers = {"Content-Type": "application/json", "Lattice-Protocol": "1", "Retry-After": "3"}
    body = b'{"ok": false, "error": {"code": "BOARD_BUSY", "message": "busy"}}'
    with fixed_answer(503, headers, body) as proxy, pytest.raises(http.ServerError) as err:
        http.request(_remote(proxy.url), "GET", SYNC)
    assert (err.value.code, err.value.status, err.value.retry_after) == ("BOARD_BUSY", 503, 3.0)


# A gateway's own 502, 503 or 504 is unreachable instead (test_gateway_failures.py).
@pytest.mark.parametrize("status", [500, 501, 505])
@pytest.mark.parametrize(
    "content_type,body",
    [("text/html", b"<html>Bad gateway</html>"), ("application/json", b'{"message": "down"}')],
)
def test_a_non_lattice_error_page_is_refused(status: int, content_type: str, body: bytes) -> None:
    with (
        fixed_answer(status, {"Content-Type": content_type}, body) as proxy,
        pytest.raises(OpError) as err,
    ):
        http.request(_remote(proxy.url), "GET", SYNC)
    assert err.value.code == "PROXY_REJECTED"
    assert f"HTTP {status}" in err.value.message


def test_a_lattice_5xx_without_an_envelope_is_refused() -> None:
    headers = {"Content-Type": "text/html", "Lattice-Protocol": "1"}
    with (
        fixed_answer(500, headers, b"<html>oops</html>") as proxy,
        pytest.raises(OpError) as err,
    ):
        http.request(_remote(proxy.url), "GET", SYNC)
    assert err.value.code == "PROXY_REJECTED"


def test_a_refused_connection_is_unreachable_and_unsent() -> None:
    with fixed_answer(200, {}) as proxy:
        url = proxy.url
    with pytest.raises(http.Unreachable) as err:
        http.request(_remote(url), "GET", SYNC)
    assert err.value.sent is False


def test_credentials_are_unredirected_headers() -> None:
    req = http.build_request(_remote("http://127.0.0.1:1"), "GET", "http://127.0.0.1:1/v1/info")
    assert req.unredirected_hdrs["Authorization"] == f"Bearer {TOKEN}"
    for name, value in PROXY_HEADERS.items():
        assert req.unredirected_hdrs[name.capitalize()] == value
        assert name.capitalize() not in req.headers
    assert "Authorization" not in req.headers
    assert req.headers["Lattice-protocol"] == "1"


@pytest.mark.parametrize(
    "href,ok",
    [
        ("/v1/projects/demo/files/tasks/a.json?sha256=1", True),
        ("http://evil.example/v1/x", False),
        ("//evil.example/v1/x", False),
        ("https://127.0.0.1:1/v1/x", False),
        ("http://127.0.0.1:1/v1/projects/demo/files/a?sha256=1", False),  # same origin, absolute
        ("v1/relative", False),
        ("/\\evil.example/x", False),
    ],
)
def test_only_same_origin_relative_hrefs(href: str, ok: bool) -> None:
    remote = _remote("http://127.0.0.1:1")
    assert (http.href_url(remote, href) is not None) is ok


# ---------------------------------------------------------------------------
# Through the client: catch_up's sync and files requests (AC-20, H-10b part)
# ---------------------------------------------------------------------------

LOGIN_PAGE = (200, {"Content-Type": "text/html"}, b"<html>Sign in</html>")


def _bind_with_proxy_headers(client: Path, url: str, monkeypatch: pytest.MonkeyPatch) -> None:
    bind(client, url, TOKEN, monkeypatch)
    monkeypatch.setenv("PROXY_ID", PROXY_HEADERS["CF-Access-Client-Id"])
    monkeypatch.setenv("PROXY_SECRET", PROXY_HEADERS["CF-Access-Client-Secret"])
    monkeypatch.setenv(
        "LATTICE_REMOTE_TEAM_HEADERS",
        '{"CF-Access-Client-Id": "PROXY_ID", "CF-Access-Client-Secret": "PROXY_SECRET"}',
    )


def test_catch_up_refuses_a_redirected_sync(
    tmp_path: Path, stub: StubServer, monkeypatch: pytest.MonkeyPatch
) -> None:
    client = tmp_path / "client"
    with recording_listener() as second:
        with fixed_answer(302, {"Location": second.url + "/login"}) as proxy:
            _bind_with_proxy_headers(client, proxy.url, monkeypatch)
            with pytest.raises(OpError) as err:
                cache.catch_up(client)
            assert _credential_leaked(proxy.requests)  # sent to the configured URL only
        assert second.requests == []
    assert err.value.code == "PROXY_REJECTED"
    assert not (client / ".lattice" / "cache" / "state.json").exists()


def test_catch_up_refuses_an_html_sync_answer(
    tmp_path: Path, stub: StubServer, monkeypatch: pytest.MonkeyPatch
) -> None:
    client = tmp_path / "client"
    with fixed_answer(*LOGIN_PAGE) as proxy:
        _bind_with_proxy_headers(client, proxy.url, monkeypatch)
        with pytest.raises(OpError) as err:
            cache.catch_up(client)
    assert err.value.code == "PROXY_REJECTED"


@pytest.mark.parametrize("kind", ["redirect", "html"])
def test_catch_up_refuses_a_bad_files_answer(
    tmp_path: Path, stub: StubServer, monkeypatch: pytest.MonkeyPatch, kind: str
) -> None:
    client = tmp_path / "client"
    _bind_with_proxy_headers(client, stub.url, monkeypatch)
    monkeypatch.setenv("LATTICE_REMOTE_TEAM_TOKEN", stub.token)
    stub.inline_file_bytes = 10  # every file travels through the files endpoint
    with recording_listener() as second:
        stub.fault.raw_files = (
            (302, {"Location": second.url + "/steal"}, b"") if kind == "redirect" else LOGIN_PAGE
        )
        with pytest.raises(OpError) as err:
            cache.catch_up(client)
        assert second.requests == []
    assert err.value.code == "PROXY_REJECTED"
    assert any(kind == "files" for kind, _ in stub.arrivals)
    assert not (client / ".lattice" / "cache" / "state.json").exists()


# ---------------------------------------------------------------------------
# Malformed redirects and the two time budgets
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("location", ["http://[bad", "http://[::1", "::::", ""])
def test_a_malformed_redirect_location_is_still_refused(location: str) -> None:
    headers = {"Location": location} if location else {}
    with fixed_answer(302, headers) as proxy, pytest.raises(OpError) as err:
        http.request(_remote(proxy.url), "GET", SYNC)
    assert err.value.code == "PROXY_REJECTED"
    assert "HTTP 302" in err.value.message


def _lattice_head(length: int) -> bytes:
    return (
        "HTTP/1.1 200 OK\r\n"
        "Content-Type: application/json\r\n"
        "Lattice-Protocol: 1\r\n"
        f"Content-Length: {length}\r\n"
        "Connection: close\r\n\r\n"
    ).encode()


def test_a_dribbled_header_cannot_stretch_the_response_start_budget() -> None:
    def dribble(conn: socket.socket) -> None:
        conn.sendall(b"HTTP/1.1 200 OK\r\n")
        for _ in range(50):
            conn.sendall(b"X-Pad: a\r\n")
            time.sleep(0.1)

    policy = http.Policy(connect_seconds=0.5, response_seconds=0.6)
    with scripted_tcp(dribble) as url:
        started = time.monotonic()
        with pytest.raises(http.Unreachable) as err:
            http.request(_remote(url), "GET", SYNC, policy=policy)
        elapsed = time.monotonic() - started
    assert elapsed < 1.5
    assert "no answer within 0.6 s" in err.value.reason


def test_a_slow_but_valid_body_gets_the_body_budget() -> None:
    body = b'{"ok": true, "data": {"slow": true}}'

    def slow_body(conn: socket.socket) -> None:
        conn.sendall(_lattice_head(len(body)) + body[:10])
        time.sleep(1.0)  # longer than the whole response-start budget
        conn.sendall(body[10:])

    policy = http.Policy(connect_seconds=0.5, response_seconds=0.6)
    with scripted_tcp(slow_body) as url:
        response = http.request(_remote(url), "GET", SYNC, policy=policy)
    assert response.data() == {"slow": True}


def test_a_stalled_body_fails_at_its_deadline(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(http, "body_budget_seconds", lambda length: 0.5)

    def stall(conn: socket.socket) -> None:
        conn.sendall(_lattice_head(100) + b"{")
        time.sleep(3)

    with scripted_tcp(stall) as url:
        started = time.monotonic()
        with pytest.raises(http.Unreachable):
            http.request(_remote(url), "GET", SYNC)
        assert time.monotonic() - started < 2.0


# ---------------------------------------------------------------------------
# AC-20 (stream part, H-10c): the change stream rides the same transport
# ---------------------------------------------------------------------------

STREAM = "/v1/projects/demo/stream"


@pytest.mark.parametrize("status", [301, 302, 303, 307, 308])
def test_redirecting_proxy_fails_the_stream_with_proxy_rejected(status) -> None:
    with recording_listener() as second:
        location = second.url + "/login?next=" + STREAM
        with fixed_answer(status, {"Location": location}) as proxy:
            with pytest.raises(OpError) as err:
                stream.open_stream(
                    _remote(proxy.url), "demo", last_event_id="ep_1:3:" + "0" * 32, timeout=2
                )
            assert err.value.code == "PROXY_REJECTED"
            assert str(status) in err.value.message and "127.0.0.1" in err.value.message
            # The proxy is the configured URL, so it saw the credentials ...
            assert proxy.requests and TOKEN in proxy.requests[0]["headers"].get(
                "Authorization", ""
            )
        # ... and the second listener received nothing at all.
        assert second.requests == []
        assert not _credential_leaked(second.requests)


def test_redirect_on_info_is_rejected_too() -> None:
    with recording_listener() as second:
        with fixed_answer(302, {"Location": second.url + "/"}) as proxy:
            with pytest.raises(OpError) as err:
                stream.get_info(_remote(proxy.url))
            assert err.value.code == "PROXY_REJECTED"
        assert second.requests == []


@pytest.mark.parametrize(
    ("status", "headers", "body", "code", "fragments"),
    [
        (
            200,
            {"Content-Type": "text/html"},
            b"<html>Sign in</html>",
            "PROXY_REJECTED",
            ["200", "text/html"],
        ),
        (
            500,
            {"Content-Type": "text/html"},
            b"<html>Internal error</html>",
            "PROXY_REJECTED",
            ["500"],
        ),
        (
            502,
            {"Content-Type": "text/html"},
            b"<html>Bad gateway</html>",
            "SERVER_UNREACHABLE",
            ["502"],
        ),
        (
            200,
            {"Content-Type": "application/json", "Lattice-Protocol": "1"},
            b'{"ok": true, "data": {}}',
            "PROXY_REJECTED",
            ["application/json"],
        ),
        (200, {"Content-Type": "text/event-stream"}, b"", "PROXY_REJECTED", ["200"]),
        (
            200,
            {"Content-Type": "text/event-stream", "Lattice-Protocol": "2"},
            b"",
            "PROTOCOL_MISMATCH",
            [],
        ),
        (
            401,
            {"Content-Type": "application/json", "Lattice-Protocol": "1"},
            b'{"ok": false, "error": {"code": "UNAUTHENTICATED", "message": "bad token"}}',
            "UNAUTHENTICATED",
            ["bad token"],
        ),
    ],
)
def test_only_a_lattice_event_stream_counts(status, headers, body, code, fragments) -> None:
    with fixed_answer(status, headers, body) as proxy:
        with pytest.raises(OpError) as err:
            stream.open_stream(_remote(proxy.url), "demo", last_event_id=None, timeout=2)
    assert err.value.code == code
    for fragment in fragments:
        assert fragment in err.value.message


def test_stream_connection_refused_is_unreachable() -> None:
    with fixed_answer(200, {}) as server:
        url = server.url
    with pytest.raises(OpError) as err:
        stream.open_stream(_remote(url), "demo", last_event_id=None, timeout=1)
    assert err.value.code == "SERVER_UNREACHABLE"


def test_follower_behind_a_redirecting_proxy_reports_proxy_rejected(tmp_path, stream_stub) -> None:
    with recording_listener() as second:
        with fixed_answer(302, {"Location": second.url + "/login"}) as proxy:
            syncer = StubSyncer(stream_stub.url)
            with following(tmp_path, proxy.url, syncer, heartbeat_seconds=0.2) as follower:
                assert wait_for(lambda: follower.last_stream_error is not None, 2)
                assert follower.last_stream_error.code == "PROXY_REJECTED"
                assert follower.stream_live_until is None
        assert second.requests == []
