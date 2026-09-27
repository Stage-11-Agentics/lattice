"""AC-31 (health and disk floor): /healthz answers while projects prewarm; below the
disk floor it reports 503 and operations get 507 STORAGE_LOW while reads still work."""

from __future__ import annotations

import dataclasses
import threading
import time
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


def test_sync_works_under_the_disk_floor_and_an_empty_sync_logs_below_info(
    tmp_path: Path,
) -> None:
    """AC-31 (H-10a row): below the disk floor a sync still succeeds (delta, reset, and
    at the head); a sync with no change writes no log line at ``info``."""
    from lattice.server.testing import serve_board

    with serve_board(tmp_path, log_level="info") as board:
        task = board.op("task.create", {"title": "t"})["task"]["id"]
        board.op("task.comment", {"task": task, "text": "c"})
        board.handle.state.disk.minimum = 2**62
        status, _, body = board.handle.op(
            "demo",
            "task.comment",
            {"task": task, "text": "x"},
            token=board.token,
            actor=board.user,
        )
        assert status == 507 and body["error"]["code"] == "STORAGE_LOW"

        reset = board.sync()
        assert reset["reset"] is True and reset["head_seq"] == 2
        delta = board.sync(since=1, epoch=reset["epoch"], hash=board.project.journal.hash_at(1))
        assert delta["reset"] is False and delta["files"]

        def requests() -> list[dict]:
            return [x for x in board.handle.log_lines if x.get("event") == "request"]

        # A request's log line is written after its response: wait for all five
        # (two ops, the refused op, the reset, the delta) before counting.
        assert wait_for(lambda: len(requests()) == 5)
        head = board.sync(since=2, epoch=reset["epoch"], hash=reset["head_hash"])
        assert head["files"] == {} and head["removed"] == []
        # An empty delta assembled under the locks logs below info too.
        project = board.project
        state = project._state
        project._state = dataclasses.replace(
            state, journal=dataclasses.replace(state.journal, head=("elsewhere", 0, None))
        )
        try:  # the fast path no longer matches: assembled under the locks
            locked = board.sync(since=2, epoch=reset["epoch"], hash=reset["head_hash"])
        finally:
            project._state = state
        assert locked["files"] == {} and locked["reset"] is False
        time.sleep(0.2)
        assert len(requests()) == 5, requests()[5:]
