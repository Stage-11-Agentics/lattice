"""SPEC §10: dashboard sign-in by Tailscale identity (``server.json`` ``tailnet``).

The in-process server trusts 127.0.0.1 as a proxy, so a test presents a client
address with ``X-Forwarded-For`` and a host with ``Host``; ``tailscale whois``
is replaced by a table.
"""

from __future__ import annotations

import asyncio
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
from lattice.server.testing import ServerHandle, open_stream, running_server
from tests.test_server.conftest import create_task, mint
from tests.test_server.web_client import WebClient


def _tailnet_ip(offset: int) -> str:
    """An address in the tailnet range, computed (the repository holds no literal one)."""
    return str(TAILNET_V4.network_address + offset)


HOST = "board.test:8740"
LAPTOP_IP = _tailnet_ip(0x250103)
PHONE_IP = _tailnet_ip(0x250104)
STRANGER_IP = _tailnet_ip(0x250105)
LAPTOP = WhoisAnswer(node="laptop.example.ts.net", stable_id="nLAPTOP1CNTRL", login="alice@x.dev")
PHONE = WhoisAnswer(node="phone.example.ts.net", stable_id="nPHONE1CNTRL", login="alice@x.dev")
STRANGER = WhoisAnswer(node="laptop.example.ts.net", stable_id="nOTHER1CNTRL", login="eve@x.dev")


def _visit(ip: str = LAPTOP_IP, host: str = HOST) -> dict[str, str]:
    return {"Host": host, "X-Forwarded-For": ip}


class Tailnet:
    def __init__(self, server: ServerHandle, token_id: str, calls: list[str]) -> None:
        self.server = server
        self.token_id = token_id
        self.calls = calls

    def get(self, path: str, ip: str = LAPTOP_IP, host: str = HOST, **headers: str):
        return WebClient(self.server).request("GET", path, headers={**_visit(ip, host), **headers})


@pytest.fixture()
def tailnet(root: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Tailnet]:
    record = tokens.create_token(root, user="human:alice", machine="laptop", all_projects=True)
    token_id = record["record"]["id"]
    table = {LAPTOP_IP: LAPTOP, PHONE_IP: PHONE, STRANGER_IP: STRANGER}
    calls: list[str] = []

    def fake_whois(_tailscale: str, address: str) -> WhoisAnswer | None:
        calls.append(address)
        return table.get(address)

    monkeypatch.setattr(tailnet_mod, "run_whois", fake_whois)
    config = {
        "trusted_proxies": ["127.0.0.1"],
        "tailnet": {
            "hosts": [HOST, "Board.Other"],
            "nodes": [
                {"node": "laptop.example.ts.net", "login": "alice@x.dev", "token": token_id}
            ],
        },
    }
    with running_server(root, config=config) as server:
        yield Tailnet(server, token_id, calls)


def test_a_granted_node_opens_the_dashboard_with_no_token_or_cookie(tailnet: Tailnet) -> None:
    page = tailnet.get("/p/alpha/")
    assert page.status == 200, page.text
    assert page.set_cookies() == []
    assert tailnet.get("/p/alpha/api/tasks").status == 200
    assert tailnet.get("/p/alpha/static/escape.js").status == 200
    index = tailnet.get("/")
    assert index.status == 200
    assert "Signed in as human:alice (laptop) through Tailscale (laptop.example.ts.net)" in (
        index.text
    )
    assert 'id="logout"' not in index.text
    assert tailnet.get("/", host="board.other").status == 200  # Host compares case-insensitively
    lines = [
        line
        for line in tailnet.server.log_lines
        if line.get("event") == "request" and line.get("path") == "/p/alpha/"
    ]
    assert lines and lines[-1]["tailnet"] == "laptop.example.ts.net"
    assert lines[-1]["token_id"] == tailnet.token_id


def test_one_whois_serves_many_requests(tailnet: Tailnet) -> None:
    for _ in range(5):
        assert tailnet.get("/p/alpha/api/config").status == 200
    assert tailnet.calls == [LAPTOP_IP]


def test_login_page_goes_straight_on_when_identified(tailnet: Tailnet) -> None:
    response = tailnet.get("/login?next=/p/alpha/")
    assert response.status == 303
    assert response.headers["location"] == "/p/alpha/"
    assert tailnet.get("/login?next=https://evil.example/").headers["location"] == "/"


