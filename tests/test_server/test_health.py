"""AC-31 (health and disk floor): /healthz answers while projects prewarm; below the
disk floor it reports 503 and operations get 507 STORAGE_LOW while reads still work."""

from __future__ import annotations

import threading
from pathlib import Path

import pytest

from lattice.server.project import Project
from lattice.server.testing import running_server, wait_for
from tests.test_server.conftest import board_hash, mint


def test_healthz_answers_during_the_prewarm(root: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    release = threading.Event()
    real_load = Project._load

    def slow_load(self: Project) -> None:
        release.wait(5)
        real_load(self)

    monkeypatch.setattr(Project, "_load", slow_load)
    with running_server(root, wait_prewarm=False) as server:
        status, _, body = server.request("GET", "/healthz", timeout=2)
        assert status == 200
        assert body["projects"]["loading"] == 1 and body["projects"]["unloaded"] == 1
        assert body["disk_free_bytes"] > 0 and body["protocol"] == 1
        assert set(body) == {"ok", "version", "protocol", "disk_free_bytes", "projects"}
        release.set()
        assert wait_for(lambda: server.request("GET", "/healthz")[2]["projects"]["loaded"] == 2)
        assert "alpha" not in str(server.request("GET", "/healthz")[2])


def test_disk_floor(root: Path) -> None:
    token = mint(root)
    with running_server(root, config={"limits": {"min_free_disk_bytes": 2**62}}) as server:
        status, _, body = server.request("GET", "/healthz")
        assert status == 503 and body["ok"] is False
        before = board_hash(root, "alpha")
        status, _, body = server.op("alpha", "task.create", {"title": "x"}, token=token)
        assert status == 507 and body["error"]["code"] == "STORAGE_LOW"
        assert board_hash(root, "alpha") == before
        assert server.request("GET", "/v1/projects/alpha/tasks", token=token)[0] == 200
        assert server.request("GET", "/v1/info", token=token)[0] == 200
