"""Task type validation gives mode-specific, actionable guidance."""

from __future__ import annotations

from pathlib import Path

import pytest

from lattice.boards import resolve_board
from lattice.core.errors import OpError
from lattice.ops import Caller
from lattice.storage.board_init import create_board
from lattice.server.testing import ServerHandle


def test_unconfigured_type_error_message_uses_server_project_command(
    tmp_path: Path, server: ServerHandle, token: str
) -> None:
    local_root = tmp_path / "local"
    create_board(local_root, actor="human:test")
    local = resolve_board(local_root)

    with pytest.raises(OpError) as local_error:
        local.execute("task.create", {"title": "Local", "type": "research"}, Caller("human:test"))

    status, _, body = server.op(
        "alpha", "task.create", {"title": "Hosted", "type": "research"}, token=token
    )
    assert status == 400
    assert body["error"]["code"] == "VALIDATION_ERROR"
    assert body["error"]["message"].startswith("Invalid task type: 'research'.")
    assert body["error"]["message"] != local_error.value.message
    assert "lattice server project config alpha --set" in body["error"]["message"]
    assert 'task_types=["task","bug","chore","research"]' in body["error"]["message"]
    assert "server host" in body["error"]["message"]
    assert ".lattice/config.json" not in body["error"]["message"]

    created_status, _, created = server.op(
        "alpha", "task.create", {"title": "Existing"}, token=token
    )
    assert created_status == 200
    update_status, _, update = server.op(
        "alpha",
        "task.update",
        {"task": created["data"]["result"]["task"]["id"], "pairs": ["type=research"]},
        token=token,
    )
    assert update_status == 400
    assert update["error"]["code"] == "VALIDATION_ERROR"
    assert update["error"]["message"] == body["error"]["message"]
