"""Client-cache fixtures: a server-side board behind the STUB sync server, and a
bound client checkout whose remote points at it.

``server_root`` is the project directory whose ``.lattice/`` the stub serves;
``client_root`` is a checkout holding only ``.lattice-remote.json``, with the
remote ``team`` configured through the environment.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterator
from pathlib import Path

import pytest

from lattice.remote import cache
from lattice.storage.board_init import create_board
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


def bind(root: Path, url: str, token: str, monkeypatch: pytest.MonkeyPatch) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    (root / cache.BINDING_FILE).write_text(json.dumps({"remote": REMOTE, "project": PROJECT}))
    monkeypatch.setenv(f"LATTICE_REMOTE_{REMOTE.upper()}_URL", url)
    monkeypatch.setenv(f"LATTICE_REMOTE_{REMOTE.upper()}_TOKEN", token)
    return root


@pytest.fixture()
def client_root(tmp_path: Path, stub: StubServer, monkeypatch: pytest.MonkeyPatch) -> Path:
    return bind(tmp_path / "client", stub.url, stub.token, monkeypatch)


def create_task(stub: StubServer, title: str = "A task") -> str:
    result = stub.op("task.create", {"title": title})
    return result.task["id"]


def tree_hashes(lattice_dir: Path) -> dict[str, str]:
    return {
        rel: hashlib.sha256(path.read_bytes()).hexdigest()
        for rel, path in durable_files(lattice_dir).items()
    }


def assert_mirror(client: Path, stub: StubServer) -> None:
    """The cache holds exactly the server's synced files, byte for byte."""
    assert tree_hashes(client / ".lattice") == tree_hashes(stub.board)
