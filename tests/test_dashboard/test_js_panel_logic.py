"""Bridge the dashboard's JS panel-logic tests into pytest, plus a shadowing guard.

The pure click-outside-to-dismiss logic lives in a static JS file
(``src/lattice/dashboard/static/panel-logic.js``) and is tested with node's
built-in test runner (``tests/js/panel-logic.test.js``, zero npm deps). This
module makes ``uv run pytest`` the single test entrypoint by shelling out to
node, and adds a guard that fails if any identifier is (re)defined inline in
``index.html`` — a stale inline copy would silently shadow the tested file and
make the node tests exercise dead code. Same pattern as test_js_lane_logic.py.
"""

from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
NODE_TEST_FILE = REPO_ROOT / "tests" / "js" / "panel-logic.test.js"
INDEX_HTML = REPO_ROOT / "src" / "lattice" / "dashboard" / "static" / "index.html"
PANEL_LOGIC_JS = REPO_ROOT / "src" / "lattice" / "dashboard" / "static" / "panel-logic.js"

# Everything panel-logic.js defines as a browser global.
PANEL_IDENTIFIERS = [
    "clickPathMatches",
    "clickPath",
]


def _definition_pattern(name: str) -> str:
    escaped = re.escape(name)
    return rf"function\s+{escaped}\s*\(|(?:var|let|const)\s+{escaped}\s*="


@pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")
def test_js_panel_logic_node_tests_pass() -> None:
    result = subprocess.run(
        ["node", "--test", str(NODE_TEST_FILE)],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )
    assert result.returncode == 0, (
        "node panel-logic tests failed:\n"
        f"--- stdout ---\n{result.stdout}\n--- stderr ---\n{result.stderr}"
    )


def test_panel_logic_script_tag_present_and_no_inline_shadowing() -> None:
    html = INDEX_HTML.read_text()

    assert '<script src="/static/panel-logic.js">' in html, (
        'index.html is missing the <script src="/static/panel-logic.js"> tag'
    )
    assert '<script src="/static/panel-logic.js" defer>' not in html, (
        "panel-logic.js must NOT be loaded with defer — it must run before the inline IIFE"
    )

    for name in PANEL_IDENTIFIERS:
        assert not re.search(_definition_pattern(name), html), (
            f"'{name}' is still defined inline in index.html — it must live only in "
            f"panel-logic.js, or the browser global is shadowed and the node tests test "
            f"dead code."
        )


def test_panel_logic_js_defines_identifiers() -> None:
    js = PANEL_LOGIC_JS.read_text()
    for name in PANEL_IDENTIFIERS:
        assert re.search(_definition_pattern(name), js), (
            f"'{name}' is not defined in panel-logic.js"
        )


def test_click_outside_handler_uses_click_path() -> None:
    """The #48 regression guard: the document click-outside handler must decide
    'inside' from the propagation path, not from ``contains(e.target)``."""
    html = INDEX_HTML.read_text()
    marker = html.index("// --- Click-outside-to-dismiss for sidebars ---")
    start = html.index('document.addEventListener("click", function(e) {', marker)
    end = html.index("\n});\n", start)
    handler = html[start:end]
    assert "clickPath(e)" in handler
    assert "clickPathMatches(" in handler
    assert ".contains(e.target)" not in handler, (
        "click-outside handler must not use contains(e.target): a target detached "
        "mid-propagation (inline edit swap) makes it false and closes the panel (#48)"
    )
