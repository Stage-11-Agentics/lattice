"""The public reporter form's browser-side contract, exercised by MiniDOM."""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).parents[2]
NODE_TEST = REPO_ROOT / "tests" / "js" / "reporter-form.test.js"


@pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")
def test_reporter_form_node_acceptance_contract() -> None:
    result = subprocess.run(
        ["node", "--test", str(NODE_TEST)],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=15,
        check=False,
    )
    assert result.returncode == 0, (
        "node reporter form acceptance test failed:\n"
        f"--- stdout ---\n{result.stdout}\n--- stderr ---\n{result.stderr}"
    )
