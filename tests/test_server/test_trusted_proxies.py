"""Forwarded headers count only from listed proxies (SPEC §8.1 ``trusted_proxies``,
AC-11, LAT-349).

The in-process server listens on 127.0.0.1, so the test client's address is
``127.0.0.1``. Other addresses come from the documentation ranges (G-3).
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from click.testing import CliRunner

from lattice.cli.main import cli
from lattice.server.config import ServerConfig, ServerConfigError, parse_config
from lattice.server.serve import uvicorn_options
from lattice.server.testing import ServerHandle, running_server, wait_for
from tests.test_server.conftest import mint
from tests.test_server.web_client import WebClient

FORGED = {"X-Forwarded-Proto": "https", "X-Forwarded-For": "203.0.113.9"}


def _logged_client(server: ServerHandle, headers: dict[str, str]) -> str | None:
    """Send one unauthenticated request and return the client its log line names."""
    before = len(_request_lines(server, "/v1/projects"))
    status, _, _ = server.request("GET", "/v1/projects", headers=headers)
    assert status == 401
    return _next_client(server, "/v1/projects", before)


def _request_lines(server: ServerHandle, path: str) -> list[dict]:
    return [
        line
        for line in server.log_lines
        if line.get("event") == "request" and line.get("path") == path
    ]


def _next_client(server: ServerHandle, path: str, before: int) -> str | None:
    # The line is written as the response finishes, just after the client reads it.
    assert wait_for(lambda: len(_request_lines(server, path)) > before), server.log_lines
    return _request_lines(server, path)[-1]["client"]


def _login(
    server: ServerHandle, token: str, headers: dict[str, str], *, origin_scheme: str
) -> tuple[str, str | None]:
    """Log in with *headers*; return the session cookie's flags and the logged client.

    ``Origin`` carries *origin_scheme*: the scheme the server should believe,
    so a login whose forged ``X-Forwarded-Proto`` was ignored still passes the
    Origin check (and one whose header was honored would not)."""
    before = len(_request_lines(server, "/login"))
    host = server.url.removeprefix("http://")
    response = WebClient(server).request(
        "POST",
        "/login",
        body=f"token={token}".encode(),
        headers={
            "Content-Type": "application/x-www-form-urlencoded",
            "Origin": f"{origin_scheme}://{host}",
            **headers,
        },
    )
    client = _next_client(server, "/login", before)
    assert response.status == 303, response.text
    (cookie,) = response.set_cookies()
    flags = {p.strip().lower() for p in cookie.split(";")[1:]}
    return ("secure" if "secure" in flags else "plain"), client


# ---------------------------------------------------------------------------
# Login cookie and client address
# ---------------------------------------------------------------------------


def test_listed_proxy_makes_the_login_cookie_secure(root: Path) -> None:
    token = mint(root, projects=["alpha"])
    with running_server(root, config={"trusted_proxies": ["127.0.0.1"]}) as server:
        assert _login(server, token, FORGED, origin_scheme="https") == ("secure", "203.0.113.9")


@pytest.mark.parametrize("proxies", [[], ["192.0.2.1"]], ids=["empty", "unlisted"])
def test_unlisted_peer_cannot_make_the_login_cookie_secure(
    root: Path, proxies: list[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("FORWARDED_ALLOW_IPS", "*")
    token = mint(root, projects=["alpha"])
    with running_server(root, config={"trusted_proxies": proxies}) as server:
        # The forged scheme is ignored, so an https Origin is foreign: no session.
        host = server.url.removeprefix("http://")
        refused = WebClient(server).request(
            "POST",
            "/login",
            body=f"token={token}".encode(),
            headers={
                "Content-Type": "application/x-www-form-urlencoded",
                "Origin": "https://" + host,
                **FORGED,
            },
        )
        assert refused.status == 403 and refused.set_cookies() == []
        # With the Origin the server does believe, the cookie is plain and the
        # logged client is the real peer.
        assert _login(server, token, FORGED, origin_scheme="http") == ("plain", "127.0.0.1")


# ---------------------------------------------------------------------------
# Client address
# ---------------------------------------------------------------------------


def test_listed_peer_forwards_the_client_address(root: Path) -> None:
    with running_server(root, config={"trusted_proxies": ["127.0.0.1"]}) as server:
        assert _logged_client(server, FORGED) == "203.0.113.9"
        assert _logged_client(server, {}) == "127.0.0.1"


@pytest.mark.parametrize(
    "proxies", [[], ["192.0.2.1"], ["2001:db8::1", "192.0.2.0/24"]], ids=["empty", "v4", "mixed"]
)
def test_unlisted_peer_cannot_forge_the_client_address(
    root: Path, proxies: list[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    # uvicorn's own default would trust whatever this names.
    monkeypatch.setenv("FORWARDED_ALLOW_IPS", "*")
    with running_server(root, config={"trusted_proxies": proxies}) as server:
        assert _logged_client(server, FORGED) == "127.0.0.1"


def test_default_config_ignores_forwarded_headers(root: Path) -> None:
    """No ``trusted_proxies`` key at all: the default ``[]``. uvicorn trusts
    127.0.0.1 by default, so this fails if the server stops passing its options."""
    with running_server(root) as server:
        assert _logged_client(server, FORGED) == "127.0.0.1"


def test_cidr_entry_matches_the_peer(root: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("FORWARDED_ALLOW_IPS", raising=False)
    with running_server(root, config={"trusted_proxies": ["127.0.0.0/8"]}) as server:
        assert _logged_client(server, FORGED) == "203.0.113.9"


def test_multi_hop_yields_the_rightmost_untrusted_entry(root: Path) -> None:
    config = {"trusted_proxies": ["127.0.0.1", "198.51.100.0/24"]}
    chain = {"X-Forwarded-For": "203.0.113.9, 192.0.2.7, 198.51.100.3"}
    with running_server(root, config=config) as server:
        # 198.51.100.3 is a listed proxy; 192.0.2.7 is the first hop it did not vouch for.
        # The leftmost entry (203.0.113.9) is whatever the client claimed.
        assert _logged_client(server, chain) == "192.0.2.7"


# ---------------------------------------------------------------------------
# uvicorn options
# ---------------------------------------------------------------------------


def test_uvicorn_options_always_decide_trust_explicitly() -> None:
    assert uvicorn_options(ServerConfig()) == {
        "proxy_headers": False,
        "forwarded_allow_ips": [],
        "server_header": False,
    }
    listed = parse_config({"trusted_proxies": ["192.0.2.1", "2001:db8::/32"]})
    assert uvicorn_options(listed)["proxy_headers"] is True
    assert uvicorn_options(listed)["forwarded_allow_ips"] == ["192.0.2.1", "2001:db8::/32"]


# ---------------------------------------------------------------------------
# server.json
# ---------------------------------------------------------------------------


def test_valid_entries_parse() -> None:
    config = parse_config(
        {"trusted_proxies": ["127.0.0.1", " 192.0.2.0/24 ", "2001:db8::1", "2001:db8::/32"]}
    )
    assert config.trusted_proxies == ("127.0.0.1", "192.0.2.0/24", "2001:db8::1", "2001:db8::/32")


@pytest.mark.parametrize(
    ("value", "fragment"),
    [
        (True, "must be a list"),
        ("127.0.0.1", "must be a list"),
        ([1], "must be strings"),
        (["*"], "'*' is not an IPv4 or IPv6"),
        (["proxy.example"], "'proxy.example' is not"),
        (["192.0.2.1/24"], "host bits set"),
        (["192.0.2.0/33"], "'192.0.2.0/33' is not"),
    ],
)
def test_invalid_entries_are_refused(value: object, fragment: str) -> None:
    with pytest.raises(ServerConfigError, match="trusted_proxies") as info:
        parse_config({"trusted_proxies": value})
    assert fragment in str(info.value)


@pytest.mark.parametrize("value", [True, False])
def test_old_boolean_key_is_refused_naming_the_new_one(value: bool) -> None:
    with pytest.raises(ServerConfigError, match="trusted_proxies"):
        parse_config({"trusted_proxy": value})


@pytest.mark.parametrize("is_json", [False, True], ids=["plain", "json"])
def test_serve_refuses_to_start_with_the_old_key(root: Path, is_json: bool) -> None:
    config_path = root / "server.json"
    raw = json.loads(config_path.read_text(encoding="utf-8")) if config_path.exists() else {}
    raw.pop("trusted_proxies", None)
    raw["trusted_proxy"] = True
    config_path.write_text(json.dumps(raw), encoding="utf-8")
    args = ["server", "serve", "--root", str(root), "--port", "1"]
    result = CliRunner().invoke(cli, args + (["--json"] if is_json else []))
    assert result.exit_code == 1
    output = result.output
    if is_json:
        envelope = json.loads(result.stdout)
        assert envelope["ok"] is False
        assert envelope["error"]["code"] == "VALIDATION_ERROR"
        output = envelope["error"]["message"]
    assert "trusted_proxy is no longer accepted" in output
    assert "trusted_proxies" in output
