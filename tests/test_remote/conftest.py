"""Client-cache fixtures.

- ``server`` / ``client``: a project on the real Lattice server (H-10a's
  ``lattice.server.testing``) on ``127.0.0.1:0``, and a bound checkout whose
  remote points at it. Every test that needs only correct server answers uses
  these. One server runs per test module; each test gets its own new project
  and token, so tests stay independent without paying a server start each.
- ``stub`` / ``client_root``: the STUB sync server over ``server_root``, for the
  faults the real server cannot produce on demand (malformed or forced
  answers, held responses, corrupted hashes, restore manipulation).

A client checkout holds only ``.lattice-remote.json``, with the remote ``team``
configured through the environment.
"""

from __future__ import annotations

import hashlib
import json
import uuid
from collections.abc import Iterator
from pathlib import Path

import pytest

from lattice.remote import cache, http
from lattice.server import admin, tokens
from lattice.server.testing import BoardServer, ServerHandle, make_root, running_server
from lattice.storage.board_init import create_board
from tests.test_remote.stream_stub import StubServer as StreamStubServer
from tests.test_remote.stub_sync_server import StubServer, durable_files, running_stub

REMOTE = "team"
PROJECT = "demo"


@pytest.fixture()
def server_root(tmp_path: Path) -> Path:
    root = tmp_path / "server-project"
    root.mkdir()
    create_board(root, project_code="DEM", actor="human:stub")
    return root


@pytest.fixture()
def stub(server_root: Path) -> Iterator[StubServer]:
    with running_stub(server_root, slug=PROJECT) as server:
        yield server


def bind(
    root: Path,
    url: str,
    token: str,
    monkeypatch: pytest.MonkeyPatch,
    *,
    project: str = PROJECT,
) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    (root / cache.BINDING_FILE).write_text(json.dumps({"remote": REMOTE, "project": project}))
    monkeypatch.setenv(f"LATTICE_REMOTE_{REMOTE.upper()}_URL", url)
    monkeypatch.setenv(f"LATTICE_REMOTE_{REMOTE.upper()}_TOKEN", token)
    return root


@pytest.fixture()
def client_root(tmp_path: Path, stub: StubServer, monkeypatch: pytest.MonkeyPatch) -> Path:
    return bind(tmp_path / "client", stub.url, stub.token, monkeypatch)


@pytest.fixture(scope="module")
def live_server(tmp_path_factory: pytest.TempPathFactory) -> Iterator[ServerHandle]:
    """One real server for the module; tests add their own projects to it."""
    root = make_root(tmp_path_factory.mktemp("live-server"))
    with running_server(root) as handle:
        yield handle


def new_project(handle: ServerHandle, code: str = "DEM") -> BoardServer:
    """A new project on *handle*'s running server, with a token for it."""
    slug = f"p{uuid.uuid4().hex[:12]}"
    admin.create_project(handle.root, slug, code=code)
    minted = tokens.create_token(handle.root, user="human:alice", machine="test", projects=[slug])
    return BoardServer(handle, slug, minted["token"], minted["record"]["id"], "human:alice")


@pytest.fixture()
def server(live_server: ServerHandle) -> BoardServer:
    return new_project(live_server)


@pytest.fixture()
def client(tmp_path: Path, server: BoardServer, monkeypatch: pytest.MonkeyPatch) -> Path:
    return bind(tmp_path / "client", server.url, server.token, monkeypatch, project=server.slug)


def create_task(server: StubServer | BoardServer, title: str = "A task") -> str:
    result = server.op("task.create", {"title": title})
    return result["task"]["id"] if isinstance(result, dict) else result.task["id"]


def record_requests(monkeypatch: pytest.MonkeyPatch) -> list[list]:
    """Record every request the client transport makes, as ``[path, response]``;
    each is listed as it starts (``response`` is ``None`` until it answers)."""
    calls: list[list] = []
    real = http.request

    def recording(remote, method, path, **kwargs):  # noqa: ANN001, ANN202
        entry: list = [path, None]
        calls.append(entry)
        entry[1] = real(remote, method, path, **kwargs)
        return entry[1]

    monkeypatch.setattr(http, "request", recording)
    return calls


def tree_hashes(lattice_dir: Path) -> dict[str, str]:
    return {
        rel: hashlib.sha256(path.read_bytes()).hexdigest()
        for rel, path in durable_files(lattice_dir).items()
    }


def assert_mirror(client: Path, server: StubServer | BoardServer) -> None:
    """The cache holds exactly the server's synced files, byte for byte."""
    assert tree_hashes(client / ".lattice") == tree_hashes(server.board)


@pytest.fixture()
def stream_stub() -> Iterator[StreamStubServer]:
    """H-10c: a §8.8/§8.9 stub for the follower's fault cases (proxies, forced failures)."""
    server = StreamStubServer(heartbeat_seconds=0.2)
    server.files = {"config.json": b'{"project_code": "DEM"}\n', "events/T1.jsonl": b""}
    with server.running():
        yield server
from tests.test_remote.hosted import hosted_env  # noqa: F401 - H-11 end-to-end fixture
