"""AC-13 (bearer): revocation and grants apply to the next request; two edits of
tokens.json within one mtime tick are both seen."""

from __future__ import annotations

import os
from pathlib import Path

import pytest
from click.testing import CliRunner

from lattice.cli.main import cli
from lattice.server import tokens
from lattice.server.testing import ServerHandle, open_stream, running_server
from lattice.server.tokens import TokenStore
from tests.test_server.web_client import WebClient


def test_revoke_via_admin_cli_while_running(server: ServerHandle, root: Path) -> None:
    data = tokens.create_token(root, user="human:alice", machine="m", all_projects=True)
    token = data["token"]
    assert server.request("GET", "/v1/info", token=token)[0] == 200
    result = CliRunner().invoke(
        cli, ["server", "token", "revoke", data["record"]["id"], "--root", str(root)]
    )
    assert result.exit_code == 0, result.output
    status, _, body = server.request("GET", "/v1/info", token=token)
    assert status == 401 and body["error"]["code"] == "UNAUTHENTICATED"


def test_two_edits_within_one_mtime_tick_are_both_seen(root: Path) -> None:
    store = TokenStore(root)
    first = tokens.create_token(root, user="human:a", machine="m", all_projects=True)
    path = root / "tokens.json"
    stamp = path.stat().st_mtime_ns
    store.refresh()
    assert store.get(first["record"]["id"]) is not None
    second = tokens.create_token(root, user="human:b", machine="m", all_projects=True)
    os.utime(path, ns=(stamp, stamp))
    assert store.get(second["record"]["id"]) is not None
    tokens.revoke_token(root, second["record"]["id"])
    os.utime(path, ns=(stamp, stamp))
    assert store.get(second["record"]["id"]).revoked_at is not None


def test_a_broken_tokens_file_fails_closed(root: Path) -> None:
    """A hand edit that breaks tokens.json (say, while revoking a token) authenticates
    nobody until the file parses again; it never leaves the old registry in force."""
    import pytest

    from lattice.core.errors import OpError

    data = tokens.create_token(root, user="human:a", machine="m", all_projects=True)
    good = (root / "tokens.json").read_text()
    reloads = []
    store = TokenStore(root, on_reload=lambda **f: reloads.append(f))
    assert store.authenticate(f"Bearer {data['token']}").user == "human:a"
    (root / "tokens.json").write_text("{not json")
    with pytest.raises(OpError) as exc:
        store.authenticate(f"Bearer {data['token']}")
    assert exc.value.code == "UNAUTHENTICATED"
    assert reloads[-1]["ok"] is False
    (root / "tokens.json").write_text(good)
    assert store.authenticate(f"Bearer {data['token']}").user == "human:a"


# ---------------------------------------------------------------------------
# Dashboard sessions (AC-13, H-13b)
# ---------------------------------------------------------------------------


def _session_stream(server: ServerHandle, web: WebClient):
    return open_stream(
        server.url, "alpha", None, headers={"Cookie": f"lattice_session={web.session}"}
    )


def test_revoking_the_token_ends_its_sessions(root: Path) -> None:
    data = tokens.create_token(root, user="human:alice", machine="m", all_projects=True)
    with running_server(root, heartbeat_seconds=0.2) as server:
        web = WebClient(server)
        assert web.login(data["token"]).status == 303
        assert web.get("/p/alpha/api/tasks").status == 200
        stream = _session_stream(server, web)
        assert stream.status == 200
        stream.next_of("heartbeat")
        result = CliRunner().invoke(
            cli, ["server", "token", "revoke", data["record"]["id"], "--root", str(root)]
        )
        assert result.exit_code == 0, result.output
        assert web.get("/p/alpha/api/tasks").status == 401
        assert web.get("/p/alpha/").status == 303
        with pytest.raises(EOFError):
            stream.next_of("journal", timeout=5)
        stream.close()


def test_logout_ends_the_session_and_its_stream(root: Path) -> None:
    token = tokens.create_token(root, user="human:alice", machine="m", all_projects=True)["token"]
    with running_server(root, heartbeat_seconds=0.2) as server:
        web = WebClient(server)
        web.login(token)
        cookie = web.session
        stream = _session_stream(server, web)
        stream.next_of("heartbeat")
        assert web.logout(origin="http://evil.example").status == 403
        assert web.get("/p/alpha/api/tasks").status == 200
        response = web.logout()
        assert response.status == 200, response.text
        assert web.session is None
        web.cookies["lattice_session"] = cookie  # a copy kept after logout
        assert web.get("/p/alpha/api/tasks").status == 401
        with pytest.raises(EOFError):
            stream.next_of("journal", timeout=5)
        stream.close()


