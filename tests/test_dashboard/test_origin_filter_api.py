"""AC-39 (dashboard): ``GET /api/tasks?machine=&user=&worktree=``.

The dashboard filters by origin with the CLI's rule: the same board answers
the same task IDs through the API as through ``lattice list --json``. Tasks
written before v2 carry no origin and never match; no parameter is today's
list. The board fixture is the CLI test's own, so both sides read one board.
"""

from __future__ import annotations

import getpass
import json
import socket
import threading
from pathlib import Path
from urllib.parse import urlencode
from urllib.request import urlopen

from click.testing import CliRunner

from lattice.cli.main import cli
from lattice.dashboard import api
from lattice.dashboard.server import create_server
from tests.test_cli.test_list_origin import ACTOR, board  # noqa: F401 - shared board fixture


def _ld(board: dict) -> Path:  # noqa: F811
    return Path(board["env"]["LATTICE_ROOT"]) / ".lattice"


def _api_rows(board: dict, **params: str) -> list[dict]:  # noqa: F811
    response = api.route_get(_ld(board), "/api/tasks", urlencode(params))
    assert response.status == 200, response.envelope
    assert response.envelope["ok"] is True
    return response.envelope["data"]


def _api_ids(board: dict, **params: str) -> list[str]:  # noqa: F811
    return [row["id"] for row in _api_rows(board, **params)]


def _cli_ids(board: dict, *args: str) -> list[str]:  # noqa: F811
    result = CliRunner().invoke(cli, ["list", "--json", *args], env=board["env"])
    assert result.exit_code == 0, result.output
    return [task["id"] for task in json.loads(result.output)["data"]]


def _cases(board: dict) -> list[dict[str, str]]:  # noqa: F811
    worktree = str(board["repo"].resolve())
    host, user = socket.gethostname(), getpass.getuser()
    return [
        {"machine": host},
        {"machine": "alice-laptop"},
        {"machine": "lap"},  # reported host loses to authenticated
        {"user": user},
        {"user": "human:alice"},
        {"user": "alice"},
        {"worktree": worktree},
        {"worktree": "/srv/wt-auth"},
        {"worktree": "/nowhere"},
        {"user": "human:x", "machine": "m1"},
        {"user": "human:x", "machine": "m2"},  # split across two events: no match
        {"user": "human:alice", "worktree": "/srv/wt-auth"},
        {"user": user, "machine": host, "worktree": worktree},
        {"user": "human:alice", "machine": "alice-laptop", "worktree": worktree},
    ]


def test_each_filter_and_combination_agrees_with_the_cli(board: dict) -> None:  # noqa: F811
    seen_nonempty = 0
    for params in _cases(board):
        args = [a for key, value in params.items() for a in (f"--{key}", value)]
        expected = _cli_ids(board, *args)
        assert _api_ids(board, **params) == expected, params
        seen_nonempty += bool(expected)
    assert seen_nonempty >= 6  # the cases exercise matches, not only misses


def test_expected_matches(board: dict) -> None:  # noqa: F811
    assert _api_ids(board, machine="alice-laptop") == [board["served"]]
    assert _api_ids(board, user=getpass.getuser()) == [board["local"]]
    assert _api_ids(board, worktree=str(board["repo"].resolve())) == [board["local"]]
    assert _api_ids(board, user="human:x", machine="m1") == [board["split"]]
    assert _api_ids(board, user="human:x", machine="m2") == []


def test_no_filter_is_todays_list(board: dict) -> None:  # noqa: F811
    rows = _api_rows(board)
    assert rows == api.get_tasks(_ld(board))
    assert [row["id"] for row in rows] == _cli_ids(board)
    assert board["legacy"] in [row["id"] for row in rows]
    # Unrelated and empty parameters change nothing (an empty value is no filter).
    assert _api_rows(board, machine="", user="", worktree="", tag="x") == rows


def test_filtered_rows_are_todays_rows(board: dict) -> None:  # noqa: F811
    by_id = {row["id"]: row for row in _api_rows(board)}
    for row in _api_rows(board, user="human:alice"):
        assert row == by_id[row["id"]]


def test_pre_v2_tasks_never_match(board: dict) -> None:  # noqa: F811
    for params in (
        {"machine": socket.gethostname()},
        {"user": getpass.getuser()},
        {"user": "human:old"},
        {"worktree": str(board["repo"].resolve())},
    ):
        assert board["legacy"] not in _api_ids(board, **params)


