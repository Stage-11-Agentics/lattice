"""CLI start-up stays lean (LAT-359): importing ``lattice.cli.main`` loads no
operation module and no hosted-client code. Each command that needs them
imports them itself, so every local command stops paying for them."""

from __future__ import annotations

import subprocess
import sys


def test_cli_start_up_loads_no_operation_module_or_http_client() -> None:
    code = (
        "import sys, lattice.cli.main\n"
        "ops = sorted(m for m in sys.modules if m.startswith('lattice.ops.')\n"
        "             and m not in ('lattice.ops.base', 'lattice.ops.discovery'))\n"
        "remote = sorted(m for m in sys.modules if m.startswith(('lattice.remote.', 'http.')))\n"
        "print(ops, remote)\n"
        "assert not ops and not remote\n"
    )
    proc = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True)
    assert proc.returncode == 0, proc.stdout + proc.stderr
