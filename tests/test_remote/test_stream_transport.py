"""AC-20 (stream part): the stream's transport never follows a redirect and
never leaks a credential, and only a Lattice server's event stream counts."""

from __future__ import annotations

import pytest

from lattice.core.errors import OpError
from lattice.remote.stream import get_info, open_stream
from tests.test_remote.follower_support import following
from tests.test_remote.stream_stub import (
    TOKEN,
    OneShotServer,
    RecordingListener,
    StubSyncer,
    endpoint,
    wait_for,
)

PROXY_HEADERS = {
    "CF-Access-Client-Id": "proxy-id-value",
    "CF-Access-Client-Secret": "proxy-secret",
}


def _leaked(requests: list[dict[str, str]]) -> list[dict[str, str]]:
    bad = []
    for headers in requests:
        lowered = {k.lower(): v for k, v in headers.items()}
        if "authorization" in lowered or any(k.lower() in lowered for k in PROXY_HEADERS):
            bad.append(headers)
        elif any(TOKEN in v or "proxy-secret" in v for v in headers.values()):
            bad.append(headers)
    return bad


@pytest.mark.parametrize("status", [301, 302, 303, 307, 308])
def test_redirecting_proxy_fails_the_stream_with_proxy_rejected(status) -> None:
    with RecordingListener().running() as second:
        location = second.url + "/login?next=/v1/projects/demo/stream"
        with OneShotServer(status, {"Location": location}).running() as proxy:
            ep = endpoint(proxy.url, **PROXY_HEADERS)
            with pytest.raises(OpError) as info:
                open_stream(ep, last_event_id="ep_1:3:abc", timeout=2)
            assert info.value.code == "PROXY_REJECTED"
            assert str(status) in info.value.message
            assert "127.0.0.1" in info.value.message  # the Location host
            # The proxy saw the credentials (it is the configured URL) ...
            assert proxy.requests and proxy.requests[0].get("Authorization") == f"Bearer {TOKEN}"
        # ... and the second listener received nothing at all.
        assert second.requests == []
        assert _leaked(second.requests) == []


def test_redirect_on_info_is_rejected_too() -> None:
    with RecordingListener().running() as second:
        with OneShotServer(302, {"Location": second.url + "/"}).running() as proxy:
            with pytest.raises(OpError) as info:
                get_info(endpoint(proxy.url, **PROXY_HEADERS))
            assert info.value.code == "PROXY_REJECTED"
        assert second.requests == []


def test_html_page_is_proxy_rejected_not_unreachable() -> None:
    page = b"<html><body>Sign in</body></html>"
    with OneShotServer(200, {"Content-Type": "text/html"}, page).running() as proxy:
        with pytest.raises(OpError) as info:
            open_stream(endpoint(proxy.url), last_event_id=None, timeout=2)
    assert info.value.code == "PROXY_REJECTED"
    assert "200" in info.value.message and "text/html" in info.value.message


def test_lattice_json_where_a_stream_belongs_is_proxy_rejected() -> None:
    headers = {"Content-Type": "application/json", "Lattice-Protocol": "1"}
    with OneShotServer(200, headers, b'{"ok": true, "data": {}}').running() as proxy:
        with pytest.raises(OpError) as info:
            open_stream(endpoint(proxy.url), last_event_id=None, timeout=2)
    assert info.value.code == "PROXY_REJECTED"
    assert "application/json" in info.value.message


def test_event_stream_without_lattice_protocol_is_proxy_rejected() -> None:
    with OneShotServer(200, {"Content-Type": "text/event-stream"}, b"").running() as proxy:
        with pytest.raises(OpError) as info:
            open_stream(endpoint(proxy.url), last_event_id=None, timeout=2)
    assert info.value.code == "PROXY_REJECTED"


def test_server_error_envelope_keeps_its_code() -> None:
    headers = {"Content-Type": "application/json", "Lattice-Protocol": "1"}
    body = b'{"ok": false, "error": {"code": "UNAUTHENTICATED", "message": "bad token"}}'
    with OneShotServer(401, headers, body).running() as server:
        with pytest.raises(OpError) as info:
            open_stream(endpoint(server.url), last_event_id=None, timeout=2)
    assert (info.value.code, info.value.message) == ("UNAUTHENTICATED", "bad token")


def test_connection_refused_is_unreachable() -> None:
    with OneShotServer(200, {}).running() as server:
        url = server.url
    with pytest.raises(OpError) as info:
        open_stream(endpoint(url), last_event_id=None, timeout=1)
    assert info.value.code == "SERVER_UNREACHABLE"


def test_token_and_headers_are_not_in_the_endpoint_repr() -> None:
    text = repr(endpoint("http://127.0.0.1:1", **PROXY_HEADERS))
    assert TOKEN not in text and "proxy-secret" not in text


def test_follower_behind_a_redirecting_proxy_reports_proxy_rejected(tmp_path, stream_stub) -> None:
    with RecordingListener().running() as second:
        with OneShotServer(302, {"Location": second.url + "/login"}).running() as proxy:
            syncer = StubSyncer(stream_stub.url)
            ep_url = proxy.url
            with following(tmp_path, ep_url, syncer, heartbeat_seconds=0.2) as follower:
                assert wait_for(lambda: follower.last_stream_error is not None, 2)
                assert follower.last_stream_error.code == "PROXY_REJECTED"
                assert follower.stream_live_until is None
        assert second.requests == []
