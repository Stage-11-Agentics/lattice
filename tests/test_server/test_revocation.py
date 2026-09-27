"""AC-13 (bearer): revocation and grants apply to the next request; two edits of
tokens.json within one mtime tick are both seen."""

from __future__ import annotations

import os
from pathlib import Path

from click.testing import CliRunner

from lattice.cli.main import cli
from lattice.server import tokens
from lattice.server.testing import ServerHandle
from lattice.server.tokens import TokenStore


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
