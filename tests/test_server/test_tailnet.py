"""SPEC §10: dashboard sign-in by Tailscale identity (``server.json`` ``tailnet``).

The in-process server listens on loopback, so the ``direct`` fixture counts
127.0.0.0/8 as the tailnet: a request then arrives straight from a "tailnet"
peer, as a browser on a granted device reaches the server. ``tailscale whois``
is replaced by a table. The forwarded path (a ``trusted_proxies`` peer and
``X-Forwarded-For``) has its own tests.
"""

from __future__ import annotations

import asyncio
import ipaddress
import json
from collections.abc import Iterator
from pathlib import Path

import pytest

from lattice.server import tailnet as tailnet_mod
from lattice.server import tokens
from lattice.server.config import ServerConfigError, TailnetConfig, TailnetGrant, check_tailnet
from lattice.server.tailnet import (
    TAILNET_V4,
    TailnetResolver,
    WhoisAnswer,
    parse_whois,
    tailnet_address,
)
from lattice.server.testing import ServerHandle, open_stream, running_server, wait_for
from tests.test_server.conftest import create_task, mint
from tests.test_server.web_client import WebClient


def _tailnet_ip(offset: int) -> str:
    """An address in the tailnet range, computed (the repository holds no literal one)."""
    return str(TAILNET_V4.network_address + offset)


HOST = "board.test:8740"
LAPTOP_IP = _tailnet_ip(0x250103)
LAPTOP = WhoisAnswer(node="laptop.example.ts.net", stable_id="nLAPTOP1CNTRL", login="alice@x.dev")
PHONE = WhoisAnswer(node="phone.example.ts.net", stable_id="nPHONE1CNTRL", login="alice@x.dev")
STRANGER = WhoisAnswer(node="laptop.example.ts.net", stable_id="nOTHER1CNTRL", login="eve@x.dev")
GRANT = {"node": "laptop.example.ts.net", "login": "alice@x.dev"}


class Tailnet:
    """A running server with ``tailnet`` on, and the whois stub's state."""

    def __init__(self, server: ServerHandle, token_id: str, who: dict, calls: list) -> None:
        self.server = server
        self.token_id = token_id
        self.who = who  # who["answer"]: what whois says about every address
        self.calls = calls

    def get(self, path: str, host: str = HOST, **headers: str):
        return WebClient(self.server).request("GET", path, headers={"Host": host, **headers})


def _stub_whois(monkeypatch: pytest.MonkeyPatch, who: dict, calls: list) -> None:
    def fake(_tailscale: str, address: str, **_kw) -> WhoisAnswer | None:
        calls.append(address)
        return who["answer"]

    monkeypatch.setattr(tailnet_mod, "run_whois", fake)


@pytest.fixture()
def direct(root: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Tailnet]:
    token_id = tokens.create_token(root, user="human:alice", machine="laptop", all_projects=True)[
        "record"
    ]["id"]
    monkeypatch.setattr(tailnet_mod, "TAILNET_V4", ipaddress.ip_network("127.0.0.0/8"))
    who: dict = {"answer": LAPTOP}
    calls: list = []
    _stub_whois(monkeypatch, who, calls)
    config = {"tailnet": {"hosts": [HOST, "Board.Other"], "nodes": [{**GRANT, "token": token_id}]}}
    with running_server(root, config=config, heartbeat_seconds=0.2) as server:
        yield Tailnet(server, token_id, who, calls)


def test_a_granted_node_opens_the_dashboard_with_no_token_or_cookie(direct: Tailnet) -> None:
    page = direct.get("/p/alpha/")
    assert page.status == 200, page.text
    assert page.set_cookies() == []
    assert direct.get("/p/alpha/api/tasks").status == 200
    assert direct.get("/p/alpha/static/escape.js").status == 200
    index = direct.get("/")
    assert index.status == 200
    expected = "Signed in as human:alice (laptop) through Tailscale (laptop.example.ts.net)"
    assert expected in index.text
    assert 'id="logout"' not in index.text
    assert direct.get("/", host="board.other").status == 200  # Host compares case-insensitively
    lines = [
        line
        for line in direct.server.log_lines
        if line.get("event") == "request" and line.get("path") == "/p/alpha/"
    ]
    assert lines and lines[-1]["tailnet"] == "laptop.example.ts.net"
    assert lines[-1]["token_id"] == direct.token_id


