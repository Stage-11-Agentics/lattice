"""AC-11 (bearer): missing, bad, revoked → 401; other project → 403; nothing appended."""

from __future__ import annotations

import json
from pathlib import Path

from lattice.server import tokens
from lattice.server.testing import ServerHandle
from tests.test_server.conftest import board_hash, mint
from tests.test_server.web_client import WebClient


def _bad_tokens(good: str) -> list[str | None]:
    token_id, secret = tokens.parse_token(good)
    flipped = ("A" if secret[0] != "A" else "B") + secret[1:]
    return [
        None,
        "",
        "garbage",
        "lat_tok_nope_x",
        tokens.format_token(token_id, flipped),
        tokens.format_token(tokens.new_token_id(), secret),
    ]


def test_bearer_failures(server: ServerHandle, root: Path) -> None:
    good = mint(root, projects=["alpha"])
    before = board_hash(root, "alpha")
    for bad in _bad_tokens(good):
        status, _, body = server.op("alpha", "task.create", {"title": "x"}, token=bad)
        assert status == 401, (bad, body)
        assert body["error"]["code"] == "UNAUTHENTICATED"
    status, _, body = server.request("GET", "/v1/info", headers={"Authorization": f"Basic {good}"})
    assert status == 401
    status, _, body = server.op("beta", "task.create", {"title": "x"}, token=good)
    assert status == 403 and body["error"]["code"] == "FORBIDDEN"
    assert board_hash(root, "alpha") == before
    assert server.request("GET", "/v1/projects/beta/tasks", token=good)[0] == 403


def test_revoked_token_is_401(server: ServerHandle, root: Path) -> None:
    data = tokens.create_token(root, user="human:alice", machine="m", all_projects=True)
    tokens.revoke_token(root, data["record"]["id"])
    status, _, body = server.op("alpha", "task.create", {"title": "x"}, token=data["token"])
    assert status == 401 and body["error"]["code"] == "UNAUTHENTICATED"


def test_healthz_needs_no_credential(server: ServerHandle) -> None:
    status, _, body = server.request("GET", "/healthz")
    assert status == 200 and body["ok"] is True


def test_forbidden_before_existence(server: ServerHandle, root: Path) -> None:
    """A token without a project cannot learn whether it exists; a permitted one gets 404."""
    narrow = mint(root, projects=["alpha"])
    wide = mint(root)
    for slug in ("beta", "nope", "..", "Alpha"):
        assert server.op(slug, "task.create", {"title": "x"}, token=narrow)[0] == 403, slug
    status, _, body = server.op("nope", "task.create", {"title": "x"}, token=wide)
    assert status == 404 and body["error"]["code"] == "NOT_FOUND"
    assert server.op("Bad_Slug", "task.create", {"title": "x"}, token=wide)[0] == 403


# ---------------------------------------------------------------------------
# Dashboard login (AC-11, H-13b)
# ---------------------------------------------------------------------------


def _sessions(root: Path) -> list[dict]:
    path = root / "web_sessions.json"
    if not path.exists():
        return []
    return json.loads(path.read_text())["sessions"]


def test_login_form_answers_without_a_credential(server: ServerHandle) -> None:
    response = WebClient(server).get("/login")
    assert response.status == 200
    assert response.headers["content-type"].startswith("text/html")
    assert '<form method="post" action="/login"' in response.text
    assert "<script" not in response.text


def test_login_with_a_bad_token_is_401_and_no_session(server: ServerHandle, root: Path) -> None:
    good = mint(root, projects=["alpha"])
    for bad in [b for b in _bad_tokens(good) if b is not None]:
        web = WebClient(server)
        response = web.login(bad)
        assert response.status == 401, bad
        assert web.session is None
    tokens.revoke_token(root, tokens.parse_token(good)[0])
    assert WebClient(server).login(good).status == 401
    assert _sessions(root) == []


def test_login_with_a_foreign_origin_is_403_and_no_session(
    server: ServerHandle, root: Path
) -> None:
    good = mint(root, projects=["alpha"])
    for origin in ("http://evil.example", "null", None, server.url + ".evil.example"):
        web = WebClient(server)
        response = web.login(good, origin=origin)
        assert response.status == 403, origin
        assert web.session is None
    assert _sessions(root) == []


def test_login_sets_a_strict_http_only_cookie(server: ServerHandle, root: Path) -> None:
    web = WebClient(server)
    response = web.login(mint(root, projects=["alpha"]))
    assert response.status == 303
    assert response.headers["location"] == "/"
    (cookie,) = response.set_cookies()
    parts = [p.strip() for p in cookie.split(";")]
    assert parts[0].startswith("lattice_session=") and len(parts[0]) > 40
    lowered = {p.lower() for p in parts[1:]}
    assert {"httponly", "samesite=strict", "path=/"} <= lowered
    assert "secure" not in lowered  # plain HTTP, no trusted proxy
    assert len(_sessions(root)) == 1


def test_login_redirects_only_to_a_dashboard_path(server: ServerHandle, root: Path) -> None:
    token = mint(root, projects=["alpha"])
    for next_path, expected in [
        ("/p/alpha/", "/p/alpha/"),
        ("https://evil.example/", "/"),
        ("//evil.example/", "/"),
        ("/p/alpha/../../x", "/"),
        ("/\\evil.example", "/"),
    ]:
        response = WebClient(server).login(token, next_path=next_path)
        assert response.headers["location"] == expected, next_path


def test_secure_cookie_behind_a_trusted_proxy(root: Path) -> None:
    from lattice.server.testing import running_server

    token = mint(root, projects=["alpha"])
    with running_server(root, config={"trusted_proxies": ["127.0.0.1"]}) as server:
        web = WebClient(server)
        response = web.request(
            "POST",
            "/login",
            body=f"token={token}".encode(),
            headers={
                "Content-Type": "application/x-www-form-urlencoded",
                "Origin": "https://" + server.url.removeprefix("http://"),
                "X-Forwarded-Proto": "https",
            },
        )
        assert response.status == 303, response.text
        assert "secure" in response.set_cookies()[0].lower()


def test_forwarded_proto_is_ignored_without_trusted_proxy(
    server: ServerHandle, root: Path
) -> None:
    web = WebClient(server)
    response = web.request(
        "POST",
        "/login",
        body=f"token={mint(root)}".encode(),
        headers={
            "Content-Type": "application/x-www-form-urlencoded",
            "Origin": "https://" + server.url.removeprefix("http://"),
            "X-Forwarded-Proto": "https",
        },
    )
    assert response.status == 403


def test_index_requires_a_session(server: ServerHandle, root: Path) -> None:
    response = WebClient(server).get("/")
    assert response.status == 303 and response.headers["location"] == "/login"
    # A bearer token is not a browser session.
    bearer = WebClient(server).get("/", Authorization=f"Bearer {mint(root)}")
    assert bearer.status == 303 and bearer.headers["location"] == "/login"


def test_login_with_many_form_fields_is_an_ordinary_failure(
    server: ServerHandle, root: Path
) -> None:
    """A2 (H-13b review): extra fields under the 4 KiB cap never become a 500."""
    body = b"token=bad&a=1&b=1&c=1&d=1&e=1"
    headers = {"Content-Type": "application/x-www-form-urlencoded", "Origin": server.url}
    bad = WebClient(server).request("POST", "/login", body=body, headers=headers)
    assert bad.status == 401
    assert _sessions(root) == []
    good = f"token={mint(root)}&a=1&b=1&c=1&d=1&e=1".encode()
    web = WebClient(server)
    assert web.request("POST", "/login", body=good, headers=headers).status == 303
    assert web.session
