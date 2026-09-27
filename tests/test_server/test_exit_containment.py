"""G-6: a SystemExit (or any BaseException) inside an operation answers 500, is logged,
and the server keeps serving."""

from __future__ import annotations

from pathlib import Path

import pytest

from lattice.server.testing import ServerHandle
from tests.test_server.conftest import create_task, mint


@pytest.mark.parametrize(
    ("kind", "exception"),
    [("exit", "SystemExit"), ("interrupt", "KeyboardInterrupt"), ("error", "RuntimeError")],
)
def test_a_crashing_operation_answers_500_and_the_server_keeps_serving(
    server: ServerHandle, root: Path, kind: str, exception: str
) -> None:
    token = mint(root)
    status, _, body = server.op("alpha", "xtest.raise", {"kind": kind}, token=token)
    assert status == 500
    assert body == {
        "ok": False,
        "error": {"code": "INTERNAL_ERROR", "message": "internal server error"},
    }
    crashes = [line for line in server.log_lines if line["event"] == "op_crashed"]
    assert crashes and crashes[-1]["exception"] == exception
    assert crashes[-1]["op"] == "xtest.raise" and crashes[-1]["project"] == "alpha"
    # the same project, and another, keep working
    create_task(server, token, "alpha")
    create_task(server, token, "beta")
    assert server.request("GET", "/healthz")[0] == 200