def test_an_expired_session_is_refused_and_pruned(root: Path) -> None:
    import json

    token = tokens.create_token(root, user="human:alice", machine="m", all_projects=True)["token"]
    with running_server(root) as server:
        web = WebClient(server)
        web.login(token)
        path = root / "web_sessions.json"
        body = json.loads(path.read_text())
        body["sessions"][0]["expires_at"] = "2000-01-01T00:00:00Z"
        path.write_text(json.dumps(body))
        assert web.get("/p/alpha/api/tasks").status == 401
        WebClient(server).login(token)  # any write prunes the expired session
        assert len(json.loads(path.read_text())["sessions"]) == 1


def test_a_session_cannot_reach_another_project_after_ungrant(root: Path) -> None:
    data = tokens.create_token(root, user="human:alice", machine="m", projects=("alpha",))
    with running_server(root) as server:
        web = WebClient(server)
        web.login(data["token"])
        assert web.get("/p/alpha/api/tasks").status == 200
        tokens.ungrant(root, data["record"]["id"], projects=("alpha",))
        assert web.get("/p/alpha/api/tasks").status == 403


# ---------------------------------------------------------------------------
# A1 (H-13b review): a dead session cookie is cleared on every answer
# ---------------------------------------------------------------------------


def _clears(response) -> bool:
    return any(
        c.startswith("lattice_session=;") and "max-age=0" in c.lower()
        for c in response.set_cookies()
    )


def _kill(kind: str, root: Path, token_id: str, web: WebClient) -> None:
    import json

    path = root / "web_sessions.json"
    if kind == "expired":
        body = json.loads(path.read_text())
        for session in body["sessions"]:
            session["expires_at"] = "2000-01-01T00:00:00Z"
        path.write_text(json.dumps(body))
    elif kind == "revoked":
        tokens.revoke_token(root, token_id)
    elif kind == "deleted":
        path.write_text(json.dumps({"sessions": []}))
    elif kind == "malformed":
        web.cookies["lattice_session"] = "not-a-session!"


@pytest.mark.parametrize("kind", ["expired", "revoked", "deleted", "malformed"])
def test_a_dead_session_cookie_is_cleared_everywhere(root: Path, kind: str) -> None:
    data = tokens.create_token(root, user="human:alice", machine="m", all_projects=True)
    with running_server(root) as server:
        web = WebClient(server)
        assert web.login(data["token"]).status == 303
        _kill(kind, root, data["record"]["id"], web)
        cookie = web.session
        responses = {
            "api": web.request(
                "GET",
                "/p/alpha/api/tasks",
                headers={"Cookie": f"lattice_session={cookie}"},
                send_cookies=False,
            ),
            "page": web.request(
                "GET",
                "/p/alpha/",
                headers={"Cookie": f"lattice_session={cookie}"},
                send_cookies=False,
            ),
            "index": web.request(
                "GET", "/", headers={"Cookie": f"lattice_session={cookie}"}, send_cookies=False
            ),
        }
        assert responses["api"].status == 401
        assert responses["page"].status == 303 and responses["index"].status == 303
        for where, response in responses.items():
            assert _clears(response), (kind, where)
        stream = open_stream(
            server.url, "alpha", None, headers={"Cookie": f"lattice_session={cookie}"}
        )
        assert stream.status == 401
        assert any(
            v.startswith("lattice_session=;")
            for k, v in stream.response.getheaders()
            if k.lower() == "set-cookie"
        ), kind
        stream.close()


def test_no_cookie_and_bearer_failures_set_no_cookie(root: Path) -> None:
    with running_server(root) as server:
        anon = WebClient(server)
        assert not anon.get("/p/alpha/api/tasks").set_cookies()
        bearer = anon.request(
            "GET",
            "/p/alpha/api/tasks",
            headers={"Authorization": "Bearer nope", "Cookie": "lattice_session=x"},
            send_cookies=False,
        )
        assert bearer.status == 401 and not bearer.set_cookies()
