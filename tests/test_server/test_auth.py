"""AC-11 (bearer): missing, bad, revoked → 401; other project → 403; nothing appended."""

from __future__ import annotations

from pathlib import Path

from lattice.server import tokens
from lattice.server.testing import ServerHandle
from tests.test_server.conftest import board_hash, mint


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