def test_one_whois_serves_many_requests(direct: Tailnet) -> None:
    for _ in range(5):
        assert direct.get("/p/alpha/api/config").status == 200
    assert direct.calls == ["127.0.0.1"]


def test_login_page_goes_straight_on_when_identified(direct: Tailnet) -> None:
    response = direct.get("/login?next=/p/alpha/")
    assert response.status == 303
    assert response.headers["location"] == "/p/alpha/"
    assert direct.get("/login?next=https://evil.example/").headers["location"] == "/"


@pytest.mark.parametrize(
    ("answer", "host", "headers", "whois_runs"),
    [
        (LAPTOP, "evil.example:8740", {}, False),  # DNS rebinding: the attacker's Host
        (PHONE, HOST, {}, True),  # a node no grant lists
        (STRANGER, HOST, {}, True),  # the granted name, another login
        (None, HOST, {}, True),  # no node holds the address
        (LAPTOP, HOST, {"X-Forwarded-For": LAPTOP_IP}, False),  # forwarded, not trusted
        (LAPTOP, HOST, {"Sec-Fetch-Site": "cross-site", "Sec-Fetch-Mode": "no-cors"}, False),
        (LAPTOP, HOST, {"Sec-Fetch-Site": "same-site", "Sec-Fetch-Mode": "cors"}, False),
    ],
)
def test_who_is_not_identified(direct: Tailnet, answer, host, headers, whois_runs) -> None:
    direct.who["answer"] = answer
    response = direct.get("/p/alpha/", host=host, **headers)
    assert response.status == 303
    assert response.headers["location"] == "/login?next=/p/alpha/"
    assert direct.get("/p/alpha/api/tasks", host=host, **headers).status == 401
    assert (direct.calls != []) is whois_runs


def test_another_site_may_link_to_a_board(direct: Tailnet) -> None:
    headers = {"Sec-Fetch-Site": "cross-site", "Sec-Fetch-Mode": "navigate"}
    page = direct.get("/p/alpha/", **headers, **{"Sec-Fetch-Dest": "document"})
    assert page.status == 200
    assert page.headers["cross-origin-resource-policy"] == "same-origin"
    framed = direct.get("/p/alpha/", **headers, **{"Sec-Fetch-Dest": "iframe"})
    assert framed.status == 303
    same = {"Sec-Fetch-Site": "same-origin", "Sec-Fetch-Mode": "cors"}
    assert direct.get("/p/alpha/api/tasks", **same).status == 200


def test_a_non_tailnet_address_is_never_looked_up(root: Path, monkeypatch) -> None:
    who: dict = {"answer": LAPTOP}
    calls: list = []
    _stub_whois(monkeypatch, who, calls)
    grant = {
        **GRANT,
        "token": tokens.create_token(root, user="human:a", machine="m", all_projects=True)[
            "record"
        ]["id"],
    }
    config = {"tailnet": {"hosts": [HOST], "nodes": [grant]}}
    with running_server(root, config=config) as server:
        response = WebClient(server).request("GET", "/p/alpha/", headers={"Host": HOST})
        assert response.status == 303
    assert calls == []  # 127.0.0.1 is not in 100.64.0.0/10


def test_revoking_the_grants_token_ends_access_at_once(direct: Tailnet, root: Path) -> None:
    assert direct.get("/p/alpha/").status == 200
    tokens.revoke_token(root, direct.token_id)
    assert direct.get("/p/alpha/").status == 303


def _records(root: Path) -> list[dict]:
    data = json.loads((root / "tokens.json").read_text())
    return data["tokens"] if isinstance(data, dict) else data


def _edit_record(root: Path, token_id: str, **changes) -> None:
    path = root / "tokens.json"
    data = json.loads(path.read_text())
    for record in data["tokens"] if isinstance(data, dict) else data:
        if record["id"] == token_id:
            record.update(changes)
    path.write_text(json.dumps(data, indent=2) + "\n")


def test_a_grant_token_the_bearer_rule_refuses_authenticates_nobody(
    direct: Tailnet, root: Path
) -> None:
    """An ``only`` restriction without the ``only-v1:`` hash prefix (a hand edit,
    version skew) is refused for a bearer, so it is refused here too."""
    _edit_record(root, direct.token_id, only=["issue.comment"])
    assert direct.get("/p/alpha/api/tasks").status == 401


