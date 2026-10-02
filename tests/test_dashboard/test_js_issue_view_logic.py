"""Node bridge and feature-off integration guard for issue-view-logic.js."""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
NODE_TEST_FILE = REPO_ROOT / "tests" / "js" / "issue-view-logic.test.js"
INDEX_HTML = REPO_ROOT / "src" / "lattice" / "dashboard" / "static" / "index.html"
ISSUE_VIEW_JS = REPO_ROOT / "src" / "lattice" / "dashboard" / "static" / "issue-view.js"


@pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")
def test_issue_view_logic_node_tests_pass() -> None:
    result = subprocess.run(
        ["node", "--test", str(NODE_TEST_FILE)],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )
    assert result.returncode == 0, (
        "node issue-view-logic tests failed:\n"
        f"--- stdout ---\n{result.stdout}\n--- stderr ---\n{result.stderr}"
    )


def test_issue_assets_are_loaded_only_for_an_enabled_issue_view() -> None:
    html = INDEX_HTML.read_text()
    assert "config.issues.enabled === true" in html
    assert 'await _loadIssueScript("issue-view-logic.js")' in html
    assert 'await _loadIssueScript("issue-view.js")' in html
    assert '<script src="static/issue-view.js">' not in html
    assert '<script src="static/issue-view-logic.js">' not in html
    assert 'if (view === "issues" && !_issuesEnabled()) {' in html


def test_view_has_only_filing_and_top_level_comment_write_calls() -> None:
    source = ISSUE_VIEW_JS.read_text()
    assert (
        'apiPost("/api/issues", { title: title, description: description, media: media })'
        in source
    )
    assert (
        'apiPost("/api/issues/" + encodeURIComponent(issue.id) + "/comment", { body: body })'
        in source
    )
    for control in ("promote", "unlink", "dismiss", "duplicate", "reopen"):
        assert f'data-action="{control}"' not in source
