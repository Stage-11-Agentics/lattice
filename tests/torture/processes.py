"""A real ``lattice server serve`` process for the torture tests (through
``serve_with_test_ops.py``, so the ``xtest.*`` operations are registered)."""

from __future__ import annotations

import os
import socket
import subprocess
import sys
from pathlib import Path

from lattice.server.testing import http_request, wait_for

LAUNCHER = Path(__file__).with_name("serve_with_test_ops.py")


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def start_server(
    root: Path, port: int | None = None, *, env: dict[str, str] | None = None
) -> tuple[subprocess.Popen, int]:
    """Start ``serve`` and wait until ``/healthz`` answers. With no *port*, pick a
    free one (retrying if another process takes it first); with a *port*, use
    exactly that one."""
    base = {k: v for k, v in os.environ.items() if not k.startswith("LATTICE_")}
    base.update(env or {})
    for _attempt in range(5):
        chosen = port or free_port()
        proc = subprocess.Popen(
            [sys.executable, str(LAUNCHER), "server", "serve", "--root", str(root)]
            + ["--port", str(chosen)],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=base,
        )

        def healthy(p: int = chosen) -> bool:
            if proc.poll() is not None:
                return True
            try:
                return http_request("GET", f"http://127.0.0.1:{p}/healthz", timeout=1)[0] == 200
            except OSError:
                return False

        wait_for(healthy, timeout=15)
        if proc.poll() is None:
            return proc, chosen
        _out, err = proc.communicate(timeout=5)
        if port is not None:
            raise AssertionError(f"serve did not start on port {port}: {err.decode()[-500:]}")
    raise AssertionError("serve did not start on any of five ports")


def stop(proc: subprocess.Popen) -> None:
    if proc.poll() is None:
        proc.kill()
    proc.communicate(timeout=10)
