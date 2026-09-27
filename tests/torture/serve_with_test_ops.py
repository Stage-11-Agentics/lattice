"""Launcher for the process-lifecycle tests: ``lattice`` with the test-only
``xtest.*`` operations registered in the server process.

Run as ``python tests/torture/serve_with_test_ops.py server serve ...``. The
product has no hook for this: the launcher imports the test operations
itself, then runs the ordinary CLI.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import tests.test_server.server_ops  # noqa: E402, F401 - registers xtest.sleep
from lattice.cli.main import cli  # noqa: E402

if __name__ == "__main__":
    cli()