def test_archived_tasks_stay_off_the_board(board: dict) -> None:  # noqa: F811
    assert _api_ids(board, user="human:alice") == [board["served"]]
    assert board["archived"] not in _api_ids(board, user="human:alice")


def test_erased_task_never_listed(board: dict) -> None:  # noqa: F811
    result = CliRunner().invoke(
        cli, ["erase", board["served"], "--reason", "gone", "--actor", ACTOR], env=board["env"]
    )
    assert result.exit_code == 0, result.output
    assert _api_ids(board, user="human:alice") == []
    assert _cli_ids(board, "--user", "human:alice") == []


def test_and_with_status(board: dict) -> None:  # noqa: F811
    """The page's own filters (status lanes, tag, assignee, creator) apply to
    the rows the server returns, so they AND with the origin filters."""
    moved = CliRunner().invoke(
        cli, ["status", board["local"], "in_planning", "--actor", ACTOR], env=board["env"]
    )
    assert moved.exit_code == 0, moved.output
    rows = _api_rows(board, user=getpass.getuser())
    assert [(r["id"], r["status"]) for r in rows] == [(board["local"], "in_planning")]
    assert [r["id"] for r in rows if r["status"] == "in_planning"] == _cli_ids(
        board, "--user", getpass.getuser(), "--status", "in_planning"
    )


def test_absolute_worktree_normalizes_as_the_cli(board: dict) -> None:  # noqa: F811
    repo = str(board["repo"].resolve())
    for worktree in (
        "/srv/wt-auth/",
        "/srv//wt-auth",
        "/srv/./wt-auth/.",
        "/srv/other/../wt-auth",
        "//srv/wt-auth",
        repo + "/",
        repo + "/sub/..",
    ):
        expected = _cli_ids(board, "--worktree", worktree)
        assert _api_ids(board, worktree=worktree) == expected, worktree
    assert _api_ids(board, worktree="/srv/wt-auth/") == [board["served"]]


def _refused(board: dict, **params: str) -> str:  # noqa: F811
    response = api.route_get(_ld(board), "/api/tasks", urlencode(params))
    assert response.status == 400, response.envelope
    assert response.envelope["ok"] is False
    assert response.envelope["error"]["code"] == "VALIDATION_ERROR"
    return response.envelope["error"]["message"]


def test_client_relative_worktree_is_refused(board: dict) -> None:  # noqa: F811
    """The CLI resolves ``.`` and ``~`` against the caller's directory; a
    server cannot, so the API refuses them rather than silently missing."""
    for worktree in (".", "wt", "~/wt"):
        assert "absolute path" in _refused(board, worktree=worktree)


def test_symlink_alias_is_not_resolved(board: dict, tmp_path: Path) -> None:  # noqa: F811
    """The CLI resolves a symlink on the caller's machine; the server never
    resolves a caller's path on its own filesystem, so an alias misses."""
    link = tmp_path / "link"
    link.symlink_to(board["repo"])
    assert _cli_ids(board, "--worktree", str(link)) == [board["local"]]
    assert _api_ids(board, worktree=str(link)) == []


def test_length_caps(board: dict) -> None:  # noqa: F811
    assert _api_ids(board, machine="m" * 256) == []
    assert _api_ids(board, user="u" * 256) == []
    assert _api_ids(board, worktree="/" + "w" * 1023) == []
    assert "256" in _refused(board, machine="m" * 257)
    assert "256" in _refused(board, user="u" * 257)
    assert "1024" in _refused(board, worktree="/" + "w" * 1024)


def test_over_http(board: dict) -> None:  # noqa: F811
    """The local dashboard's transport passes the query through."""
    server = create_server(_ld(board), "127.0.0.1", 0)
    port = server.server_address[1]
    thread = threading.Thread(
        target=server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True
    )
    thread.start()
    try:
        query = urlencode({"user": "human:alice", "machine": "alice-laptop"})
        with urlopen(f"http://127.0.0.1:{port}/api/tasks?{query}", timeout=5) as resp:
            body = json.loads(resp.read())
    finally:
        server.shutdown()
        server.server_close()
    assert [row["id"] for row in body["data"]] == [board["served"]]