def test_an_authorization_header_alone_decides(direct: Tailnet) -> None:
    response = direct.get("/p/alpha/api/tasks", Authorization="Bearer lat_not-a-token")
    assert response.status == 401
    assert direct.calls == []  # never resolved


def test_dashboard_writes_still_need_this_servers_origin(direct: Tailnet, root: Path) -> None:
    task = create_task(direct.server, mint(root))
    web = WebClient(direct.server)
    path = f"/p/alpha/api/tasks/{task['id']}/status"
    body = json.dumps({"status": "in_planning"}).encode()
    for origin, expected in (("http://evil.example", 403), (f"http://{HOST}", 200)):
        headers = {"Host": HOST, "Content-Type": "application/json", "Origin": origin}
        response = web.request("POST", path, body=body, headers=headers)
        assert response.status == expected, response.text


def test_a_live_cookie_comes_first_and_skips_the_lookup(direct: Tailnet, root: Path) -> None:
    web = WebClient(direct.server)
    assert web.login(mint(root, user="human:bob")).status == 303
    index = web.request("GET", "/", headers={"Host": HOST})
    assert "Signed in as human:bob" in index.text
    assert "Tailscale" not in index.text
    assert direct.calls == []


def test_a_dead_cookie_is_cleared_and_the_tailnet_identity_used(direct: Tailnet) -> None:
    web = WebClient(direct.server, cookies={"lattice_session": "x" * 43})
    response = web.request("GET", "/p/alpha/", headers={"Host": HOST})
    assert response.status == 200
    assert any("max-age=0" in c.lower() for c in response.set_cookies())


def test_logout_on_a_tailnet_visit_changes_nothing(direct: Tailnet, root: Path) -> None:
    before = (root / "web_sessions.json").exists()
    headers = {"Host": HOST, "Content-Type": "application/json", "Origin": f"http://{HOST}"}
    response = WebClient(direct.server).request("POST", "/logout", body=b"{}", headers=headers)
    assert response.status == 200
    assert (root / "web_sessions.json").exists() is before
    assert not [line for line in direct.server.log_lines if line.get("event") == "logout"]


def test_the_stream_follows_the_tailnet_identity(direct: Tailnet, root: Path) -> None:
    reader = open_stream(direct.server.url, "alpha", None, headers={"Host": HOST})
    try:
        assert reader.status == 200
        assert reader.next(timeout=5) is not None
        tokens.revoke_token(root, direct.token_id)  # the heartbeat recheck ends it

        def ended() -> bool:
            try:
                return reader.next(timeout=1) is None  # None: the server closed it
            except TimeoutError:
                return False

        assert wait_for(ended, timeout=10)
    finally:
        reader.close()


def test_the_stream_refuses_an_ungranted_node_and_a_cross_site_frame(direct: Tailnet) -> None:
    framed = {"Host": HOST, "Sec-Fetch-Site": "cross-site", "Sec-Fetch-Mode": "navigate"}
    framed["Sec-Fetch-Dest"] = "iframe"
    reader = open_stream(direct.server.url, "alpha", None, headers=framed)
    try:
        assert reader.status == 401
    finally:
        reader.close()
    assert direct.calls == []  # refused before any lookup
    direct.who["answer"] = PHONE
    refused = open_stream(direct.server.url, "alpha", None, headers={"Host": HOST})
    try:
        assert refused.status == 401
    finally:
        refused.close()
    assert direct.calls == ["127.0.0.1"]  # looked up, and PHONE is not granted


def test_a_session_whose_token_gains_a_bad_restriction_ends(direct: Tailnet, root: Path) -> None:
    web = WebClient(direct.server)
    token = mint(root, user="human:bob")
    assert web.login(token).status == 303
    assert web.request("GET", "/p/alpha/api/tasks", headers={"Host": "elsewhere"}).status == 200
    bob = next(r for r in _records(root) if r["user"] == "human:bob")
    _edit_record(root, bob["id"], only=["issue.comment"])
    assert web.request("GET", "/p/alpha/api/tasks", headers={"Host": "elsewhere"}).status == 401


