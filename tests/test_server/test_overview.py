"""The projects overview at ``GET /`` (SPEC §10): rows, counts, sort orders,
the 24-hour window, permissions, the per-head memo, and the page's shape."""

from __future__ import annotations

import re
import time
from datetime import datetime, timezone
from pathlib import Path

import pytest

from lattice.server import overview
from lattice.server.testing import ServerHandle
from tests.test_server.conftest import create_task, mint
from tests.test_server.web_client import WebClient

ROW_RE = re.compile(r'<a class="row bk" href="/p/([a-z0-9-]+)/" ([^>]*)>')
ATTR_RE = re.compile(r'data-([a-z0-9-]+)="([^"]*)"')


def _logged_in(server: ServerHandle, token: str) -> WebClient:
    web = WebClient(server)
    assert web.login(token).status == 303
    return web


def rows(page: str) -> list[dict[str, str]]:
    out = []
    for slug, attrs in ROW_RE.findall(page):
        found = dict(ATTR_RE.findall(attrs))
        assert found["slug"] == slug
        out.append(found)
    return out


def stat(page: str, name: str) -> str:
    match = re.search(rf'data-stat="{name}">([^<]*)<', page)
    assert match, name
    return match.group(1)


def _archive(server: ServerHandle, token: str, task_id: str, slug: str = "alpha") -> None:
    status, _, body = server.op(slug, "task.archive", {"task": task_id}, token=token)
    assert status == 200, body


@pytest.fixture()
def web(server: ServerHandle, token: str) -> WebClient:
    return _logged_in(server, token)


