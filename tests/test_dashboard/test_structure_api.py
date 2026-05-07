"""Tests for the Structure Overview dashboard API (CEL-77)."""

from __future__ import annotations

import json
import shutil
import socket
import threading
from pathlib import Path
from urllib.request import Request, urlopen

import pytest

from lattice.core.config import default_config, serialize_config
from lattice.dashboard.server import create_server
from lattice.storage.fs import atomic_write, ensure_lattice_dirs

FIXTURES = Path(__file__).parent.parent / "fixtures" / "structure"


def _get(base_url: str, path: str) -> tuple[int, dict | str]:
    req = Request(f"{base_url}{path}")
    try:
        with urlopen(req) as resp:
            body = resp.read().decode("utf-8")
            return resp.status, json.loads(body)
    except Exception as exc:  # noqa: BLE001
        if hasattr(exc, "code"):
            body = exc.read().decode("utf-8")  # type: ignore[union-attr]
            return exc.code, json.loads(body)  # type: ignore[union-attr]
        raise


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _make_project(tmp_path: Path, *, project_type: str | None, with_fixtures: bool) -> Path:
    ensure_lattice_dirs(tmp_path)
    ld = tmp_path / ".lattice"
    config = dict(default_config())
    if project_type:
        config["project_type"] = project_type
    atomic_write(ld / "config.json", serialize_config(config))
    if with_fixtures:
        shutil.copy(FIXTURES / "structure.json", tmp_path / "structure.json")
        shutil.copy(FIXTURES / "events.jsonl", tmp_path / "events.jsonl")
    return ld


@pytest.fixture()
def structure_server(tmp_path: Path):
    ld = _make_project(tmp_path, project_type="structure", with_fixtures=True)
    port = _free_port()
    server = create_server(ld, "127.0.0.1", port)
    thread = threading.Thread(
        target=server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True
    )
    thread.start()
    try:
        yield f"http://127.0.0.1:{port}", tmp_path, ld
    finally:
        server.shutdown()
        server.server_close()


@pytest.fixture()
def standard_server(tmp_path: Path):
    ld = _make_project(tmp_path, project_type=None, with_fixtures=False)
    port = _free_port()
    server = create_server(ld, "127.0.0.1", port)
    thread = threading.Thread(
        target=server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True
    )
    thread.start()
    try:
        yield f"http://127.0.0.1:{port}"
    finally:
        server.shutdown()
        server.server_close()


class TestStructureApi:
    def test_structure_endpoint_returns_fixture_data(self, structure_server) -> None:
        base_url, root, _ld = structure_server
        status, body = _get(base_url, "/api/structure")
        assert status == 200
        assert body["ok"] is True
        data = body["data"]
        assert data["schema_version"] == 1
        assert data["structure"]["name"] == "demo-structure"
        assert data["totals"]["cells_alive"] == 3
        assert len(data["cells"]) == 3
        assert data["cells"][0]["name"] == "simple-1"

    def test_structure_events_endpoint_returns_events(self, structure_server) -> None:
        base_url, _root, _ld = structure_server
        status, body = _get(base_url, "/api/structure/events?limit=500")
        assert status == 200
        assert body["ok"] is True
        events = body["data"]["events"]
        # Fixture has 15 lines, one malformed → 14 parsed
        assert len(events) == 14
        assert events[0]["kind"] == "cell.spawned"
        assert events[-1]["kind"] == "task.accepted"
        assert all("frequency" in e and "ts" in e for e in events)

    def test_structure_events_respects_limit(self, structure_server) -> None:
        base_url, _root, _ld = structure_server
        _, body = _get(base_url, "/api/structure/events?limit=3")
        events = body["data"]["events"]
        assert len(events) <= 3
        assert body["data"]["truncated"] is True

    def test_structure_events_missing_file_returns_empty(self, tmp_path: Path) -> None:
        ld = _make_project(tmp_path, project_type="structure", with_fixtures=False)
        # structure.json exists? no. Write an empty structure.json so /api/structure
        # test isn't the one hitting this path — we're only exercising events here.
        (tmp_path / "structure.json").write_text('{"schema_version": 1}\n')
        port = _free_port()
        server = create_server(ld, "127.0.0.1", port)
        thread = threading.Thread(
            target=server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True
        )
        thread.start()
        try:
            status, body = _get(f"http://127.0.0.1:{port}", "/api/structure/events")
            assert status == 200
            assert body["data"]["events"] == []
        finally:
            server.shutdown()
            server.server_close()

    def test_standard_project_structure_forbidden(self, standard_server) -> None:
        base_url = standard_server
        status, body = _get(base_url, "/api/structure")
        assert status == 403
        assert body["error"]["code"] == "NOT_STRUCTURE_PROJECT"

    def test_standard_project_structure_events_forbidden(self, standard_server) -> None:
        base_url = standard_server
        status, body = _get(base_url, "/api/structure/events")
        assert status == 403
        assert body["error"]["code"] == "NOT_STRUCTURE_PROJECT"

    def test_structure_file_missing_returns_404(self, tmp_path: Path) -> None:
        ld = _make_project(tmp_path, project_type="structure", with_fixtures=False)
        port = _free_port()
        server = create_server(ld, "127.0.0.1", port)
        thread = threading.Thread(
            target=server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True
        )
        thread.start()
        try:
            status, body = _get(f"http://127.0.0.1:{port}", "/api/structure")
            assert status == 404
            assert body["error"]["code"] == "STRUCTURE_NOT_FOUND"
        finally:
            server.shutdown()
            server.server_close()
