"""SPEC §8.5: admission waits on the event loop with a timeout; BOARD_BUSY on expiry."""

from __future__ import annotations

import threading
import time
from pathlib import Path

from lattice.server.testing import running_server, wait_for
from tests.test_server.conftest import mint


def test_busy_project_answers_board_busy(root: Path) -> None:
    token = mint(root)
    with running_server(root, config={"limits": {"lock_timeout_seconds": 1}}) as server:
        thread = threading.Thread(
            target=server.op, args=("alpha", "xtest.sleep", {"ms": 1800}), kwargs={"token": token}
        )
        thread.start()
        assert wait_for(lambda: server.project("alpha").work.locked())
        started = time.monotonic()
        status, headers, body = server.op("alpha", "task.create", {"title": "x"}, token=token)
        assert status == 503 and body["error"]["code"] == "BOARD_BUSY"
        assert headers["retry-after"] == "2"
        assert 0.9 < time.monotonic() - started < 1.7
        thread.join()
