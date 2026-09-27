"""Hosted ``lattice watch`` plain output and filters, on the real server and cache."""

from __future__ import annotations

import pytest

from lattice.cli.watch_cmd import _format_human
from tests.test_remote.hosted_board import real_hosted_board, watch_while_the_server_changes


@pytest.fixture
def board(tmp_path, monkeypatch):
    with real_hosted_board(tmp_path, monkeypatch) as board:
        board.task = board.create("Watched task")
        yield board


def test_hosted_watch_plain_and_filters(board) -> None:
    out, expected = watch_while_the_server_changes(board, "--type", "status_changed")
    wanted = [e for e in expected if e["type"] == "status_changed"]
    assert len(wanted) == 1
    assert out.splitlines() == [_format_human(e) for e in wanted]
