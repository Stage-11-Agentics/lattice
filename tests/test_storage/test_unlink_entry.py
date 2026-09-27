"""``unlink_entry``: remove a directory entry without following it, through the
same confinement, marker check, and recorder as every other primitive."""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from lattice.core.errors import BoardIsCache, BoardPathError
from lattice.storage.fs import recording, unlink_entry
from lattice.storage.ownership import syncing_board


def _board(tmp_path: Path) -> Path:
    board = tmp_path / "proj" / ".lattice"
    (board / "tasks").mkdir(parents=True)
    return board


def test_a_symlink_is_removed_and_its_outside_target_kept(tmp_path: Path) -> None:
    board = _board(tmp_path)
    outside = tmp_path / "outside.txt"
    outside.write_text("keep me")
    link = board / "tasks" / "link.json"
    link.symlink_to(outside)
    with recording() as recorder:
        unlink_entry(link)
    assert not os.path.lexists(link)
    assert outside.read_text() == "keep me"
    assert recorder.relative_paths(board) == ["tasks/link.json"]


def test_a_fifo_is_removed(tmp_path: Path) -> None:
    board = _board(tmp_path)
    fifo = board / "tasks" / "pipe"
    os.mkfifo(fifo)
    unlink_entry(fifo)
    assert not os.path.lexists(fifo)


def test_confinement_still_applies_to_the_parent(tmp_path: Path) -> None:
    board = _board(tmp_path)
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    (elsewhere / "victim").write_text("x")
    (board / "tasks" / "escape").symlink_to(elsewhere)
    with pytest.raises(BoardPathError):
        unlink_entry(board / "tasks" / "escape" / "victim")
    assert (elsewhere / "victim").exists()


def test_a_cache_refuses_without_the_syncer_flag(tmp_path: Path) -> None:
    board = _board(tmp_path)
    (board / "cache").mkdir()
    (board / "cache" / "state.json").write_text(json.dumps({"remote": "r", "project": "p"}))
    link = board / "tasks" / "link.json"
    link.symlink_to(tmp_path)
    with pytest.raises(BoardIsCache):
        unlink_entry(link)
    assert os.path.lexists(link)
    with syncing_board(board):
        unlink_entry(link)
    assert not os.path.lexists(link)
