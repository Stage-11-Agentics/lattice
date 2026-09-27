"""AC-31: ``lattice server serve`` as a real process (SPEC §8.11).

Health answers without a credential, stdout carries only JSON lines, and a
SIGTERM that arrives while an operation runs, with a follower's stream open,
ends the stream at once and lets that operation complete,
then shuts down in its phases (drain, final audit, ``clean_shutdown``, lease
release) and exits 0 within 5 s of the operation completing. The slow
operation is ``xtest.sleep``, registered in the server process by
``serve_with_test_ops.py``.
"""

from __future__ import annotations

import json
import signal
import threading
import time
from pathlib import Path

import pytest

from lattice.server import control, tokens
from lattice.server.testing import http_request, make_root, open_stream, wait_for
from tests.torture.processes import start_server, stop

pytestmark = pytest.mark.torture


@pytest.mark.timeout(60)
def test_sigterm_during_a_slow_op_with_a_stream_open_completes_it_then_exits_0(
    tmp_path: Path,
) -> None:
    root = make_root(tmp_path, projects={"alpha": {"code": "ALP"}, "beta": {"code": "BET"}})
    token = tokens.create_token(root, user="human:alice", machine="m", all_projects=True)["token"]
    proc, port = start_server(root)
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

        # A follower's stream is open throughout (SPEC §8.9 "Framing and lifecycle").
        stream = open_stream(url, "alpha", token, timeout=30)
        assert stream.status == 200
        assert stream.next_of("heartbeat", timeout=10).data["head_seq"] == 0

        def follow() -> None:
            try:
                while stream.next(timeout=30) is not None:
                    pass
            except TimeoutError:
                return  # never ended: asserted below
            outcome["stream_ended_at"] = time.monotonic()

        follower = threading.Thread(target=follow)
        follower.start()
        writer = threading.Thread(target=slow_op)
        writer.start()
        board = root / "projects" / "alpha" / ".lattice"
        # The op is in flight once its undo log exists.
        assert wait_for(lambda: any((board / "hosted" / "undo").glob("*.jsonl")), timeout=10)
        proc.send_signal(signal.SIGTERM)
        sigterm_at = time.monotonic()
        writer.join(timeout=30)
        follower.join(timeout=30)
        stream.close()
        out, err = proc.communicate(timeout=30)
        exited_at = time.monotonic()
    finally:
        if proc.poll() is None:
            stop(proc)

    status, _, body = outcome["response"]
    assert status == 200, body  # the in-flight op completed
    assert proc.returncode == 0, err.decode()
    assert exited_at - outcome["done_at"] < 5.0
    # SIGTERM ends every open stream at once, before the op even completes.
    assert "stream_ended_at" in outcome, "the open stream was never ended"
    assert outcome["stream_ended_at"] - sigterm_at < 1.0
    assert outcome["stream_ended_at"] <= outcome["done_at"]

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
