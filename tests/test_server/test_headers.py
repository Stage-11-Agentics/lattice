"""SPEC §8.4: the Lattice-* headers on every response; no-store on /v1; envelopes."""

from __future__ import annotations

from pathlib import Path

from lattice.server.protocol import MIN_CLIENT_VERSION, server_version
from lattice.server.testing import ServerHandle
from tests.test_server.conftest import mint


def test_headers_everywhere(server: ServerHandle, root: Path) -> None:
    token = mint(root)
    responses = [
        server.request("GET", "/healthz"),
        server.request("GET", "/v1/info", token=token),
        server.request("GET", "/v1/info"),
        server.request("GET", "/v1/projects", token=token),
        server.op("alpha", "task.create", {"title": "x"}, token=token),
        server.op("alpha", "task.create", {}, token=token),
        server.request("GET", "/v1/nowhere", token=token),
        server.request("GET", "/elsewhere"),
    ]
    for status, headers, body in responses:
        assert headers["lattice-protocol"] == "1", status
        assert headers["lattice-server-version"] == server_version()
        assert headers["lattice-min-client-version"] == MIN_CLIENT_VERSION
        assert isinstance(body, dict) and "ok" in body
    for status, headers, _ in responses[1:7]:
        assert headers["cache-control"] == "no-store"
    assert "cache-control" not in responses[0][1]
    assert responses[6][0] == 404 and responses[7][0] == 404


def test_success_envelope(server: ServerHandle, root: Path) -> None:
    token = mint(root)
    status, _, body = server.op("alpha", "task.create", {"title": "x"}, token=token)
    assert status == 200 and body["ok"] is True
    data = body["data"]
    assert set(data) == {"result", "seq", "op_id"}
    assert {"task", "events", "value", "idempotent", "replayed"} <= set(data["result"])
    assert "paths" not in data["result"]
    assert data["seq"] == 1 and data["result"]["replayed"] is False


def test_uvicorn_never_trusts_forwarded_headers_itself() -> None:
    """uvicorn rewrites the scheme and client from ``X-Forwarded-*`` sent from
    127.0.0.1 by default; only ``trusted_proxies`` may (SPEC §8.1)."""
    from lattice.server.config import ServerConfig
    from lattice.server.serve import uvicorn_options

    assert uvicorn_options(ServerConfig())["proxy_headers"] is False