def test_a_filing_only_grant_authenticates_nobody(root: Path, monkeypatch) -> None:
    record = tokens.create_token(
        root,
        user="human:alice",
        machine="svc",
        projects=["alpha"],
        only=("issue.file",),
        source="s",
    )
    monkeypatch.setattr(tailnet_mod, "TAILNET_V4", ipaddress.ip_network("127.0.0.0/8"))
    _stub_whois(monkeypatch, {"answer": LAPTOP}, [])
    config = {"tailnet": {"hosts": [HOST], "nodes": [{**GRANT, "token": record["record"]["id"]}]}}
    with running_server(root, config=config) as server:
        response = WebClient(server).request("GET", "/p/alpha/", headers={"Host": HOST})
        assert response.status == 303
        warned = [
            line for line in server.log_lines if line.get("event") == "tailnet_grant_unusable"
        ]
        assert warned and warned[0]["token_id"] == record["record"]["id"]


@pytest.mark.parametrize("trust", [False, True])
def test_forwarded_requests_need_trust_forwarded(root: Path, monkeypatch, trust: bool) -> None:
    """Any process able to connect from a ``trusted_proxies`` address can write
    ``X-Forwarded-For``; only ``trust_forwarded`` lets it name the device."""
    who: dict = {"answer": LAPTOP}
    calls: list = []
    _stub_whois(monkeypatch, who, calls)
    grant = {
        **GRANT,
        "token": tokens.create_token(root, user="human:a", machine="m", all_projects=True)[
            "record"
        ]["id"],
    }
    config = {
        "trusted_proxies": ["127.0.0.1"],
        "tailnet": {"hosts": [HOST], "nodes": [grant], "trust_forwarded": trust},
    }
    with running_server(root, config=config) as server:
        headers = {"Host": HOST, "X-Forwarded-For": LAPTOP_IP}
        response = WebClient(server).request("GET", "/p/alpha/", headers=headers)
        assert response.status == (200 if trust else 303)
    assert calls == ([LAPTOP_IP] if trust else [])


def test_without_the_section_nothing_is_resolved(server: ServerHandle, monkeypatch) -> None:
    monkeypatch.setattr(tailnet_mod, "run_whois", lambda *_, **__: pytest.fail("whois ran"))
    response = WebClient(server).request("GET", "/p/alpha/", headers={"Host": HOST})
    assert response.status == 303
    assert server.state.tailnet is None


# ---------------------------------------------------------------------------
# Units: addresses, whois parsing, the resolver, config
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("host", "expected"),
    [
        (_tailnet_ip(1), _tailnet_ip(1)),
        (_tailnet_ip(2**22 - 2), _tailnet_ip(2**22 - 2)),  # the range's last host
        (f"::ffff:{LAPTOP_IP}", LAPTOP_IP),
        ("fd7a:115c:a1e0::1", "fd7a:115c:a1e0::1"),
        ("100.128.0.1", None),
        ("127.0.0.1", None),
        ("fd7a:115c:a1e1::1", None),
        ("testclient", None),
        (None, None),
    ],
)
def test_tailnet_address(host: str | None, expected: str | None) -> None:
    assert tailnet_address(host) == expected


def test_parse_whois_reads_name_id_and_login() -> None:
    text = json.dumps(
        {
            "Node": {"Name": "Laptop.Example.ts.net.", "StableID": "nX1CNTRL"},
            "UserProfile": {"LoginName": "alice@x.dev"},
        }
    )
    assert parse_whois(text) == WhoisAnswer("laptop.example.ts.net", "nX1CNTRL", "alice@x.dev")
    with pytest.raises(tailnet_mod.WhoisError):
        parse_whois("{}")


def _resolver(whois, **config) -> TailnetResolver:
    grant = TailnetGrant(node="nLAPTOP1CNTRL", login=LAPTOP.login, token="tok_1")
    return TailnetResolver(TailnetConfig(hosts=(HOST,), grants=(grant,), **config), whois=whois)


def _scope(ip: str = LAPTOP_IP) -> dict:
    return {"type": "http", "client": (ip, 1), "headers": [(b"host", HOST.encode())]}


def test_a_direct_tailnet_peer_is_identified_and_a_grant_may_name_the_stable_id() -> None:
    identity = asyncio.run(_resolver(lambda _a: LAPTOP).identify(_scope()))
    assert identity is not None and identity.node == LAPTOP.node


def test_concurrent_misses_share_one_lookup() -> None:
    calls = []

    def slow(address: str) -> WhoisAnswer:
        import time

        calls.append(address)
        time.sleep(0.05)
        return LAPTOP

    resolver = _resolver(slow)

    async def many() -> list:
        return await asyncio.gather(*(resolver.identify(_scope()) for _ in range(10)))

    assert all(asyncio.run(many()))
    assert calls == [LAPTOP_IP]


