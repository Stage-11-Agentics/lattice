"""Regressions for review round 1 of PR #66 (auth and HTTP pipeline lens)."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from lattice.core.errors import OpError
from lattice.server.app import read_body
from lattice.server.testing import ServerHandle, running_server
from tests.test_server.conftest import mint


def test_unmatched_v1_paths_authenticate_first(server: ServerHandle, root: Path) -> None:
    """A2: an unknown /v1 path is 401 without a credential, 404 only with one."""
    token = mint(root)
    for path in ("/v1/nowhere", "/v1", "/v1/projects/alpha/nowhere", "/v1/projects/alpha/ops"):
        status, _, body = server.request("GET", path)
        assert status == 401 and body["error"]["code"] == "UNAUTHENTICATED", path
        assert server.request("POST", path, body={})[0] == 401
        status, _, body = server.request("GET", path, token=token)
        assert status == 404 and body["error"]["code"] == "NOT_FOUND", path
    status, _, body = server.request(
        "GET", "/v1/nowhere", token=token, headers={"Lattice-Protocol": "2"}
    )
    assert status == 400 and body["error"]["code"] == "PROTOCOL_MISMATCH"
    assert server.request("GET", "/elsewhere")[0] == 404


class _Uncopyable:
    """A chunk that cannot be copied: extending a buffer with it raises TypeError."""

    def __init__(self, size: int) -> None:
        self.size = size

    def __len__(self) -> int:
        return self.size


class _FakeRequest:
    def __init__(self, chunks: list, headers: dict | None = None) -> None:
        self.headers = headers or {}
        self._chunks = chunks

    async def stream(self):  # noqa: ANN201
        for chunk in self._chunks:
            yield chunk


class _State:
    def __init__(self, limit: int) -> None:
        from lattice.server.config import ServerConfig
        from lattice.server.limits import TokenLimits

        self.config = ServerConfig().with_limits(max_body_bytes=limit)
        self.limits = TokenLimits(self.config.limits)


class _Token:
    id = "tok_x"


def test_one_oversized_chunk_is_refused_before_it_is_copied() -> None:
    """A3: the limit is checked before a chunk is appended, so nothing is buffered past it."""
    state = _State(1024)
    with pytest.raises(OpError) as exc:
        asyncio.run(
            read_body(_FakeRequest([b"x" * 1000, _Uncopyable(50_000_000)]), state, _Token())
        )
    assert exc.value.code == "PAYLOAD_TOO_LARGE"
    body = asyncio.run(read_body(_FakeRequest([b"x" * 1000, b"y" * 24]), state, _Token()))
    assert len(body) == 1024
    with pytest.raises(OpError):
        asyncio.run(read_body(_FakeRequest([b"x" * 1024, b"y"]), state, _Token()))


def _journal(root: Path) -> list[dict]:
    path = root / "projects" / "alpha" / ".lattice" / "hosted" / "journal.jsonl"
    return [json.loads(line) for line in path.read_text().splitlines()]


def test_no_actor_operations_ignore_the_envelope_actor(root: Path) -> None:
    """A4: session.start runs as the token's default actor whatever actor the body names."""
    token = mint(root)  # [human:alice, agent:*]
    params = {"model": "m", "framework": "pytest", "name": "Orion"}
    with running_server(root) as server:
        for envelope in (
            {"actor": "human:bob"},
            {"actor": "agent:forged"},
            {"actor_name": "../../beta/.lattice/sessions/X-1"},
            {"actor": 5},
            {},
        ):
            status, _, body = server.op("alpha", "session.start", params, token=token, **envelope)
            assert status == 200, (envelope, body)
        requests = [x for x in server.log_lines if x.get("op") == "session.start"]
    assert {x["actor"] for x in requests} == {"human:alice"}
    lines = [x for x in _journal(root) if x["op"] == "session.start"]
    assert len(lines) == 5 and len({x["fp"] for x in lines}) == 1  # the actor never enters fp
    sessions = sorted((root / "projects" / "alpha" / ".lattice" / "sessions").glob("Orion-*.json"))
    assert len(sessions) == 5
    for session in sessions:
        assert json.loads(session.read_text())["origin"]["authenticated"]["actor"] == "human:alice"
