"""Bridge the dashboard's JS escaping tests into pytest, plus page-wiring guards.

``esc``, ``basePath`` and ``apiUrl`` live in ``static/escape.js`` and are
tested with node's built-in runner (``tests/js/escape.test.js``, which also
scans every page script for inline event handlers). This module runs them
under pytest, and guards the wiring the node tests cannot see: ``esc`` is not
redefined inline (a stale copy would shadow the tested one), ``escape.js``
loads before the inline script, and every asset and API call resolves against
the page's base path (SPEC §10). Same pattern as test_js_panel_logic.py.
"""

from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
NODE_TEST_FILE = REPO_ROOT / "tests" / "js" / "escape.test.js"
STATIC = REPO_ROOT / "src" / "lattice" / "dashboard" / "static"
INDEX_HTML = STATIC / "index.html"

ESCAPE_IDENTIFIERS = [
    "esc",
    "classToken",
    "ownValue",
    "statusDisplayName",
    "legendItemHtml",
    "boardColumnOpenTag",
    "basePath",
    "apiUrl",
]


def _definition_pattern(name: str) -> str:
    escaped = re.escape(name)
    return rf"function\s+{escaped}\s*\(|(?:var|let|const)\s+{escaped}\s*="


@pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")
def test_js_escape_node_tests_pass() -> None:
    result = subprocess.run(
        ["node", "--test", str(NODE_TEST_FILE)],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )
    assert result.returncode == 0, (
        "node escape tests failed:\n"
        f"--- stdout ---\n{result.stdout}\n--- stderr ---\n{result.stderr}"
    )


def test_escape_script_loads_before_the_inline_script_and_is_not_shadowed() -> None:
    html = INDEX_HTML.read_text()
    tag = '<script src="static/escape.js"></script>'
    assert tag in html, "index.html must load static/escape.js (without defer)"
    assert html.index(tag) < html.index("<script>\n(function(){"), (
        "escape.js must load before the inline IIFE that calls esc()"
    )
    for name in ESCAPE_IDENTIFIERS:
        assert not re.search(_definition_pattern(name), html), (
            f"'{name}' is defined inline in index.html; it must live only in escape.js"
        )


def test_escape_js_defines_identifiers() -> None:
    js = (STATIC / "escape.js").read_text()
    for name in ESCAPE_IDENTIFIERS:
        assert re.search(_definition_pattern(name), js), f"'{name}' is not defined in escape.js"


def test_assets_and_api_calls_use_the_base_path() -> None:
    html = INDEX_HTML.read_text()
    assert not re.search(r"""(?:src|href)=["']/(?!/)""", html), (
        "asset references must be relative, so the page works at / and at /p/<slug>/"
    )
    assert "fetch(apiUrl(BASE_PATH, path), opts)" in html
    assert (
        re.search(r"fetch\(", html.replace("fetch(apiUrl(BASE_PATH, path), opts)", "")) is None
    ), "every fetch goes through api()/apiPost(), which resolve against the base path"


def test_status_and_legend_markup_goes_through_the_tested_helpers() -> None:
    """Where workflow statuses and display names reach markup (review round 1)."""
    html = INDEX_HTML.read_text()
    assert "html += boardColumnOpenTag(status, items.length === 0);" in html
    assert "return statusDisplayName(config && config.workflow, slug);" in html
    assert not re.search(r'"status-"\s*\+\s*status', html), (
        "board lane class built from a raw status"
    )
    assert not re.search(r"pri-' \+ esc\(", html), "priority class must be a classToken"
    for name in ("cube-v2.js", "cube3d.js"):
        js = (STATIC / name).read_text()
        assert js.count("legendItemHtml(") >= 2, f"{name} legends must use legendItemHtml"
        assert 'legend-dot" style="background:\' +' not in js
