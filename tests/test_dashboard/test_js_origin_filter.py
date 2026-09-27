"""Bridge the dashboard's JS origin-filter tests into pytest, plus a shadowing guard.

Same pattern as test_js_actor_logic.py: node runs
``tests/js/origin-filter.test.js`` against ``static/origin-filter.js``, and a
guard fails if index.html redefines any of its identifiers inline (a stale
inline copy would shadow the tested file).
"""

from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
NODE_TEST_FILE = REPO_ROOT / "tests" / "js" / "origin-filter.test.js"
STATIC = REPO_ROOT / "src" / "lattice" / "dashboard" / "static"

IDENTIFIERS = [
    "ORIGIN_FILTER_KEYS",
    "normalizeWorktree",
    "originFiltersFromSearch",
    "originFilterCount",
    "keepsAbsentSelection",
    "taskListPath",
    "sameOriginFilters",
    "createTaskListGate",
    "resolveSelection",
    "taskMatchesSelections",
]


def _definition_pattern(name: str) -> str:
    escaped = re.escape(name)
    return rf"function\s+{escaped}\s*\(|(?:var|let|const)\s+{escaped}\s*="


@pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")
def test_js_origin_filter_node_tests_pass() -> None:
    result = subprocess.run(
        ["node", "--test", str(NODE_TEST_FILE)],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )
    assert result.returncode == 0, (
        "node origin-filter tests failed:\n"
        f"--- stdout ---\n{result.stdout}\n--- stderr ---\n{result.stderr}"
    )


def test_origin_filter_script_tag_present_and_no_inline_shadowing() -> None:
    html = (STATIC / "index.html").read_text()
    assert '<script src="static/origin-filter.js"></script>' in html
    for name in IDENTIFIERS:
        assert not re.search(_definition_pattern(name), html), (
            f"'{name}' is defined inline in index.html; it must live only in origin-filter.js"
        )


def test_origin_filter_js_defines_identifiers() -> None:
    js = (STATIC / "origin-filter.js").read_text()
    for name in IDENTIFIERS:
        assert re.search(_definition_pattern(name), js), f"'{name}' missing from origin-filter.js"


def test_page_routes_task_lists_and_selections_through_the_tested_logic() -> None:
    """The inline page uses the tested helpers: every task list goes through
    the gate, and the tag/assignee/creator sections resolve their selection
    and match tasks with origin-filter.js."""
    html = (STATIC / "index.html").read_text()
    assert 'api("/api/tasks")' not in html, "a task list fetch bypasses loadTasks()"
    assert "taskListGate.request()" in html and "taskListGate.request(filters)" in html
    assert html.count("taskListGate.accept(req)") == 2
    assert "resolveSelection(Object.keys(tagSet), activeTagFilter" in html
    assert "resolveSelection(keys, active, taskListGate.active())" in html
    assert "return taskMatchesSelections(" in html
