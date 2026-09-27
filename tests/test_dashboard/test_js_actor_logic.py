"""Bridge the dashboard's JS actor-logic tests into pytest, plus a shadowing guard.

The pure actor identity logic (normalize / classify / display / hue / filter
matching) lives in a static JS file
(``src/lattice/dashboard/static/actor-logic.js``) and is tested with node's
built-in test runner (``tests/js/actor-logic.test.js``, zero npm deps). This
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
NODE_TEST_FILE = REPO_ROOT / "tests" / "js" / "actor-logic.test.js"
INDEX_HTML = REPO_ROOT / "src" / "lattice" / "dashboard" / "static" / "index.html"
ACTOR_LOGIC_JS = REPO_ROOT / "src" / "lattice" / "dashboard" / "static" / "actor-logic.js"

# Everything actor-logic.js defines as a browser global. If any of these is
# defined inline in index.html, the global from actor-logic.js is shadowed and
# the node tests test dead code.
ACTOR_IDENTIFIERS = [
    "ACTOR_UNASSIGNED",
    "normalizeActor",
    "actorKind",
    "actorDisplayName",
    "actorTooltip",
    "actorHue",
    "actorMatchesFilter",
    "actorPairLayout",
]


def _definition_pattern(name: str) -> str:
    """Match any JS definition form — a `let`/`const` reintroduction inside the IIFE
    would shadow the global just like `var` or a function declaration."""
    escaped = re.escape(name)
    return rf"function\s+{escaped}\s*\(|(?:var|let|const)\s+{escaped}\s*="


@pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")
def test_js_actor_logic_node_tests_pass() -> None:
    """Run the node:test suite for actor-logic.js and require it green."""
    result = subprocess.run(
        ["node", "--test", str(NODE_TEST_FILE)],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )
    assert result.returncode == 0, (
        "node actor-logic tests failed:\n"
        f"--- stdout ---\n{result.stdout}\n--- stderr ---\n{result.stderr}"
    )


def test_actor_logic_script_tag_present_and_no_inline_shadowing() -> None:
    """Guard the extraction: script tag wired, and no identifier defined inline."""
    html = INDEX_HTML.read_text()

    assert '<script src="static/actor-logic.js">' in html, (
        'index.html is missing the <script src="static/actor-logic.js"> tag'
    )
    # Must load without defer (before the inline IIFE runs).
    assert '<script src="static/actor-logic.js" defer>' not in html, (
        "actor-logic.js must NOT be loaded with defer — it must run before the inline IIFE"
    )

    for name in ACTOR_IDENTIFIERS:
        pattern = re.compile(_definition_pattern(name))
        assert not pattern.search(html), (
            f"'{name}' is defined inline in index.html — it must live only in "
            f"actor-logic.js, or the browser global is shadowed and the node tests "
            f"test dead code."
        )


def test_actor_logic_js_defines_identifiers() -> None:
    """Sanity: the identifiers actually live in actor-logic.js."""
    js = ACTOR_LOGIC_JS.read_text()
    for name in ACTOR_IDENTIFIERS:
        pattern = re.compile(_definition_pattern(name))
        assert pattern.search(js), f"'{name}' is not defined in actor-logic.js"