def test_a_failed_lookup_identifies_nobody_and_is_reported() -> None:
    reported = []

    def broken(_address: str) -> WhoisAnswer:
        raise tailnet_mod.WhoisError("tailscaled is not running")

    resolver = _resolver(broken)
    resolver.on_error = lambda **fields: reported.append(fields)
    assert asyncio.run(resolver.identify(_scope())) is None
    assert reported == [
        {"address": LAPTOP_IP, "error": "WhoisError", "detail": "tailscaled is not running"}
    ]


def test_cache_seconds_zero_asks_every_time() -> None:
    calls = []
    resolver = _resolver(lambda a: calls.append(a) or LAPTOP, cache_seconds=0)
    for _ in range(3):
        assert asyncio.run(resolver.identify(_scope()))
    assert len(calls) == 3


def test_the_cache_stays_bounded(monkeypatch) -> None:
    monkeypatch.setattr(tailnet_mod, "_CACHE_MAX", 4)
    resolver = _resolver(lambda _a: LAPTOP)
    for offset in range(10):
        assert asyncio.run(resolver.identify(_scope(_tailnet_ip(offset + 1))))
    assert len(resolver._cache) <= 4


GOOD = {
    "hosts": ["b:1"],
    "nodes": [{"node": "n.tailnet.example.", "login": "a@b", "token": "tok_A1"}],
}


def test_check_tailnet_accepts_a_good_section() -> None:
    config = check_tailnet(GOOD)
    assert config is not None
    assert config.grants[0].node == "n.tailnet.example"  # trailing dot dropped
    assert config.cache_seconds == 60.0 and config.tailscale == "tailscale"
    assert config.trust_forwarded is False
    assert check_tailnet({**GOOD, "trust_forwarded": True}).trust_forwarded is True
    assert check_tailnet(None) is None


@pytest.mark.parametrize(
    "bad",
    [
        [],
        {**GOOD, "extra": 1},
        {**GOOD, "hosts": []},
        {**GOOD, "hosts": [" b:1"]},
        {**GOOD, "nodes": []},
        {**GOOD, "nodes": [{"node": "n", "login": "a@b"}]},
        {**GOOD, "nodes": [{"node": "n", "login": "a@b", "token": "secret-token"}]},
        {**GOOD, "nodes": [{"node": ".", "login": "a@b", "token": "tok_A1"}]},
        {**GOOD, "cache_seconds": -1},
        {**GOOD, "tailscale": ""},
        {**GOOD, "trust_forwarded": "yes"},
    ],
)
def test_check_tailnet_refuses_mistakes(bad) -> None:
    with pytest.raises(ServerConfigError):
        check_tailnet(bad)


def test_trust_forwarded_needs_a_trusted_proxy() -> None:
    from lattice.server.config import parse_config

    with pytest.raises(ServerConfigError, match="trusted_proxies"):
        parse_config({"tailnet": {**GOOD, "trust_forwarded": True}})
    config = parse_config(
        {"trusted_proxies": ["192.0.2.1"], "tailnet": {**GOOD, "trust_forwarded": True}}
    )
    assert config.tailnet is not None and config.tailnet.trust_forwarded


# ---------------------------------------------------------------------------
# How whois asks: tailscaled's LocalAPI in-process, else the CLI
# ---------------------------------------------------------------------------

WHOIS_JSON = json.dumps(
    {
        "Node": {"Name": "laptop.example.ts.net.", "StableID": "nLAPTOP1CNTRL"},
        "UserProfile": {"LoginName": "alice@x.dev"},
    }
).encode()


class _LocalAPIHandler:
    """A fake LocalAPI: records each request's path and headers."""

    seen: list[tuple[str, dict]] = []
    status = 200

    @classmethod
    def build(cls):
        import http.server

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_GET(self) -> None:  # noqa: N802 - the stdlib's name
                cls.seen.append((self.path, dict(self.headers)))
                body = WHOIS_JSON if cls.status == 200 else b"no match"
                self.send_response(cls.status)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *_args) -> None:
                pass

        return Handler


