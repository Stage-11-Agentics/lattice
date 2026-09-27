"""AC-12: actor patterns, defaults, the built-in auto-review allowance, sessions, grants."""

from __future__ import annotations

import hashlib
from pathlib import Path

import pytest
from click.testing import CliRunner

from lattice.cli.main import cli
from lattice.server import tokens
from lattice.server.testing import ServerHandle
from lattice.storage.ownership import owning_board
from lattice.storage.sessions import create_session
from tests.test_server.conftest import board_hash, mint


def _create(server: ServerHandle, token: str, **envelope):
    return server.op("alpha", "task.create", {"title": "x"}, token=token, **envelope)


def _created_by(body: dict) -> str:
    return body["data"]["result"]["task"]["created_by"]


def test_default_person_token(server: ServerHandle, root: Path) -> None:
    data = tokens.create_token(root, user="human:alice", machine="m", all_projects=True)
    assert data["record"]["actors"] == ["human:alice", "agent:*"]
    assert data["warning"] is None
    token = data["token"]
    status, _, body = _create(server, token)
    assert status == 200 and _created_by(body) == "human:alice"
    assert _create(server, token, actor="agent:anything")[0] == 200
    status, _, body = _create(server, token, actor="human:bob")
    assert status == 403 and body["error"]["code"] == "ACTOR_NOT_PERMITTED"


def test_seat_token(server: ServerHandle, root: Path) -> None:
    data = tokens.create_token(
        root, user="human:alice", machine="seat-1", actors=["agent:owner-3"], all_projects=True
    )
    assert data["record"]["actors"] == ["agent:owner-3"]
    assert "not act as the user" in data["warning"]
    token, token_id = data["token"], data["record"]["id"]
    status, _, body = _create(server, token)
    assert status == 200 and _created_by(body) == "agent:owner-3"
    status, _, body = _create(server, token, actor="agent:other")
    assert status == 403
    message = body["error"]["message"]
    assert "`agent:other`" in message and token_id in message and "`agent:owner-3`" in message
    assert f"lattice server token grant {token_id} --actor" in message
    status, _, body = _create(server, token, actor="agent:lattice-auto-review")
    assert status == 200 and _created_by(body) == "agent:lattice-auto-review"


def test_wildcard_only_token_has_no_default(server: ServerHandle, root: Path) -> None:
    token = mint(root, actors=["agent:*"])
    status, _, body = _create(server, token)
    assert status == 400 and body["error"]["code"] == "MISSING_ACTOR"
    assert _create(server, token, actor="agent:x")[0] == 200
    assert _create(server, token, actor="human:y")[0] == 403


def test_two_literal_patterns_have_no_default(server: ServerHandle, root: Path) -> None:
    token = mint(root, actors=["human:alice", "agent:bot"])
    status, _, body = _create(server, token)
    assert status == 400 and body["error"]["code"] == "MISSING_ACTOR"
    assert _create(server, token, actor="agent:bot")[0] == 200


def test_invalid_actor_is_400(server: ServerHandle, root: Path) -> None:
    token = mint(root, actors=["*:*"])
    status, _, body = _create(server, token, actor="no-colon")
    assert status == 400 and body["error"]["code"] == "INVALID_ACTOR"


def _session(root: Path, slug: str, base_name: str) -> str:
    board = root / "projects" / slug / ".lattice"
    with owning_board(board):
        return create_session(
            board, base_name=base_name, model="test-model", framework="pytest"
        ).name


def _session_bytes(root: Path, slug: str, name: str) -> str:
    path = root / "projects" / slug / ".lattice" / "sessions" / f"{name}.json"
    return hashlib.sha256(path.read_bytes()).hexdigest()


def test_session_actor_checked_as_agent_base_name_before_any_write(
    server: ServerHandle, root: Path
) -> None:
    permitted = _session(root, "alpha", "Argus")
    refused = _session(root, "alpha", "Vesper")
    token = mint(root, actors=["agent:Argus"])
    before = _session_bytes(root, "alpha", refused)
    board_before = board_hash(root, "alpha")
    status, _, body = _create(server, token, actor_name=refused)
    assert status == 403 and body["error"]["code"] == "ACTOR_NOT_PERMITTED"
    assert "agent:Vesper" in body["error"]["message"]
    assert _session_bytes(root, "alpha", refused) == before
    assert board_hash(root, "alpha") == board_before
    status, _, body = _create(server, token, actor_name=permitted)
    assert status == 200, body
    assert _created_by(body)["base_name"] == "Argus"


@pytest.mark.parametrize(
    "name",
    ["../../beta/.lattice/sessions/Vesper-1", "../x", "..", ".", "a/b", "a\\b", "x\x00y", ""],
)
def test_unsafe_session_names_are_refused_before_any_read(
    server: ServerHandle, root: Path, name: str
) -> None:
    _session(root, "beta", "Vesper")
    token = mint(root, actors=["agent:*"])
    before = board_hash(root, "alpha")
    status, _, body = _create(server, token, actor_name=name)
    assert status == 400 and body["error"]["code"] == "VALIDATION_ERROR", body
    assert board_hash(root, "alpha") == before


def test_another_projects_session_is_not_found(server: ServerHandle, root: Path) -> None:
    other = _session(root, "beta", "Vesper")
    token = mint(root, actors=["agent:*"])
    status, _, body = _create(server, token, actor_name=other)
    assert status == 404 and body["error"]["code"] == "SESSION_NOT_FOUND"


def test_grant_and_ungrant_apply_to_the_next_request(server: ServerHandle, root: Path) -> None:
    data = tokens.create_token(root, user="human:alice", machine="m", projects=["alpha"])
    token, token_id = data["token"], data["record"]["id"]
    assert _create(server, token, actor="human:bob")[0] == 403
    runner = CliRunner()
    result = runner.invoke(
        cli,
        ["server", "token", "grant", token_id, "--actor", "human:bob", "--root", str(root)],
    )
    assert result.exit_code == 0, result.output
    assert _create(server, token, actor="human:bob")[0] == 200
    tokens.ungrant(root, token_id, actors=("human:bob",))
    assert _create(server, token, actor="human:bob")[0] == 403
    assert server.op("beta", "task.create", {"title": "x"}, token=token)[0] == 403
    tokens.grant(root, token_id, projects=("beta",))
    assert server.op("beta", "task.create", {"title": "x"}, token=token)[0] == 200


def test_token_create_warns_when_no_pattern_matches_the_user(root: Path) -> None:
    result = CliRunner().invoke(
        cli,
        [
            "server",
            "token",
            "create",
            "--user",
            "human:alice",
            "--machine",
            "m",
            "--actor",
            "agent:bot",
            "--root",
            str(root),
        ],
    )
    assert result.exit_code == 0
    assert "Warning: no --actor pattern matches human:alice" in result.output
