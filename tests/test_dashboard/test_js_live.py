"""Bridge the hosted page's live-refresh and write tests into pytest, plus
page-wiring guards (SPEC §8.6, §10; AC-24).

The pure logic lives in ``static/live.js`` and is tested with node's runner
(``tests/js/live.test.js``). This module runs those tests under pytest and
guards what node cannot see: live.js loads before the inline script and is not
shadowed there; every write goes through the one retry-safe writer; a hosted
page follows its stream and polls only as a fallback; every refresh (stream,
head watch, poll) goes through one scheduler; the log-out button is hosted-only.
"""

from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
NODE_TEST_FILE = REPO_ROOT / "tests" / "js" / "live.test.js"
INDEX_HTML = REPO_ROOT / "src" / "lattice" / "dashboard" / "static" / "index.html"
LIVE_IDENTIFIERS = [
    "newOpId",
    "hostedSlug",
    "streamUrl",
    "retryDelay",
    "createRefreshScheduler",
    "createWriter",
    "createLiveRefresh",
    "createHeadWatch",
]


def _inline_script() -> str:
    return re.findall(r"<script>(.*?)</script>", INDEX_HTML.read_text(), re.S)[0]


def _function_body(source: str, name: str) -> str:
    start = source.index(f"function {name}(")
    depth, i = 0, source.index("{", start)
    for j in range(i, len(source)):
        depth += {"{": 1, "}": -1}.get(source[j], 0)
        if depth == 0:
            return source[i : j + 1]
    raise AssertionError(f"unbalanced {name}")


@pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")
def test_js_live_node_tests_pass() -> None:
    result = subprocess.run(
        ["node", "--test", str(NODE_TEST_FILE)],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )
    assert result.returncode == 0, (
        f"node live tests failed:\n--- stdout ---\n{result.stdout}\n--- stderr ---\n{result.stderr}"
    )


def test_live_script_loads_before_the_inline_script_and_is_not_shadowed() -> None:
    html = INDEX_HTML.read_text()
    tag = '<script src="static/live.js"></script>'
    assert tag in html
    assert html.index('<script src="static/escape.js"></script>') < html.index(tag)
    assert html.index(tag) < html.index("<script>\n(function(){")
    inline = _inline_script()
    for name in LIVE_IDENTIFIERS:
        assert not re.search(rf"function\s+{name}\s*\(|(?:var|let|const)\s+{name}\s*=", inline), (
            f"'{name}' is defined inline; it must live only in live.js"
        )


def test_every_write_goes_through_one_retry_safe_api_post() -> None:
    inline = _inline_script()
    assert "pageWriter.post(path, data)" in _function_body(inline, "apiPost")
    writer = inline[inline.index("var pageWriter = createWriter({") :]
    writer = writer[: writer.index("});")]
    assert "hosted: !!HOSTED_SLUG" in writer
    assert "newOpId(Date.now(), _randomBytes)" in writer
    # No code path in the page POSTs to the API behind the writer's back.
    assert '"POST"' not in inline, "every POST must go through apiPost (the writer)"


def test_a_hosted_page_follows_its_stream_and_polls_only_as_fallback() -> None:
    inline = _inline_script()
    start = _function_body(inline, "startAutoRefresh")
    assert "HOSTED_SLUG" in start and "createLiveRefresh(" in start
    assert "streamUrl(HOSTED_SLUG)" in start
    assert "schedule: refreshScheduler.trigger" in start
    assert "startPoll: startPolling" in start and "stopPoll: stopPolling" in start
    assert "liveRefresh.close()" in _function_body(inline, "stopAutoRefresh")


def test_every_refresh_goes_through_one_scheduler() -> None:
    """The stream, the head watch, and the poll share one single-flight refresh:
    refreshCurrentView is called only by the scheduler."""
    inline = _inline_script()
    assert "var refreshScheduler = createRefreshScheduler(refreshCurrentView);" in inline
    assert inline.count("refreshCurrentView") == 2  # its definition, and the scheduler
    assert "setInterval(refreshScheduler.trigger, AUTO_REFRESH_MS)" in _function_body(
        inline, "startPolling"
    )


def test_the_log_out_button_is_hosted_only() -> None:
    html = INDEX_HTML.read_text()
    assert '<button type="button" class="btn" id="logout" hidden>Log out</button>' in html
    inline = _inline_script()
    block = inline[inline.index("if (HOSTED_SLUG) {\n  var logoutButton") :]
    assert '"/web/logout.js"' in block[:400]


def test_a_local_page_watches_the_bound_cache_head() -> None:
    inline = _inline_script()
    start = _function_body(inline, "startAutoRefresh")
    local = start[start.index("startPolling();") :]
    assert "createHeadWatch(" in local and 'api("/api/head")' in local
    assert "schedule: refreshScheduler.trigger" in local and "intervalMs: HEAD_WATCH_MS" in local
    assert "var HEAD_WATCH_MS = 1000;" in inline
    assert "headWatch.stop()" in _function_body(inline, "stopAutoRefresh")
