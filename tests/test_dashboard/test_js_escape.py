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
    "statusSpanHtml",
    "boardCardOpenTag",
    "statusOptionHtml",
    "statusSelectOptionsHtml",
    "boardColumnHeaderHtml",
    "laneSortSelectOpenTag",
    "laneColorRowHtml",
    "statsBarRowHtml",
    "wipAlertHtml",
    "webStatusRowHtml",
    "statusTransitionHtml",
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
    reads = "fetch(apiUrl(BASE_PATH, path), opts)"  # api()
    writes = (  # apiPost()'s writer (static/live.js), which fetches only url(path)
        "fetch: function(url, opts) { return fetch(url, opts); },\n"
        "  url: function(path) { return apiUrl(BASE_PATH, path); },"
    )
    assert reads in html and writes in html
    assert re.search(r"fetch\(", html.replace(reads, "").replace(writes, "")) is None, (
        "every fetch goes through api()/apiPost(), which resolve against the base path"
    )


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


# Where a workflow status, its display name, or its description reaches markup,
# and the builder (node-tested with the hostile corpus) that must render it.
STATUS_SITES = [
    ("index.html", 'statusSpanHtml("badge", getStatusDisplayName(task.status), {background:', 2),
    ("index.html", "statusSelectOptionsHtml(config && config.workflow, task.status, allowed)", 2),
    (
        "index.html",
        'statusSpanHtml("badge badge-stat", getStatusDisplayName(task.status), '
        "{title: getStatusDescription(task.status)})",
        3,
    ),
    ("index.html", 'statusSpanHtml("tpi-status", getStatusDisplayName(status))', 1),
    ("index.html", 'statusSpanHtml("list-status", t.status || "")', 1),
    ("index.html", "statusSpanHtml(statusClass, c.status", 1),
    (
        "index.html",
        "boardColumnHeaderHtml(laneColor, getStatusDescription(status), "
        "getStatusDisplayName(status), items.length)",
        1,
    ),
    ("index.html", "boardColumnOpenTag(status, items.length === 0)", 1),
    ("index.html", "boardCardOpenTag(t, ", 1),
    ("index.html", "laneSortSelectOpenTag(status)", 1),
    ("index.html", "laneColorRowHtml(s, color)", 1),
    ("index.html", "statusOptionHtml(s, s, false)", 1),
    ("index.html", "statsBarRowHtml(row[0], pct, color, row[1])", 1),
    ("index.html", "statsBarRowHtml(r.status, pct, color, label,", 1),
    ("index.html", 'wipAlertHtml(t("stats.wip_exceeded"), w.status, w.current, w.limit)', 1),
    ("index.html", "webStatusRowHtml(getLaneColor(node.status), node.status)", 1),
    ("index.html", "statusTransitionHtml(config && config.workflow, d.from, d.to)", 1),
    ("cube3d.js", "statusSpanHtml('cube3d-card-status', getStatusDisplayName(", 1),
    ("cube3d.js", "statusSpanHtml('cube3d-workspace-status', getStatusDisplayName(", 1),
    ("cube3d.js", "legendItemHtml('cube3d-legend-item', 'cube3d-legend-dot'", 1),
    ("cube-v2.js", "statusSpanHtml('cv2-tooltip-status', statusName, {color: statusColor})", 1),
    ("cube-v2.js", "legendItemHtml('cv2-legend-item', 'cv2-legend-dot'", 1),
]

STATUS_BUILDERS = re.compile(
    r"statusSpanHtml|statusOptionHtml|statusSelectOptionsHtml|boardColumnHeaderHtml|"
    r"boardColumnOpenTag|boardCardOpenTag|laneSortSelectOpenTag|laneColorRowHtml|"
    r"statsBarRowHtml|wipAlertHtml|webStatusRowHtml|statusTransitionHtml|legendItemHtml"
)
STATUS_VALUE = re.compile(
    r"getStatusDisplayName\(|getStatusDescription\(|_cv2GetStatusDisplayName\(|"
    r"\bstatusName\b|esc\((?:s|status|task\.status|node\.status|t\.status[^)]*|r\.status|"
    r"w\.status)\)|_structureEscape\(c\.status"
)
MARKUP_LITERAL = re.compile(r"""['"][^'"]*<[a-zA-Z/]""")


def status_markup_outside_builders(text: str) -> list[str]:
    """Lines that put a status value into a markup literal without a builder."""
    return [
        line.strip()
        for line in text.splitlines()
        if MARKUP_LITERAL.search(line)
        and STATUS_VALUE.search(line)
        and not STATUS_BUILDERS.search(line)
    ]


@pytest.mark.parametrize(("name", "snippet", "count"), STATUS_SITES)
def test_each_status_sink_uses_its_builder(name: str, snippet: str, count: int) -> None:
    assert (STATIC / name).read_text().count(snippet) == count, f"{name}: {snippet}"


@pytest.mark.parametrize("name", ["index.html", "cube3d.js", "cube-v2.js"])
def test_no_status_markup_outside_the_builders(name: str) -> None:
    assert status_markup_outside_builders((STATIC / name).read_text()) == []


def test_the_status_scan_catches_the_pre_repair_shapes() -> None:
    old = [
        """'<span class="cv2-tooltip-status" style="color:' + c + '">' + _cv2Esc(statusName)""",
        """html += '<option value="' + esc(s) + '">' + esc(getStatusDisplayName(s)) + '</option>';""",
        """html += '<td><span class="badge badge-stat" title="' + esc(getStatusDescription(x)) + '">';""",
        """html += '<span class="stats-bar-label">' + esc(r.status) + '</span>';""",
    ]
    assert len(status_markup_outside_builders("\n".join(old))) == len(old)