@pytest.fixture()
def no_localapi(monkeypatch, tmp_path: Path) -> None:
    """No LocalAPI unless a test makes one; files this test user owns count as
    tailscaled's (in production only root's do)."""
    import os

    monkeypatch.setattr(tailnet_mod, "LOCALAPI_SOCKETS", ())
    monkeypatch.setattr(tailnet_mod, "MACOS_LOCALAPI_DIR", tmp_path / "absent")
    monkeypatch.setattr(tailnet_mod, "_TRUSTED_OWNER_UIDS", (0, os.getuid()))


def test_whois_asks_the_localapi_unix_socket(monkeypatch, no_localapi) -> None:
    import socketserver
    import tempfile
    import threading

    class UnixServer(socketserver.ThreadingMixIn, socketserver.UnixStreamServer):
        daemon_threads = True

        def get_request(self):  # BaseHTTPRequestHandler wants a client address
            sock, _ = super().get_request()
            return sock, ("local", 0)

    folder = Path(tempfile.mkdtemp(dir="/tmp"))  # a unix socket path must stay short
    path = folder / "tailscaled.sock"
    _LocalAPIHandler.seen, _LocalAPIHandler.status = [], 200
    server = UnixServer(str(path), _LocalAPIHandler.build())
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        monkeypatch.setattr(tailnet_mod, "LOCALAPI_SOCKETS", ("/nonexistent.sock", str(path)))
        assert tailnet_mod.whois_route("tailscale") == "localapi-unix"
        assert tailnet_mod.run_whois("/no/such/cli", LAPTOP_IP) == LAPTOP
        assert _LocalAPIHandler.seen[0][0] == f"/localapi/v0/whois?addr={LAPTOP_IP}"
        assert _LocalAPIHandler.seen[0][1]["Sec-Tailscale"] == "localapi"
        _LocalAPIHandler.status = 404
        assert tailnet_mod.run_whois("/no/such/cli", LAPTOP_IP) is None
        _LocalAPIHandler.status = 500
        with pytest.raises(tailnet_mod.WhoisError, match="HTTP 500"):
            tailnet_mod.run_whois("/no/such/cli", LAPTOP_IP)
        assert tailnet_mod.whois_route("ts", localapi=False) == "cli:ts"
    finally:
        server.shutdown()
        server.server_close()
        import shutil

        shutil.rmtree(folder, ignore_errors=True)


def test_whois_asks_the_macos_localapi_port_with_its_token(
    monkeypatch, tmp_path: Path, no_localapi
) -> None:
    import base64
    import http.server
    import threading

    _LocalAPIHandler.seen, _LocalAPIHandler.status = [], 200
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _LocalAPIHandler.build())
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        port = server.server_address[1]
        base = tmp_path / "Tailscale"
        base.mkdir()
        (base / "ipnport").symlink_to(str(port))
        (base / f"sameuserproof-{port}").write_text("the-token\n")
        monkeypatch.setattr(tailnet_mod, "MACOS_LOCALAPI_DIR", base)
        assert tailnet_mod.whois_route("tailscale") == "localapi-tcp"
        assert tailnet_mod.run_whois("/no/such/cli", LAPTOP_IP) == LAPTOP
        headers = _LocalAPIHandler.seen[0][1]
        assert headers["Authorization"] == "Basic " + base64.b64encode(b":the-token").decode()
        assert headers["Sec-Tailscale"] == "localapi"
    finally:
        server.shutdown()
        server.server_close()


def test_the_cli_fallback_kills_a_wrapper_and_its_child(
    monkeypatch, tmp_path: Path, no_localapi
) -> None:
    import os
    import time

    pid_file = tmp_path / "child.pid"
    cli = tmp_path / "tailscale"
    # A wrapper that does not exec, as some installs ship: the real binary is a child.
    cli.write_text(f"#!/bin/sh\nsleep 30 &\necho $! > {pid_file}\nwait\n")
    cli.chmod(0o755)
    monkeypatch.setattr(tailnet_mod, "CLI_TIMEOUT_SECONDS", 2.0)
    assert tailnet_mod.whois_route(str(cli)) == f"cli:{cli}"
    with pytest.raises(tailnet_mod.WhoisError, match="took over"):
        tailnet_mod.run_whois(str(cli), LAPTOP_IP)
    child = int(pid_file.read_text())
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        try:
            os.kill(child, 0)
        except ProcessLookupError:
            break
        time.sleep(0.05)
    else:
        pytest.fail("the wrapped child outlived the timeout")


