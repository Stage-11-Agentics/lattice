"""Hosted ``lattice wait`` on H-10a's real server and H-10b's real cache."""

from __future__ import annotations

import json
import threading
import time

import pytest

from tests.test_remote.hosted_board import real_hosted_board


@pytest.fixture
def board(tmp_path, monkeypatch):
    with real_hosted_board(tmp_path, monkeypatch) as board:
        board.task = board.create("Waited task")
        board.short = board.task["short_id"]
        from lattice.remote import cache

        cache.catch_up(board.b, bulk=True)
        yield board


def _wait_while_the_server_moves(board, *flags: str):
    timer = threading.Timer(0.3, board.status, (board.task["id"], "in_planning"))
    timer.start()
    try:
        return board.cli("wait", board.short, "--status", "in_planning", "--timeout", "4", *flags)
    finally:
        timer.cancel()


def test_hosted_wait_json(board) -> None:
    result = _wait_while_the_server_moves(board, "--json")
    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout) == {
        "ok": True,
        "data": {
            "all_complete": True,
            "completed": [board.short],
            "pending": [],
            "status": "in_planning",
        },
    }


def test_hosted_wait_plain_has_no_fswatch_line(board) -> None:
    result = _wait_while_the_server_moves(board)
    assert result.exit_code == 0, result.output
    assert result.stdout.splitlines() == [
        "Waiting for 1/1 tasks to reach 'in_planning'...",
        f"  Progress: 1/1 ({board.short})",
        f"All 1 tasks reached 'in_planning': {board.short}",
    ]


def test_hosted_wait_times_out_like_local(board) -> None:
    result = board.cli("wait", board.short, "--status", "done", "--timeout", "1")
    assert result.exit_code == 1
    assert result.stdout.splitlines()[-1] == f"Timeout. Still pending: {board.short}"


def test_status_change_landing_during_the_initial_catch_up(board) -> None:
    """The change is on the server but not in the cache when wait first checks;
    the hosted catch-up brings it, and wait returns at once."""
    board.status(board.task["id"], "in_planning")  # the server has it; the cache does not
    start = time.monotonic()
    result = board.cli("wait", board.short, "--status", "in_planning", "--timeout", "3", "--json")
    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout)["data"]["completed"] == [board.short]
    assert time.monotonic() - start < 1.5
