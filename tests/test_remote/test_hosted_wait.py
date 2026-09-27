"""Hosted ``lattice wait``: returns when a write from another client lands."""

from __future__ import annotations

import json
import threading
import time

from click.testing import CliRunner

from lattice.cli.main import cli
from tests.test_remote.hosted_board import HostedBoard


def _board(tmp_path, stream_stub, monkeypatch) -> HostedBoard:
    board = HostedBoard(tmp_path, stream_stub, monkeypatch)
    board.task = board.create("Waited task")
    board.syncer(board.b)
    return board


def _wait_while_a_moves(board: HostedBoard, *flags: str):
    task_id = board.task["id"]
    short = board.task.get("short_id") or task_id
    board.a_cli("status", task_id, "in_planning", "--actor", "human:test")
    timer = threading.Timer(0.3, board.publish)
    timer.start()
    try:
        result = CliRunner().invoke(
            cli,
            ["wait", short, "--status", "in_planning", "--timeout", "4", *flags],
            env={"LATTICE_ROOT": str(board.b)},
        )
    finally:
        timer.cancel()
    return short, result


def test_hosted_wait_json(tmp_path, stream_stub, monkeypatch) -> None:
    board = _board(tmp_path, stream_stub, monkeypatch)
    short, result = _wait_while_a_moves(board, "--json")
    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout) == {
        "ok": True,
        "data": {
            "all_complete": True,
            "completed": [short],
            "pending": [],
            "status": "in_planning",
        },
    }


def test_hosted_wait_plain_has_no_fswatch_line(tmp_path, stream_stub, monkeypatch) -> None:
    board = _board(tmp_path, stream_stub, monkeypatch)
    short, result = _wait_while_a_moves(board)
    assert result.exit_code == 0, result.output
    assert result.stdout.splitlines() == [
        "Waiting for 1/1 tasks to reach 'in_planning'...",
        f"  Progress: 1/1 ({short})",
        f"All 1 tasks reached 'in_planning': {short}",
    ]


def test_hosted_wait_times_out_like_local(tmp_path, stream_stub, monkeypatch) -> None:
    board = _board(tmp_path, stream_stub, monkeypatch)
    short = board.task.get("short_id") or board.task["id"]
    result = CliRunner().invoke(
        cli,
        ["wait", short, "--status", "done", "--timeout", "1"],
        env={"LATTICE_ROOT": str(board.b)},
    )
    assert result.exit_code == 1
    assert result.stdout.splitlines()[-1] == f"Timeout. Still pending: {short}"


def test_status_change_landing_during_the_initial_catch_up(
    tmp_path, stream_stub, monkeypatch
) -> None:
    """The change is on the server but not in the cache when wait first checks;
    the hosted catch-up brings it, and wait returns at once (finding 3)."""
    board = _board(tmp_path, stream_stub, monkeypatch)
    task_id = board.task["id"]
    short = board.task.get("short_id") or task_id
    board.a_cli("status", task_id, "in_planning", "--actor", "human:test")
    board.publish()  # the server has it; B's cache does not
    start = time.monotonic()
    result = CliRunner().invoke(
        cli,
        ["wait", short, "--status", "in_planning", "--timeout", "3", "--json"],
        env={"LATTICE_ROOT": str(board.b)},
    )
    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout)["data"]["completed"] == [short]
    assert time.monotonic() - start < 1.5
