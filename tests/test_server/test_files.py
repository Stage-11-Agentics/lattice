"""The files endpoint (SPEC §8.8): hash-pinned fetch, STALE_VERSION, board paths only."""

from __future__ import annotations

import hashlib
import http.client
import os
import urllib.parse
from collections.abc import Iterator
from pathlib import Path

import pytest

from lattice.server.testing import BoardServer, serve_board


@pytest.fixture()
def board(tmp_path: Path) -> Iterator[BoardServer]:
    with serve_board(tmp_path, audit=False) as served:
        yield served


def raw_get(board: BoardServer, path: str) -> tuple[int, bytes]:
    """A GET with the path sent exactly as given (no client-side normalization)."""
    parts = urllib.parse.urlsplit(board.url)
    conn = http.client.HTTPConnection(parts.hostname, parts.port, timeout=10)
    try:
        conn.request("GET", path, headers={"Authorization": f"Bearer {board.token}"})
        response = conn.getresponse()
        return response.status, response.read()
    finally:
        conn.close()


def test_a_pinned_fetch_returns_the_raw_bytes(board: BoardServer) -> None:
    task = board.op("task.create", {"title": "t"})["task"]["id"]
    rel = f"events/{task}.jsonl"
    data = (board.board / rel).read_bytes()
    status, body = board.file(rel, hashlib.sha256(data).hexdigest())
    assert status == 200 and body == data
    status, body = board.file(rel)  # unpinned: the current bytes
    assert status == 200 and body == data


def test_a_changed_file_is_stale(board: BoardServer) -> None:
    task = board.op("task.create", {"title": "t"})["task"]["id"]
    rel = f"events/{task}.jsonl"
    old = hashlib.sha256((board.board / rel).read_bytes()).hexdigest()
    board.op("task.comment", {"task": task, "text": "more"})
    status, body = board.file(rel, old)
    assert status == 412
    assert b'"STALE_VERSION"' in body


@pytest.mark.parametrize(
    "path",
    [
        "/v1/projects/demo/files/../config.json",
        "/v1/projects/demo/files/events/../../hosted/journal.jsonl",
        "/v1/projects/demo/files/events/%2e%2e/%2e%2e/hosted/journal.jsonl",
        "/v1/projects/demo/files//etc/passwd",
        "/v1/projects/demo/files/events/./_lifecycle.jsonl",
        "/v1/projects/demo/files/events%5C_lifecycle.jsonl",
    ],
)
def test_traversal_is_refused(board: BoardServer, path: str) -> None:
    status, body = raw_get(board, path)
    assert status in (400, 404), body
    assert b"root:" not in body and b'"seq"' not in body


@pytest.mark.parametrize(
    "rel", ["hosted/journal.jsonl", "hosted/journal_meta.json", "locks/x", "runner.log", "cache/a"]
)
def test_paths_that_are_not_synced_board_files_are_not_found(board: BoardServer, rel: str):
    status, _ = board.file(rel)
    assert status == 404


def test_a_symlink_out_of_the_board_is_not_followed(board: BoardServer, tmp_path: Path) -> None:
    secret = tmp_path / "secret.txt"
    secret.write_text("secret")
    os.symlink(secret, board.board / "notes" / "leak.md")
    status, body = board.file("notes/leak.md")
    assert status == 404 and b"secret" not in body


def test_a_directory_is_not_a_file(board: BoardServer) -> None:
    status, _ = board.file("events")
    assert status == 404


def test_a_pinned_fetch_of_a_file_that_moved_away_is_stale(board: BoardServer) -> None:
    """Review round 1, finding 3: sync, archive, then fetch the old href → 412, so the
    client syncs again instead of failing its catch-up."""
    task = board.op("task.create", {"title": "t"})["task"]["id"]
    body = board.sync()
    snapshot = body["files"][f"tasks/{task}.json"]
    board.op("task.archive", {"task": task})
    href = f"/v1/projects/demo/files/tasks/{task}.json?sha256={snapshot['sha256']}"
    status, data = board.file_href(href)
    assert status == 412 and b'"STALE_VERSION"' in data
    status, _ = board.file(f"tasks/{task}.json")  # unpinned: simply not found
    assert status == 404
    status, _ = board.file(f"archive/tasks/{task}.json", snapshot["sha256"])
    assert status in (200, 412)  # the archived copy: same bytes or a newer snapshot
