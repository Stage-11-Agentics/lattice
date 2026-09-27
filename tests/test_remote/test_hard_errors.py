"""Hard sync errors (``OpError`` from ``catch_up``) reach every command
boundary as today's typed error envelope and exit code (finding 4)."""

from __future__ import annotations

import json

import pytest
from click.testing import CliRunner

from lattice.cli.main import cli
from tests.test_remote.hosted_board import StubHostedBoard

HTML = (200, {"Content-Type": "text/html"}, b"<html>login</html>")
UNAUTH = (
    401,
    {"Content-Type": "application/json", "Lattice-Protocol": "1"},
    b'{"ok": false, "error": {"code": "UNAUTHENTICATED", "message": "token revoked"}}',
)
PROTO = (200, {"Content-Type": "application/json", "Lattice-Protocol": "2"}, b"{}")
MALFORMED = (
    200,
    {"Content-Type": "application/json", "Lattice-Protocol": "1"},
    b'{"ok": true, "data": {"epoch": 5}}',
)
JSON_HEADERS = {"Content-Type": "application/json", "Lattice-Protocol": "1"}


def _envelope_error(status: int, code: str) -> tuple[int, dict[str, str], bytes]:
    body = {"ok": False, "error": {"code": code, "message": f"{code} from the server"}}
    return status, JSON_HEADERS, json.dumps(body).encode()


INTEGRITY_500 = _envelope_error(500, "INTEGRITY_ERROR")
CASES = [
    (INTEGRITY_500, "INTEGRITY_ERROR"),
    (_envelope_error(507, "STORAGE_LOW"), "STORAGE_LOW"),
    (_envelope_error(500, "SOMETHING_NEW"), "SOMETHING_NEW"),
    (HTML, "PROXY_REJECTED"),
    (UNAUTH, "UNAUTHENTICATED"),
    (PROTO, "PROTOCOL_MISMATCH"),
    (MALFORMED, "INTEGRITY_ERROR"),
]


@pytest.fixture
def board(tmp_path, stream_stub, monkeypatch) -> StubHostedBoard:
    board = StubHostedBoard(tmp_path, stream_stub, monkeypatch)
    board.task = board.create("Task")
    board.syncer(board.b)
    return board


def _run(board: StubHostedBoard, *args: str):
    return CliRunner().invoke(cli, list(args), env={"LATTICE_ROOT": str(board.b)})


def _commands(board: StubHostedBoard) -> list[list[str]]:
    short = board.task.get("short_id") or board.task["id"]
    return [
        ["sync"],
        ["watch", "--timeout", "2"],
        ["wait", short, "--status", "done", "--timeout", "2"],
    ]


@pytest.mark.parametrize(("response", "code"), CASES, ids=[c for _, c in CASES])
def test_hard_error_json(board, stream_stub, monkeypatch, response, code) -> None:
    monkeypatch.chdir(board.b)
    stream_stub.sync_override = response
    for command in _commands(board):
        result = _run(board, *command, "--json")
        assert result.exit_code == 1, (command, result.output)
        envelope = json.loads(result.stdout)
        assert envelope["ok"] is False and envelope["error"]["code"] == code, command


PLAIN = [c for c in CASES if c[1] in ("INTEGRITY_ERROR", "PROXY_REJECTED", "UNAUTHENTICATED")]


@pytest.mark.parametrize(("response", "code"), PLAIN, ids=[c for _, c in PLAIN])
def test_hard_error_plain(board, stream_stub, monkeypatch, response, code) -> None:
    monkeypatch.chdir(board.b)
    stream_stub.sync_override = response
    for command in _commands(board):
        result = _run(board, *command)
        assert result.exit_code == 1, (command, result.output)
        last = result.stderr.splitlines()[-1]
        expected = _message(response)
        if expected is None:
            assert last.startswith("Error: "), (command, last)
        else:
            assert last == f"Error: {expected}", (command, last)
        assert "Traceback" not in result.output


def _message(response) -> str | None:
    """The server's own message, when the response is an error envelope."""
    try:
        return json.loads(response[2])["error"]["message"]
    except (ValueError, KeyError, TypeError):
        return None


FATAL = [c for c in CASES if c[1] in ("PROXY_REJECTED", "UNAUTHENTICATED")]


@pytest.mark.parametrize(("response", "code"), FATAL, ids=[c for _, c in FATAL])
def test_follow_ends_with_a_fatal_sync_error(
    board, stream_stub, monkeypatch, response, code
) -> None:
    monkeypatch.chdir(board.b)
    stream_stub.sync_override = response
    result = _run(board, "sync", "--follow")
    assert result.exit_code == 1
    assert result.stderr.splitlines()[-1].startswith("Error: ")


@pytest.mark.parametrize(
    ("status", "server_code", "cli_code"),
    [
        (503, "BOARD_UNAVAILABLE", "SERVER_UNREACHABLE"),
        (503, "BOARD_BUSY", "BOARD_BUSY"),
        (429, "RATE_LIMITED", "BOARD_BUSY"),
    ],
)
def test_only_availability_codes_are_outcomes(
    board, stream_stub, monkeypatch, status, server_code, cli_code
) -> None:
    """H-10b's AVAILABILITY_CODES become outcomes; `lattice sync` reports the
    outcome's code (SPEC 9.5), not the server's error."""
    monkeypatch.chdir(board.b)
    stream_stub.sync_override = _envelope_error(status, server_code)
    result = _run(board, "sync", "--json")
    assert result.exit_code == 1
    assert json.loads(result.stdout)["error"]["code"] == cli_code


def test_follow_treats_a_500_integrity_error_as_a_failed_sync_not_fatal(
    tmp_path, stream_stub
) -> None:
    """A non-fatal hard error: freshness cleared, the follower keeps going."""
    from lattice.remote.follower import live_follower
    from tests.test_remote.follower_support import following
    from tests.test_remote.stream_stub import StubSyncer, wait_for

    syncer = StubSyncer(stream_stub.url)
    with following(tmp_path, stream_stub.url, syncer) as follower:
        assert wait_for(lambda: live_follower(tmp_path), 2)
        stream_stub.sync_override = INTEGRITY_500
        stream_stub.write({"events/T1.jsonl": b"x\n"})
        assert wait_for(lambda: (follower.last_sync_error or "").startswith("INTEGRITY_ERROR"), 2)
        assert not live_follower(tmp_path)
        stream_stub.sync_override = None
        assert wait_for(lambda: live_follower(tmp_path), 3)
