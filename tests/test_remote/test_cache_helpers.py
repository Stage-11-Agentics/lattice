"""Unit tests for the cache's pure helpers: identity, fingerprint, path checks."""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from lattice.remote import cache


def _board(tmp_path: Path) -> Path:
    board = tmp_path / ".lattice"
    for rel in ("tasks", "events", "locks", "cache", "notes"):
        (board / rel).mkdir(parents=True)
    (board / "tasks" / "t.json").write_text("{}")
    (board / "events" / "t.jsonl").write_text("{}\n")
    (board / "config.json").write_text("{}")
    (board / "locks" / "x.lock").write_text("")
    (board / "cache" / "state.json").write_text("{}")
    (board / "runner.log").write_text("unmanaged")
    return board


def test_fingerprint_sees_durable_files_only(tmp_path: Path) -> None:
    board = _board(tmp_path)
    before = cache.fingerprint(board)
    assert cache.synced_files(board) == ["config.json", "events/t.jsonl", "tasks/t.json"]
    (board / "locks" / "y.lock").write_text("")
    (board / "runner.log").write_text("changed")
    (board / "cache" / "state.json").write_text('{"x": 1}')
    assert cache.fingerprint(board) == before


@pytest.mark.parametrize(
    "change",
    [
        lambda b: (b / "tasks" / "t.json").write_text('{"a": 1}'),
        lambda b: os.utime(b / "tasks" / "t.json", ns=(1, 1)),
        lambda b: (b / "notes" / "new.md").write_text("x"),
        lambda b: (b / "tasks" / "t.json").unlink(),
        lambda b: (b / "notes" / "link.md").symlink_to(b / "config.json"),
    ],
)
def test_fingerprint_changes_on_durable_edits(tmp_path: Path, change) -> None:
    board = _board(tmp_path)
    before = cache.fingerprint(board)
    change(board)
    assert cache.fingerprint(board) != before


def test_identity_order(tmp_path: Path) -> None:
    root = tmp_path
    assert cache.cache_identity(root) is None
    (root / ".lattice-remote.json").write_text(json.dumps({"remote": "b", "project": "p1"}))
    assert cache.cache_identity(root) == ("b", "p1")
    (root / ".lattice" / "cache").mkdir(parents=True)
    (root / ".lattice" / "cache" / "applying").write_text(
        json.dumps({"remote": "a", "project": "p2"})
    )
    assert cache.cache_identity(root) == ("a", "p2")
    (root / ".lattice" / "cache" / "state.json").write_text(
        json.dumps({"remote": "s", "project": "p3"})
    )
    assert cache.cache_identity(root) == ("s", "p3")


@pytest.mark.parametrize(
    "rel",
    [
        "../x.json",
        "tasks/../../x",
        "/etc/passwd",
        "tasks//x.json",
        "tasks/./x.json",
        "locks/x.lock",
        "review_state/t.json",
        ".daemon/x",
        "tmp-prompts/x",
        "cache/state.json",
        "hosted/journal.jsonl",
        "runner.log",
        "reviews/x.md",
        "tasks/.tmp.abc",
        "tasks\\x.json",
        "",
        None,
        7,
    ],
)
def test_unsafe_paths_are_refused(tmp_path: Path, rel: object) -> None:
    assert cache.unsafe_path_reason(rel, tmp_path / ".lattice") is not None


@pytest.mark.parametrize(
    "rel",
    ["tasks/x.json", "config.json", "orchestration/run/state.md", "archive/events/x.jsonl"],
)
def test_board_paths_are_accepted(tmp_path: Path, rel: str) -> None:
    assert cache.unsafe_path_reason(rel, tmp_path / ".lattice") is None


def test_a_symlinked_directory_is_refused(tmp_path: Path) -> None:
    board = tmp_path / ".lattice"
    board.mkdir()
    (tmp_path / "elsewhere").mkdir()
    (board / "tasks").symlink_to(tmp_path / "elsewhere")
    assert "symlink" in cache.unsafe_path_reason("tasks/x.json", board)
