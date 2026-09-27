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
