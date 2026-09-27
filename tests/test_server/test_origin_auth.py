"""AC-37: the server stamps origin.authenticated from the token, discarding a forged one.
AC-36 (hosted): origin.reported values over their caps or holding control characters
are refused before the operation runs."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from lattice.server.testing import ServerHandle
from tests.test_server.conftest import board_hash, mint


def _events(root: Path, task_id: str) -> list[dict]:
    log = root / "projects" / "alpha" / ".lattice" / "events" / f"{task_id}.jsonl"
    return [json.loads(line) for line in log.read_text().splitlines()]


def test_forged_authenticated_is_replaced(server: ServerHandle, root: Path) -> None:
    token = mint(root, user="human:alice", machine="alice-laptop")
    reported = {
        "host": "h",
        "os_user": "alice",
        "worktree": "/src/wt",
        "branch": "feat/x",
        "client_version": "2.0.0",
    }
    forged = {"token_id": "tok_forged", "user": "human:mallory", "machine": "evil"}
    status, _, body = server.op(
        "alpha",
        "task.create",
        {"title": "x"},
        token=token,
        op_id="op_01J9Z0000000000000000000AB",
        origin={"reported": reported, "authenticated": forged},
    )
    assert status == 200, body
    task_id = body["data"]["result"]["task"]["id"]
    origin = _events(root, task_id)[0]["origin"]
    assert origin["op"] == "task.create"
    assert origin["op_id"] == "op_01J9Z0000000000000000000AB"
    assert origin["reported"] == reported
    assert origin["authenticated"]["user"] == "human:alice"
    assert origin["authenticated"]["machine"] == "alice-laptop"
    assert origin["authenticated"]["token_id"].startswith("tok_")
    assert origin["authenticated"]["token_id"] != "tok_forged"


def test_a_minted_op_id_when_none_is_sent(server: ServerHandle, root: Path) -> None:
    token = mint(root)
    _, _, body = server.op("alpha", "task.create", {"title": "x"}, token=token)
    assert body["data"]["op_id"].startswith("op_")
    task_id = body["data"]["result"]["task"]["id"]
    assert _events(root, task_id)[0]["origin"]["op_id"] == body["data"]["op_id"]


@pytest.mark.parametrize(
    "reported",
    [
        {"host": "x" * 257},
        {"worktree": "/" + "w" * 1024},
        {"branch": "feat\x1b[31m"},
        {"os_user": "a\nb"},
        {"host": "a\x85b"},
        {"host": 5},
        {"shell": "zsh"},
        {"source": "cli"},
        "not-an-object",
    ],
)
def test_bad_reported_values_are_refused(server: ServerHandle, root: Path, reported) -> None:
    token = mint(root)
    before = board_hash(root, "alpha")
    status, _, body = server.op(
        "alpha", "task.create", {"title": "x"}, token=token, origin={"reported": reported}
    )
    assert status == 400 and body["error"]["code"] == "VALIDATION_ERROR", body
    assert board_hash(root, "alpha") == before


def test_caps_are_inclusive(server: ServerHandle, root: Path) -> None:
    token = mint(root)
    reported = {"host": "x" * 256, "worktree": "/" + "w" * 1023, "source": "browser"}
    status, _, _ = server.op(
        "alpha", "task.create", {"title": "x"}, token=token, origin={"reported": reported}
    )
    assert status == 200
