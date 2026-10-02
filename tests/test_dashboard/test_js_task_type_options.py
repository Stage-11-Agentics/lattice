"""Bridge pure task-type select logic into pytest and guard its live wiring."""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
NODE_TEST_FILE = REPO_ROOT / "tests" / "js" / "task-type-options.test.js"
INDEX_HTML = REPO_ROOT / "src" / "lattice" / "dashboard" / "static" / "index.html"


@pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")
def test_task_type_option_node_tests_pass() -> None:
    result = subprocess.run(
        ["node", "--test", str(NODE_TEST_FILE)],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )
    assert result.returncode == 0, (
        "node task-type options tests failed:\n"
        f"--- stdout ---\n{result.stdout}\n--- stderr ---\n{result.stderr}"
    )


def test_task_type_options_helper_is_loaded_and_used_by_all_selects() -> None:
    html = INDEX_HTML.read_text(encoding="utf-8")
    script = '<script src="static/task-type-options.js"></script>'
    assert script in html
    assert html.index(script) < html.index("<script>\n")
    assert html.count("buildTaskTypeOptions(types, task.type, true)") == 2
    assert 'buildTaskTypeOptions(types, "task", false)' in html
