"""AC-31: ``lattice server serve`` as a real process (SPEC §8.11).

Health answers without a credential, stdout carries only JSON lines, and a
SIGTERM that arrives while an operation runs lets that operation complete,
then shuts down in its phases (drain, final audit, ``clean_shutdown``, lease
release) and exits 0 within 5 s of the operation completing. The slow
operation is ``xtest.sleep``, registered in the server process by
``serve_with_test_ops.py``.
"""

from __future__ import annotations

import json
import os
import signal
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

from lattice.server import control, tokens
from lattice.server.testing import http_request, make_root, wait_for

pytestmark = pytest.mark.torture

LAUNCHER = Path(__file__).with_name("serve_with_test_ops.py")


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _start(root: Path) -> tuple[subprocess.Popen, int]:
    env = {k: v for k, v in os.environ.items() if not k.startswith("LATTICE_")}
    for _attempt in range(5):
        port = _free_port()
        proc = subprocess.Popen(
            [
                sys.executable,
                str(LAUNCHER),
                "server",
                "serve",
                "--root",
                str(root),
                "--port",
                str(port),
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=env,
        )

        def healthy(port: int = port) -> bool:
            if proc.poll() is not None:
                return True
            try:
                return http_request("GET", f"http://127.0.0.1:{port}/healthz", timeout=1)[0] == 200
            except OSError:
                return False

        wait_for(healthy, timeout=15)
        if proc.poll() is None:
            return proc, port
        proc.communicate(timeout=5)
    raise AssertionError("serve did not start on any of five ports")


@pytest.mark.timeout(60)
def test_sigterm_during_a_slow_op_completes_it_then_exits_0(tmp_path: Path) -> None:
    root = make_root(tmp_path, projects={"alpha": {"code": "ALP"}, "beta": {"code": "BET"}})
    token = tokens.create_token(root, user="human:alice", machine="m", all_projects=True)["token"]
    proc, port = _start(root)
    url = f"http://127.0.0.1:{port}"
    outcome: dict = {}
    try:
        status, _, health = http_request("GET", url + "/healthz", timeout=5)
        assert status == 200 and health["ok"] is True  # no credential needed
        assert http_request("GET", url + "/v1/info", timeout=5)[0] == 401

        def slow_op() -> None:
            outcome["response"] = http_request(
                "POST",
                url + "/v1/projects/alpha/ops/xtest.sleep",
                token=token,
                body={"params": {"ms": 1500}},
                timeout=30,
            )
            outcome["done_at"] = time.monotonic()

        writer = threading.Thread(target=slow_op)
        writer.start()
        board = root / "projects" / "alpha" / ".lattice"
        # The op is in flight once its undo log exists.
        assert wait_for(lambda: any((board / "hosted" / "undo").glob("*.jsonl")), timeout=10)
        proc.send_signal(signal.SIGTERM)
        writer.join(timeout=30)
        out, err = proc.communicate(timeout=30)
        exited_at = time.monotonic()
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.communicate(timeout=5)

    status, _, body = outcome["response"]
    assert status == 200, body  # the in-flight op completed
    assert proc.returncode == 0, err.decode()
    assert exited_at - outcome["done_at"] < 5.0

    lines = [json.loads(line) for line in out.decode().splitlines()]  # JSON lines only
    events = [line["event"] for line in lines]
    assert events[0] == "startup" and events[-1] == "shutdown"
    assert "clean_shutdown" in events
    journal = board / "hosted" / "journal.jsonl"
    (line,) = [json.loads(x) for x in journal.read_text().splitlines()]
    assert line["op"] == "xtest.sleep" and line["seq"] == body["data"]["seq"]
    meta = json.loads((board / "hosted" / "journal_meta.json").read_text())
    assert meta["clean_shutdown"]["head_seq"] == 1
    assert not control.server_running(root)
    assert not list((board / "hosted" / "undo").glob("*.jsonl"))