@pytest.mark.parametrize(
    ("ip", "host", "whois_runs"),
    [
        (LAPTOP_IP, "evil.example:8740", False),  # DNS rebinding: the attacker's Host
        ("192.0.2.10", HOST, False),  # not a tailnet address
        (PHONE_IP, HOST, True),  # a node no grant lists
        (STRANGER_IP, HOST, True),  # the granted name, another login
    ],
)
def test_who_is_not_identified(tailnet: Tailnet, ip: str, host: str, whois_runs: bool) -> None:
    response = tailnet.get("/p/alpha/", ip=ip, host=host)
    assert response.status == 303
    assert response.headers["location"] == "/login?next=/p/alpha/"
    assert tailnet.get("/p/alpha/api/tasks", ip=ip, host=host).status == 401
    assert tailnet.get("/login", ip=ip, host=host).status == 200
    assert (ip in tailnet.calls) is whois_runs


def test_revoking_the_grants_token_ends_access_at_once(tailnet: Tailnet, root: Path) -> None:
    assert tailnet.get("/p/alpha/").status == 200
    tokens.revoke_token(root, tailnet.token_id)
    assert tailnet.get("/p/alpha/").status == 303


def test_an_authorization_header_alone_decides(tailnet: Tailnet) -> None:
    response = tailnet.get("/p/alpha/api/tasks", Authorization="Bearer lat_not-a-token")
    assert response.status == 401
    assert tailnet.calls == []  # never resolved


def test_dashboard_writes_still_need_this_servers_origin(tailnet: Tailnet, root: Path) -> None:
    task = create_task(tailnet.server, mint(root))
    web = WebClient(tailnet.server)
    path = f"/p/alpha/api/tasks/{task['id']}/status"
    body = json.dumps({"status": "in_planning"}).encode()
    for origin, expected in (("http://evil.example", 403), (f"http://{HOST}", 200)):
        headers = {**_visit(), "Content-Type": "application/json", "Origin": origin}
        response = web.request("POST", path, body=body, headers=headers)
        assert response.status == expected, response.text


def test_a_live_cookie_comes_before_the_tailnet_identity(tailnet: Tailnet, root: Path) -> None:
    web = WebClient(tailnet.server)
    assert web.login(mint(root, user="human:bob")).status == 303
    index = web.request("GET", "/", headers=_visit())
    assert "Signed in as human:bob" in index.text
    assert "Tailscale" not in index.text


def test_a_dead_cookie_is_cleared_and_the_tailnet_identity_used(tailnet: Tailnet) -> None:
    web = WebClient(tailnet.server, cookies={"lattice_session": "x" * 43})
    response = web.request("GET", "/p/alpha/", headers=_visit())
    assert response.status == 200
    assert any("max-age=0" in c.lower() for c in response.set_cookies())


def test_the_stream_follows_the_tailnet_identity(tailnet: Tailnet) -> None:
    reader = open_stream(tailnet.server.url, "alpha", None, headers=_visit())
    try:
        assert reader.status == 200
        assert reader.next(timeout=5) is not None
    finally:
        reader.close()
    refused = open_stream(tailnet.server.url, "alpha", None, headers=_visit(ip=PHONE_IP))
    try:
        assert refused.status == 401
    finally:
        refused.close()


def test_a_filing_only_grant_authenticates_nobody(root: Path, monkeypatch) -> None:
    record = tokens.create_token(
        root,
        user="human:alice",
        machine="svc",
        projects=["alpha"],
        only=("issue.file",),
        source="s",
    )
    monkeypatch.setattr(tailnet_mod, "run_whois", lambda _t, _a: LAPTOP)
    grant = {"node": LAPTOP.node, "login": LAPTOP.login, "token": record["record"]["id"]}
    config = {"trusted_proxies": ["127.0.0.1"], "tailnet": {"hosts": [HOST], "nodes": [grant]}}
    with running_server(root, config=config) as server:
        response = WebClient(server).request("GET", "/p/alpha/", headers=_visit())
        assert response.status == 303


def test_without_the_section_nothing_is_resolved(server: ServerHandle, monkeypatch) -> None:
    monkeypatch.setattr(tailnet_mod, "run_whois", lambda *_: pytest.fail("whois ran"))
    response = WebClient(server).request("GET", "/p/alpha/", headers=_visit())
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


def test_a_grant_may_name_the_stable_id() -> None:
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


GOOD = {
    "hosts": ["b:1"],
    "nodes": [{"node": "n.tailnet.example.", "login": "a@b", "token": "tok_A1"}],
}


def test_check_tailnet_accepts_a_good_section() -> None:
    config = check_tailnet(GOOD)
    assert config is not None
    assert config.grants[0].node == "n.tailnet.example"  # trailing dot dropped
    assert config.cache_seconds == 60.0 and config.tailscale == "tailscale"
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
        {**GOOD, "cache_seconds": -1},
        {**GOOD, "tailscale": ""},
    ],
)
def test_check_tailnet_refuses_mistakes(bad) -> None:
    with pytest.raises(ServerConfigError):
        check_tailnet(bad)
