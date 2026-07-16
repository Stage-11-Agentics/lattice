"""Bridge the dashboard's JS lane-logic tests into pytest, plus a shadowing guard.

The pure lane sort/group logic lives in a static JS file
(``src/lattice/dashboard/static/lane-logic.js``) and is tested with node's built-in
test runner (``tests/js/lane-logic.test.js``, zero npm deps). This module makes
``uv run pytest`` the single test entrypoint by shelling out to node, and adds a guard
that fails if the extraction ever regresses — a stale inline copy in ``index.html``
would silently shadow the tested file and make the node tests exercise dead code.
"""

from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
NODE_TEST_FILE = REPO_ROOT / "tests" / "js" / "lane-logic.test.js"
INDEX_HTML = REPO_ROOT / "src" / "lattice" / "dashboard" / "static" / "index.html"
LANE_LOGIC_JS = REPO_ROOT / "src" / "lattice" / "dashboard" / "static" / "lane-logic.js"

# Full §1 inventory of identifiers moved out of the IIFE into lane-logic.js. If any of
# these is defined inline again, the browser's global from lane-logic.js is shadowed and
# the node tests test dead code.
MOVED_IDENTIFIERS = [
    "DEFAULT_LANE_SORT",
    "LANE_SORT_MODES",
    "DONE_LANE_MODES",
    "PRIORITY_RANK",
    "URGENCY_RANK",
    "COMPLEXITY_RANK",
    "DONE_DISPLAY_TO_MODE",
    "MODE_TO_DONE_DISPLAY",
    "LANE_COMPARATORS",
    "laneModesFor",
    "isValidLaneMode",
    "resolveLaneSort",
    "_byRank",
    "_byTime",
    "sortLaneItems",
    "groupLaneItems",
]


@pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")
def test_js_lane_logic_node_tests_pass() -> None:
    """Run the node:test suite for lane-logic.js and require it green.

    Explicit file path (not ``node --test <dir>``) to sidestep version-sensitive
    directory discovery. stdout+stderr are surfaced on failure so node assertion
    output is readable straight from pytest.
    """
    result = subprocess.run(
        ["node", "--test", str(NODE_TEST_FILE)],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert result.returncode == 0, (
        "node lane-logic tests failed:\n"
        f"--- stdout ---\n{result.stdout}\n--- stderr ---\n{result.stderr}"
    )


def test_lane_logic_script_tag_present_and_no_inline_shadowing() -> None:
    """Guard the extraction: script tag wired, and no moved identifier defined inline.

    Fails if ``index.html`` is missing the ``lane-logic.js`` script tag, or if any moved
    identifier reappears as an inline definition (``function name(`` / ``var name =``) —
    a partial move that would shadow the tested file while the node tests stay green.
    """
    html = INDEX_HTML.read_text()

    assert '<script src="/static/lane-logic.js">' in html, (
        'index.html is missing the <script src="/static/lane-logic.js"> tag'
    )
    # Must load without defer (before the inline IIFE runs).
    assert '<script src="/static/lane-logic.js" defer>' not in html, (
        "lane-logic.js must NOT be loaded with defer — it must run before the inline IIFE"
    )

    for name in MOVED_IDENTIFIERS:
        pattern = re.compile(rf"function\s+{re.escape(name)}\s*\(|var\s+{re.escape(name)}\s*=")
        assert not pattern.search(html), (
            f"'{name}' is still defined inline in index.html — it must live only in "
            f"lane-logic.js, or the browser global is shadowed and the node tests test "
            f"dead code."
        )


def test_lane_logic_js_defines_moved_identifiers() -> None:
    """Sanity: the moved identifiers actually live in lane-logic.js now."""
    js = LANE_LOGIC_JS.read_text()
    for name in MOVED_IDENTIFIERS:
        pattern = re.compile(rf"function\s+{re.escape(name)}\s*\(|var\s+{re.escape(name)}\s*=")
        assert pattern.search(js), f"'{name}' is not defined in lane-logic.js"
