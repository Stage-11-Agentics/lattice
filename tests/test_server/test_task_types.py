"""Task type validation uses identical wording on local and hosted boards."""

from __future__ import annotations

from pathlib import Path

import pytest

from lattice.boards import resolve_board
from lattice.core.errors import OpError
from lattice.ops import Caller
from lattice.storage.board_init import create_board
from lattice.server.testing import ServerHandle


def test_unconfigured_type_error_message_is_identical_locally_and_hosted(
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
    assert body["error"]["message"] == local_error.value.message
    assert body["error"]["message"].startswith("Invalid task type: 'research'.")
