"""``lattice server serve`` as a real process: JSON-line logs on stdout, health without a
credential, and a graceful SIGTERM that releases the root and exits 0 (SPEC §8.11)."""

from __future__ import annotations

import json
import os
import signal
import socket
import subprocess
import sys
from pathlib import Path

from lattice.server import control
from lattice.server.testing import http_request, wait_for


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def test_serve_process_lifecycle(root: Path) -> None:
    port = _free_port()
    env = {k: v for k, v in os.environ.items() if not k.startswith("LATTICE_")}
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
    try:

        def healthy() -> bool:
            try:
                return http_request("GET", f"http://127.0.0.1:{port}/healthz", timeout=1)[0] == 200
            except OSError:
                return False

        assert wait_for(healthy, timeout=10), proc.stderr.read1().decode() if proc.poll() else ""
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