def test_the_cli_fallback_reads_answers_and_misses(monkeypatch, tmp_path: Path, no_localapi):
    cli = tmp_path / "tailscale"
    answer = tmp_path / "answer.json"
    answer.write_bytes(WHOIS_JSON)
    cli.write_text(
        f'#!/bin/sh\nif [ "$3" = "{LAPTOP_IP}" ]; then cat {answer}; exit 0; fi\n'
        'echo "peer not found" >&2\nexit 1\n'
    )
    cli.chmod(0o755)
    assert tailnet_mod.run_whois(str(cli), LAPTOP_IP) == LAPTOP
    assert tailnet_mod.run_whois(str(cli), _tailnet_ip(9)) is None


def _tcp_localapi(monkeypatch, tmp_path: Path, handler, *, owner_ok: bool = True):
    """A fake macOS LocalAPI on a TCP port; returns the server."""
    import http.server
    import threading

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    port = server.server_address[1]
    base = tmp_path / "Tailscale"
    base.mkdir()
    (base / "ipnport").symlink_to(str(port))
    (base / f"sameuserproof-{port}").write_text("the-token\n")
    monkeypatch.setattr(tailnet_mod, "MACOS_LOCALAPI_DIR", base)
    if not owner_ok:
        monkeypatch.setattr(tailnet_mod, "_TRUSTED_OWNER_UIDS", (0,))
    return server


def test_a_404_without_tailscaleds_no_match_is_an_error(monkeypatch, tmp_path, no_localapi):
    """A stale port now held by another server answers 404 to everything: that
    must fail loudly, not quietly mean "no such node"."""
    import http.server

    class Foreign(http.server.BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802
            body = b"<html>Not Found</html>"
            self.send_response(404)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *_args) -> None:
            pass

    server = _tcp_localapi(monkeypatch, tmp_path, Foreign)
    try:
        with pytest.raises(tailnet_mod.WhoisError, match="HTTP 404") as raised:
            tailnet_mod.run_whois("/no/such/cli", LAPTOP_IP)
        assert "the-token" not in str(raised.value)
    finally:
        server.shutdown()
        server.server_close()


def test_a_slow_localapi_body_is_cut_off_at_the_deadline(monkeypatch, tmp_path, no_localapi):
    import http.server
    import time

    class Dripping(http.server.BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802
            self.send_response(200)
            self.send_header("Content-Length", "40")
            self.end_headers()
            for _ in range(40):
                self.wfile.write(b" ")
                self.wfile.flush()
                time.sleep(0.1)

        def log_message(self, *_args) -> None:
            pass

    monkeypatch.setattr(tailnet_mod, "WHOIS_TIMEOUT_SECONDS", 0.5)
    server = _tcp_localapi(monkeypatch, tmp_path, Dripping)
    try:
        started = time.monotonic()
        with pytest.raises(tailnet_mod.WhoisError, match="took over"):
            tailnet_mod.run_whois("/no/such/cli", LAPTOP_IP)
        assert time.monotonic() - started < 2
    finally:
        server.shutdown()
        server.server_close()


def test_localapi_files_another_user_owns_are_ignored(monkeypatch, tmp_path, no_localapi):
    _LocalAPIHandler.seen, _LocalAPIHandler.status = [], 200
    server = _tcp_localapi(monkeypatch, tmp_path, _LocalAPIHandler.build(), owner_ok=False)
    try:
        assert tailnet_mod.localapi_endpoint() is None  # not root's: never sent the token
        assert tailnet_mod.whois_route("ts") == "cli:ts"
        assert _LocalAPIHandler.seen == []
    finally:
        server.shutdown()
        server.server_close()


def test_localapi_false_always_uses_the_cli(monkeypatch, tmp_path, no_localapi) -> None:
    _LocalAPIHandler.seen, _LocalAPIHandler.status = [], 200
    server = _tcp_localapi(monkeypatch, tmp_path, _LocalAPIHandler.build())
    try:
        cli = tmp_path / "ts-cli"  # "tailscale" would be the "Tailscale" folder on macOS
        cli.write_text('#!/bin/sh\necho "peer not found" >&2\nexit 1\n')
        cli.chmod(0o755)
        assert tailnet_mod.run_whois(str(cli), LAPTOP_IP, localapi=False) is None
        assert _LocalAPIHandler.seen == []
        assert check_tailnet({**GOOD, "localapi": False}).localapi is False
    finally:
        server.shutdown()
        server.server_close()
