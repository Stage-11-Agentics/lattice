"""Server sync (SPEC §8.8): deltas, append deltas, resets, the manifest, the fast path."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import threading
import time
from collections.abc import Iterator
from pathlib import Path

import pytest

from lattice.server import syncstate
from lattice.server.testing import BoardServer, apply_sync, serve_board, wait_for
from tests.test_server.conftest import mint


@pytest.fixture()
def board(tmp_path: Path) -> Iterator[BoardServer]:
    with serve_board(tmp_path, audit=False) as served:
        yield served


def durable_hashes(lattice_dir: Path) -> dict[str, str]:
    return {
        rel: hashlib.sha256((lattice_dir / rel).read_bytes()).hexdigest()
        for rel in syncstate.synced_files(lattice_dir)
    }


def fetcher(board: BoardServer):  # noqa: ANN201
    def fetch(href: str) -> bytes:
        status, data = board.file_href(href)
        assert status == 200, data
        return data

    return fetch


def create(board: BoardServer, title: str = "t") -> str:
    return board.op("task.create", {"title": title})["task"]["id"]


def test_sync_from_zero_is_a_reset_that_mirrors_the_board(board: BoardServer, tmp_path: Path):
    create(board)
    body = board.sync()
    assert body["reset"] is True and body["removed"] == []
    assert body["head_seq"] == 1 and body["head_hash"]
    mirror = tmp_path / "mirror"
    apply_sync(mirror, body, fetcher(board))
    assert durable_hashes(mirror) == durable_hashes(board.board)
    for spec in body["files"].values():
        assert set(spec) == {"sha256", "size", "content_b64"}


def test_an_empty_board_has_no_head_hash(board: BoardServer) -> None:
    body = board.sync()
    assert body["head_seq"] == 0 and "head_hash" not in body


def test_delta_coalesces_paths_and_sends_append_deltas(board: BoardServer, tmp_path: Path):
    task = create(board)
    first = board.sync()
    mirror = tmp_path / "mirror"
    apply_sync(mirror, first, fetcher(board))
    log = f"events/{task}.jsonl"
    before = (board.board / log).stat().st_size
    board.op("task.comment", {"task": task, "text": "one"})
    board.op("task.comment", {"task": task, "text": "two"})
    delta = board.sync(since=first["head_seq"], epoch=first["epoch"], hash=first["head_hash"])
    assert delta["reset"] is False and delta["head_seq"] == 3
    spec = delta["files"][log]
    data = (board.board / log).read_bytes()
    assert spec["append_from"] == before
    assert base64.b64decode(spec["content_b64"]) == data[before:]
    assert spec["sha256"] == hashlib.sha256(data).hexdigest() and spec["size"] == len(data)
    assert spec["href"].startswith(f"/v1/projects/demo/files/{log}?sha256=")
    assert set(delta["files"]) == {log, f"tasks/{task}.json"}  # two comments, one entry each
    apply_sync(mirror, delta, fetcher(board))
    assert durable_hashes(mirror) == durable_hashes(board.board)


def test_a_log_created_after_since_is_sent_whole(board: BoardServer) -> None:
    first = board.sync()
    task = create(board)
    delta = board.sync(since=0, epoch=first["epoch"])
    spec = delta["files"][f"events/{task}.jsonl"]
    assert "append_from" not in spec and "content_b64" in spec
    lifecycle = delta["files"]["events/_lifecycle.jsonl"]
    assert lifecycle["append_from"] == 0  # in the baseline, empty when the epoch began


def test_archive_relocation_is_removed_and_the_append_base_is_dropped(
    board: BoardServer, tmp_path: Path
) -> None:
    task = create(board)
    mid = board.sync()
    mirror = tmp_path / "mirror"
    apply_sync(mirror, mid, fetcher(board))
    board.op("task.archive", {"task": task})
    delta = board.sync(since=mid["head_seq"], epoch=mid["epoch"], hash=mid["head_hash"])
    assert f"events/{task}.jsonl" in delta["removed"]
    assert f"tasks/{task}.json" in delta["removed"]
    assert "append_from" not in delta["files"][f"archive/events/{task}.jsonl"]
    apply_sync(mirror, delta, fetcher(board))
    assert durable_hashes(mirror) == durable_hashes(board.board)

    archived = board.sync(since=mid["head_seq"], epoch=mid["epoch"], hash=mid["head_hash"])
    board.op("task.unarchive", {"task": task})
    board.op("task.comment", {"task": task, "text": "back"})
    # From `mid` the active log's prefix is intact (relocation copies bytes): append delta.
    again = board.sync(since=mid["head_seq"], epoch=mid["epoch"], hash=mid["head_hash"])
    assert again["files"][f"events/{task}.jsonl"]["append_from"] > 0
    # While archived the active log did not exist: no append base, the whole file.
    whole = board.sync(
        since=archived["head_seq"], epoch=archived["epoch"], hash=archived["head_hash"]
    )
    assert "append_from" not in whole["files"][f"events/{task}.jsonl"]
    apply_sync(mirror, whole, fetcher(board))
    assert durable_hashes(mirror) == durable_hashes(board.board)


def test_large_files_travel_as_hash_pinned_hrefs(tmp_path: Path) -> None:
    with serve_board(
        tmp_path, audit=False, config={"limits": {"inline_file_bytes": 256}}
    ) as board:
        task = create(board, "x" * 300)
        body = board.sync()
        spec = body["files"][f"tasks/{task}.json"]
        assert "content_b64" not in spec and spec["href"].endswith(spec["sha256"])
        mirror = tmp_path / "mirror"
        apply_sync(mirror, body, fetcher(board))
        assert durable_hashes(mirror) == durable_hashes(board.board)
        # An append delta over the inline limit falls back to the whole-file href.
        board.op("task.comment", {"task": task, "text": "y" * 400})
        delta = board.sync(since=body["head_seq"], epoch=body["epoch"], hash=body["head_hash"])
        log = delta["files"][f"events/{task}.jsonl"]
        assert set(log) == {"sha256", "size", "href"}


def test_a_reset_inlines_up_to_the_cumulative_cap(
    board: BoardServer, monkeypatch: pytest.MonkeyPatch
) -> None:
    for _ in range(3):
        create(board)
    sizes = {rel: spec["size"] for rel, spec in board.sync()["files"].items()}
    cap = sum(sorted(sizes.values())[:4])
    monkeypatch.setattr(syncstate, "RESET_INLINE_CAP", cap)
    files = board.sync()["files"]
    inlined = sum(s["size"] for s in files.values() if "content_b64" in s)
    assert inlined <= cap
    assert any("href" in s for s in files.values())


@pytest.mark.parametrize(
    "query",
    [
        {"since": 1, "epoch": None},  # no epoch
        {"since": 1, "epoch": "ep_other"},  # another epoch
        {"since": 5},  # past the head (epoch filled in below)
        {"since": 1, "hash": "0" * 32},  # history mismatch
    ],
)
def test_mismatches_get_a_reset(board: BoardServer, query: dict) -> None:
    create(board)
    create(board)
    head = board.sync()
    query = dict(query)
    if "epoch" not in query:
        query["epoch"] = head["epoch"]
    if "hash" not in query and query["since"] <= head["head_seq"]:
        query["hash"] = board.project.journal.hash_at(query["since"])
    body = board.sync(**query)
    assert body["reset"] is True
    assert set(body["files"]) == set(syncstate.synced_files(board.board))


def test_the_manifest_follows_every_commit(board: BoardServer) -> None:
    task = create(board)
    for text in ("a", "b", "c"):
        board.op("task.comment", {"task": task, "text": text})
    board.op("task.plan_write", {"task": task, "file": "# plan\n"})
    manifest = board.project.manifest
    assert {rel: e.sha256 for rel, e in manifest.entries.items()} == durable_hashes(board.board)


def test_since_equal_to_head_is_answered_without_admission(board: BoardServer) -> None:
    create(board)
    head = board.sync()
    project = board.project
    held = threading.Event()
    release = threading.Event()

    async def hold() -> None:
        async with project.admission:
            held.set()
            while not release.is_set():
                await asyncio.sleep(0.01)

    runner = board.handle.run_on_loop(hold())
    try:
        assert held.wait(5)
        started = time.monotonic()
        body = board.sync(since=head["head_seq"], epoch=head["epoch"], hash=head["head_hash"])
        assert time.monotonic() - started < 1.0
        assert body == {
            "epoch": head["epoch"],
            "head_seq": head["head_seq"],
            "head_hash": head["head_hash"],
            "reset": False,
            "files": {},
            "removed": [],
        }
    finally:
        release.set()
        runner.result(5)


def test_since_must_be_a_non_negative_integer(board: BoardServer) -> None:
    for bad in ("-1", "x", "1.5"):
        status, _, body = board.handle.request(
            "GET", f"/v1/projects/demo/sync?since={bad}", token=board.token
        )
        assert status == 400 and body["error"]["code"] == "VALIDATION_ERROR"


def test_sync_needs_a_token_that_may_see_the_project(board: BoardServer) -> None:
    status, _, _ = board.handle.request("GET", "/v1/projects/demo/sync")
    assert status == 401
    outsider = mint(board.root, user="human:bob", projects=["other"])
    status, _, _ = board.handle.request("GET", "/v1/projects/demo/sync", token=outsider)
    assert status == 403
    status, _, _ = board.handle.request("GET", "/v1/projects/nope/sync", token=board.token)
    assert status == 403  # not listed for this token: 403 before any lookup
    everywhere = mint(board.root, user="human:carol")
    status, _, _ = board.handle.request("GET", "/v1/projects/nope/sync", token=everywhere)
    assert status == 404


def test_one_reset_is_assembled_at_a_time(board: BoardServer, monkeypatch) -> None:  # noqa: ANN001
    create(board)
    active = []
    peak = []
    real = syncstate.reset_body

    def slow_reset(*args, **kwargs):  # noqa: ANN002, ANN003, ANN202
        active.append(1)
        peak.append(len(active))
        time.sleep(0.2)
        try:
            return real(*args, **kwargs)
        finally:
            active.pop()

    monkeypatch.setattr("lattice.server.app.reset_body", slow_reset)
    results: list[dict] = []
    threads = [threading.Thread(target=lambda: results.append(board.sync())) for _ in range(3)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(10)
    assert len(results) == 3 and all(r["reset"] for r in results)
    assert max(peak) == 1
    assert wait_for(lambda: not board.project.reset_gate.locked(), 2)
