"""Hosted ``lattice watch``: events arrive through the follower and print as local."""

from __future__ import annotations

import threading

import pytest
from click.testing import CliRunner

from lattice.cli.main import cli
from lattice.cli.watch_cmd import _format_human, _format_json
from tests.test_remote.hosted_board import HostedBoard, durable_files


@pytest.fixture
def board(tmp_path, stream_stub, monkeypatch) -> HostedBoard:
    board = HostedBoard(tmp_path, stream_stub, monkeypatch)
    board.task = board.create("Watched task")
    board.syncer(board.b)
    return board


def _watch_while_a_writes(board: HostedBoard, *flags: str) -> tuple[str, list[dict]]:
    before = durable_files(board.a)
    task_id = board.task["id"]
    board.a_cli("status", task_id, "in_planning", "--actor", "human:test")
    board.a_cli("comment", task_id, "hello from A", "--actor", "human:test")
    expected = board.new_events(before)

    timer = threading.Timer(0.3, board.publish)
    timer.start()
    try:
        result = CliRunner().invoke(
            cli, ["watch", "--timeout", "1", *flags], env={"LATTICE_ROOT": str(board.b)}
        )
    finally:
        timer.cancel()
    assert result.exit_code == 0, result.output
    return result.stdout, expected


def test_hosted_watch_json_prints_the_local_lines(board) -> None:
    out, expected = _watch_while_a_writes(board, "--json")
    assert len(expected) >= 2
    assert out.splitlines() == [_format_json(e) for e in expected]


def test_hosted_watch_plain_and_filters(board) -> None:
    out, expected = _watch_while_a_writes(board, "--type", "status_changed")
    wanted = [e for e in expected if e["type"] == "status_changed"]
    assert len(wanted) == 1
    assert out.splitlines() == [_format_human(e) for e in wanted]
