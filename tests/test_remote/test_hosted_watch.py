"""Hosted ``lattice watch`` on H-10a's real server and H-10b's real cache:
events arrive through the follower and print exactly as local ``watch`` would."""

from __future__ import annotations

import pytest

from lattice.cli.watch_cmd import _format_json
from tests.test_remote.hosted_board import real_hosted_board, watch_while_the_server_changes


@pytest.fixture
def board(tmp_path, monkeypatch):
    with real_hosted_board(tmp_path, monkeypatch) as board:
        board.task = board.create("Watched task")
        yield board


def test_hosted_watch_json_prints_the_local_lines(board) -> None:
    out, expected = watch_while_the_server_changes(board, "--json")
    assert {"status_changed", "comment_added"} <= {e["type"] for e in expected}
    assert out.splitlines() == [_format_json(e) for e in expected]
