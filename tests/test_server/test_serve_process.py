"""``lattice server serve`` as a real process: JSON-line logs on stdout, health without a
credential, and a graceful SIGTERM that releases the root and exits 0 (SPEC §8.11).

A subprocess server is outside the default suite's hermetic rule (EVALUATION §1), so
this runs under the ``torture`` marker. H-22's ``tests/torture/test_process_lifecycle.py``
owns AC-31's full process-lifecycle proof.
"""

from __future__ import annotations

import json
import os
import signal
import socket
import subprocess
import sys
from pathlib import Path

import pytest

from lattice.server import control
from lattice.server.testing import http_request, wait_for

pytestmark = pytest.mark.torture


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _start(root: Path) -> tuple[subprocess.Popen, int]:
    """Start ``serve`` on a free port; retry if another process took the port first."""
    env = {k: v for k, v in os.environ.items() if not k.startswith("LATTICE_")}
    for _attempt in range(5):
        port = _free_port()
        proc = subprocess.Popen(
            [
                sys.executable,
                "-c",
                "from lattice.cli.main import cli; cli()",
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
                return True  # exited: stop waiting
            try:
                return http_request("GET", f"http://127.0.0.1:{port}/healthz", timeout=1)[0] == 200
            except OSError:
                return False

        wait_for(healthy, timeout=10)
        if proc.poll() is None:
            return proc, port
        proc.communicate(timeout=5)
    raise AssertionError("serve did not start on any of five ports")


def test_serve_process_lifecycle(root: Path) -> None:
    proc, _port = _start(root)
    try:
        assert control.server_running(root)
        proc.send_signal(signal.SIGTERM)
        out, err = proc.communicate(timeout=10)
    finally:
        if proc.poll() is None:
            proc.kill()
    assert proc.returncode == 0, err.decode()
    events = [json.loads(line)["event"] for line in out.decode().splitlines()]
    assert events[0] == "startup" and events[-1] == "shutdown"
    assert "project_load" in events
    assert not control.server_running(root)


def test_sigterm_ends_open_streams_at_once(root: Path) -> None:
    """SPEC §8.9 framing and lifecycle: on SIGTERM every open stream ends at once, so
    a follower never holds up the graceful shutdown (H-22 proves the full AC-31 row)."""
    import time

    from lattice.server import tokens
    from lattice.server.testing import open_stream

    token = tokens.create_token(root, user="human:alice", machine="m", all_projects=True)["token"]
    proc, port = _start(root)
    try:
        reader = open_stream(f"http://127.0.0.1:{port}", "alpha", token)
        assert reader.status == 200 and reader.next().event == "heartbeat"
        started = time.monotonic()
        proc.send_signal(signal.SIGTERM)
        while reader.next(timeout=5) is not None:
            pass  # heartbeats until the server ends the stream
        ended = time.monotonic() - started
        proc.communicate(timeout=10)
        exited = time.monotonic() - started
    finally:
        if proc.poll() is None:
            proc.kill()
    assert proc.returncode == 0
    assert ended < 2.0 and exited < 5.0


def _stalled(port: int, path: str, token: str) -> socket.socket:
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 4096)
    sock.connect(("127.0.0.1", port))
    sock.sendall(
        f"GET {path} HTTP/1.1\r\nHost: x\r\nAuthorization: Bearer {token}\r\n\r\n".encode()
    )
    head = b""
    while b"\r\n\r\n" not in head:
        head += sock.recv(1)
    assert head.startswith(b"HTTP/1.1 200"), head
    return sock


@pytest.mark.parametrize("blocked_in", ["live", "replay"])
def test_sigterm_ends_a_stream_blocked_on_a_client_that_never_reads(
    root: Path, blocked_in: str
) -> None:
    """Review round 1, finding 2: a stream whose client stopped reading, blocked in a
    live send or in its initial replay, does not hold up SIGTERM."""
    import time

    from lattice.server import tokens

    token = tokens.create_token(root, user="human:alice", machine="m", all_projects=True)["token"]
    proc, port = _start(root)
    url = f"http://127.0.0.1:{port}"
    payload = json.dumps({"blob": "x" * 60_000})

    def write(op: str, params: dict) -> dict:
        status, _, body = http_request(
            "POST",
            f"{url}/v1/projects/alpha/ops/{op}",
            token=token,
            body={"params": params, "actor": "human:alice"},
        )
        assert status == 200, body
        return body["data"]

    stalled = None
    try:
        task = write("task.create", {"title": "t"})["result"]["task"]["id"]
        if blocked_in == "live":
            stalled = _stalled(port, "/v1/projects/alpha/stream", token)
        for _ in range(80):  # about 5 MB of entries
            write("task.event", {"task": task, "event_type": "x_blob", "data": payload})
        if blocked_in == "replay":
            _, _, sync = http_request("GET", f"{url}/v1/projects/alpha/sync", token=token)
            epoch = sync["data"]["epoch"]
            stalled = _stalled(port, f"/v1/projects/alpha/stream?since=0&epoch={epoch}", token)
        time.sleep(0.3)
        started = time.monotonic()
        proc.send_signal(signal.SIGTERM)
        proc.communicate(timeout=15)
        elapsed = time.monotonic() - started
    finally:
        if proc.poll() is None:
            proc.kill()
        if stalled is not None:
            stalled.close()
    assert proc.returncode == 0
    assert elapsed < 5.0, elapsed