class EventClock:
    """Event timestamps a test chooses (they have one-second resolution, so two
    writes in one second would tie): ``clock.set(-90)`` stamps the next writes
    90 seconds before the test began."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self.base = float(int(time.time()))
        self._monkeypatch = monkeypatch

    def set(self, offset: float) -> None:
        from lattice.core import events

        stamp = datetime.fromtimestamp(self.base + offset, timezone.utc)
        text = stamp.strftime("%Y-%m-%dT%H:%M:%SZ")
        self._monkeypatch.setattr(events, "utc_now", lambda: text)


@pytest.fixture()
def clock(monkeypatch: pytest.MonkeyPatch) -> EventClock:
    return EventClock(monkeypatch)


# ---------------------------------------------------------------------------
# Rows and counts
# ---------------------------------------------------------------------------


def test_rows_count_live_tickets_and_link_to_each_dashboard(server, token, web) -> None:
    first = create_task(server, token, "alpha")
    create_task(server, token, "alpha")
    create_task(server, token, "alpha")
    create_task(server, token, "beta")
    _archive(server, token, first["id"])

    page = web.get("/")
    assert page.status == 200
    found = {r["slug"]: r for r in rows(page.text)}
    assert set(found) == {"alpha", "beta"}
    # Tickets and the 24-hour count leave archived tasks out ...
    assert found["alpha"]["tickets"] == "2" and found["alpha"]["updated-24h"] == "2"
    assert found["beta"]["tickets"] == "1" and found["beta"]["updated-24h"] == "1"
    # ... and each row links to its dashboard and shows its project code.
    assert '<a class="row bk" href="/p/alpha/"' in page.text
    assert '<span class="code">ALP</span>' in page.text
    assert stat(page.text, "boards") == "2"
    assert stat(page.text, "tickets") == "3"
    assert stat(page.text, "updated-24h") == "3"


def test_last_edit_counts_archiving(server, token, web, clock) -> None:
    """Archiving is an edit: the board's last edit moves to it although the task
    left the ticket count."""
    clock.set(-30)
    task = create_task(server, token, "alpha")
    clock.set(-20)
    create_task(server, token, "beta")  # beta is now the newest board
    assert [r["slug"] for r in rows(web.get("/").text)] == ["beta", "alpha"]
    clock.set(-10)
    _archive(server, token, task["id"])
    found = rows(web.get("/").text)
    assert [r["slug"] for r in found] == ["alpha", "beta"]
    assert found[0]["tickets"] == "0"


def test_an_empty_board_has_no_last_edit_and_sorts_last(server, token, web) -> None:
    create_task(server, token, "beta")
    found = rows(web.get("/").text)
    assert [r["slug"] for r in found] == ["beta", "alpha"]
    assert found[1]["last-edit"] == "" and found[1]["tickets"] == "0"


# ---------------------------------------------------------------------------
# Sort orders
# ---------------------------------------------------------------------------


def test_sort_by_last_edit_is_the_default_and_name_is_a_link(server, token, web, clock) -> None:
    clock.set(-30)
    create_task(server, token, "beta")
    clock.set(-20)
    create_task(server, token, "alpha")  # alpha edited last

    for path in ("/", "/?sort=edited", "/?sort=bogus"):
        page = web.get(path).text
        assert [r["slug"] for r in rows(page)] == ["alpha", "beta"], path
        assert '<a href="/?sort=edited" aria-current="true">' in page, path
        assert 'data-sort="edited"' in page

    clock.set(-10)
    create_task(server, token, "beta")  # now beta is newest
    assert [r["slug"] for r in rows(web.get("/").text)] == ["beta", "alpha"]

    page = web.get("/?sort=name").text
    assert [r["slug"] for r in rows(page)] == ["alpha", "beta"]
    assert '<a href="/?sort=name" aria-current="true">' in page
    assert '<a class="bk" href="/?sort=edited">' in page


# ---------------------------------------------------------------------------
# The 24-hour window and the 14-day mark (a fixed clock)
# ---------------------------------------------------------------------------


def test_the_24_hour_window_and_the_14_day_mark(server, token, web, monkeypatch) -> None:
    create_task(server, token, "alpha")
    create_task(server, token, "alpha")
    real = time.time()

    def at(offset: float) -> str:
        monkeypatch.setattr(overview, "now", lambda: real + offset)
        return web.get("/").text

    page = at(0)
    alpha = next(r for r in rows(page) if r["slug"] == "alpha")
    assert alpha["updated-24h"] == "2"
    assert "Tickets updated per day, last 14 days, oldest first: " + "0, " * 13 + "2" in page

    # A day and an hour later: out of the 24-hour count, into the second window.
    page = at(overview.DAY + overview.HOUR)
    alpha = next(r for r in rows(page) if r["slug"] == "alpha")
    assert alpha["updated-24h"] == "0" and alpha["tickets"] == "2"
    assert "0, " * 12 + "2, 0" in page
    assert stat(page, "updated-24h") == "0"
    assert ">1 d ago</time>" in page

    # Past fourteen days: every bar is empty; the tickets still count.
    page = at(15 * overview.DAY)
    assert "oldest first: " + "0, " * 13 + "0" in page
    assert ">2 wk ago</time>" in page


def test_the_server_chart_counts_per_hour(server, token, web, monkeypatch) -> None:
    create_task(server, token, "alpha")
    create_task(server, token, "beta")
    real = time.time()
    monkeypatch.setattr(overview, "now", lambda: real + 2.5 * overview.HOUR)
    page = web.get("/").text
    label = "Tickets updated per hour across these boards, last 24 hours, oldest first: "
    expected = ["0"] * 24
    expected[21] = "2"  # two to three hours ago
    assert label + ", ".join(expected) in page


# ---------------------------------------------------------------------------
# Permissions and sign-in wording
# ---------------------------------------------------------------------------


def test_a_scoped_token_sees_only_its_projects(server, root: Path, token) -> None:
    create_task(server, token, "alpha")
    create_task(server, token, "beta")
    scoped = _logged_in(server, mint(root, user="human:bob", projects=["beta"]))
    page = scoped.get("/").text
    assert [r["slug"] for r in rows(page)] == ["beta"]
    assert stat(page, "boards") == "1" and stat(page, "tickets") == "1"
    assert "/p/alpha/" not in page and "ALP" not in page
    assert "Signed in as human:bob (laptop)" in page
    assert 'id="logout"' in page  # a cookie session can log out


def test_the_page_shape(web) -> None:
    page = web.get("/")
    text = page.text
    assert page.headers["content-type"].startswith("text/html")
    assert "<title>Lattice projects</title>" in text
    assert "<h1>Projects</h1>" in text
    # No network: every asset is this server's own.
    assert not re.search(r'(?:src|href)="(?:https?:)?//', text)
    assert '<script src="/web/overview.js"></script>' in text
    assert '<link rel="icon" type="image/svg+xml" href="/web/favicon.svg">' in text
    assert "<script>" not in text  # no inline script: the CSP hashes only the dashboard's
    js = web.get("/web/overview.js")
    assert js.status == 200 and js.headers["content-type"].startswith("application/javascript")
    icon = web.get("/web/favicon.svg")
    assert icon.status == 200 and icon.headers["content-type"].startswith("image/svg+xml")


# ---------------------------------------------------------------------------
# Speed: one computation per board per head
# ---------------------------------------------------------------------------


def test_a_reload_at_one_head_computes_nothing(server, token, web, monkeypatch) -> None:
    calls: list[str] = []
    original = overview.summarize_board

    def counting(board: Path) -> overview.BoardSummary:
        calls.append(board.parent.name)
        return original(board)

    monkeypatch.setattr(overview, "summarize_board", counting)
    web.get("/")
    web.get("/?sort=name")
    assert sorted(calls) == ["alpha", "beta"]

    calls.clear()
    create_task(server, token, "alpha")  # a new head on alpha only
    web.get("/")
    assert calls == ["alpha"]


def test_an_unavailable_board_is_still_listed(server, token, web) -> None:
    create_task(server, token, "alpha")
    web.get("/")
    project = server.project("alpha")
    with project.locked():
        project._mark_unavailable("test quarantine")
    try:
        page = web.get("/").text
        found = {r["slug"]: r for r in rows(page)}
        assert found["alpha"]["state"] == "unavailable"
        # Unavailable boards sort last, after boards with no edit yet (beta is empty).
        assert [r["slug"] for r in rows(page)] == ["beta", "alpha"]
        assert "Unavailable" in page
    finally:
        with project.locked():
            project.release()


def test_a_board_that_fails_to_summarize_is_logged_and_the_rest_still_show(
    server, token, web, monkeypatch
) -> None:
    create_task(server, token, "beta")
    original = overview.summarize_board

    def broken(board: Path) -> overview.BoardSummary:
        if board.parent.name == "alpha":
            raise RuntimeError("corrupt log")
        return original(board)

    monkeypatch.setattr(overview, "summarize_board", broken)
    page = web.get("/")
    assert page.status == 200
    found = {r["slug"]: r for r in rows(page.text)}
    assert found["alpha"]["state"] == "unavailable"
    assert found["beta"]["tickets"] == "1"
    assert '<span class="code">ALP</span>' in page.text  # the code still reads
    failed = [line for line in server.log_lines if line.get("event") == "overview_board_failed"]
    assert failed and failed[0]["project"] == "alpha" and "corrupt log" in failed[0]["error"]


def test_a_board_whose_admission_fails_is_logged(server, token, web, monkeypatch) -> None:
    from lattice.core.errors import OpError

    project = server.project("alpha")

    def refuse() -> None:
        raise OpError("BOARD_UNAVAILABLE", "test refusal")

    monkeypatch.setattr(project, "admit", refuse)
    found = {r["slug"]: r for r in rows(web.get("/").text)}
    assert found["alpha"]["state"] == "unavailable"
    skipped = [line for line in server.log_lines if line.get("event") == "overview_board_skipped"]
    assert skipped and skipped[0]["error_code"] == "BOARD_UNAVAILABLE"


def test_erased_tasks_leave_the_counts_but_stay_an_edit(server, token, web, clock) -> None:
    clock.set(-30)
    create_task(server, token, "alpha")
    gone = create_task(server, token, "alpha")
    clock.set(-10)
    status, _, body = server.op(
        "alpha", "task.erase", {"task": gone["id"], "reason": "test"}, token=token
    )
    assert status == 200, body
    alpha = next(r for r in rows(web.get("/").text) if r["slug"] == "alpha")
    assert alpha["tickets"] == "1" and alpha["updated-24h"] == "1"
    erased_at = datetime.fromtimestamp(clock.base - 10, timezone.utc)
    assert alpha["last-edit"] == erased_at.strftime("%Y-%m-%dT%H:%M:%SZ")


# ---------------------------------------------------------------------------
# Pure helpers
# ---------------------------------------------------------------------------


def test_window_counts_are_rolling_and_the_newest_takes_the_future() -> None:
    at = 1_000_000.0
    stamps = sorted([at - 10, at - 3600 - 1, at - 3 * 3600, at + 5])
    # Windows start inclusive: at - 3 h opens the second window, not the first.
    assert overview.window_counts(stamps, at, 3600, 4) == [0, 1, 1, 2]
    assert overview.updated_since(stamps, at - 3600) == 2


@pytest.mark.parametrize(
    ("seconds", "text"),
    [
        (None, "—"),
        (-5, "now"),
        (59, "now"),
        (60, "1 min ago"),
        (3599, "59 min ago"),
        (3600, "1 h ago"),
        (86399, "23 h ago"),
        (86400, "1 d ago"),
        (13 * 86400, "13 d ago"),
        (14 * 86400, "2 wk ago"),
        (119 * 86400, "17 wk ago"),
        (400 * 86400, "13 mo ago"),
    ],
)
def test_relative_age(seconds, text) -> None:
    assert overview.relative_age(seconds) == text


def test_bar_height_is_one_log_scale() -> None:
    assert [overview.bar_height(n) for n in (0, 1, 3, 31, 500)] == [0, 20, 40, 100, 100]
