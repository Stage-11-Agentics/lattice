"""Launcher for the process-lifecycle tests: ``lattice`` with the test-only
``xtest.*`` operations registered in the server process.

Run as ``python tests/torture/serve_with_test_ops.py server serve ...``. The
product has no hook for this: the launcher imports the test operations
itself, then runs the ordinary CLI.

``LATTICE_TEST_PAUSE_AFTER_COMMIT=<path>``: while the file *path* exists,
every transaction, right after its journal line is fsynced (the commit
point, the ``finish.accept`` seam) and before anything else, creates
``<path>.committed`` and sleeps, so a test that waits for that marker kills
the process at exactly that instant (AC-46's kill-and-restart case).
"""

from __future__ import annotations

import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import tests.test_server.server_ops  # noqa: E402, F401 - registers xtest.sleep
from lattice.cli.main import cli  # noqa: E402

PAUSE = os.environ.get("LATTICE_TEST_PAUSE_AFTER_COMMIT")

if PAUSE:
    from lattice.server import transactions

    def _pause_after_commit(point: str, **_ctx: object) -> None:
        if point == "finish.accept" and os.path.exists(PAUSE):
            # Reached only after the journal write and its fsync succeeded.
            with open(PAUSE + ".committed", "w") as marker:
                marker.write("committed\n")
            deadline = time.monotonic() + 30
            while os.path.exists(PAUSE) and time.monotonic() < deadline:
                time.sleep(0.02)

    transactions._fault = _pause_after_commit

if __name__ == "__main__":
    cli()
