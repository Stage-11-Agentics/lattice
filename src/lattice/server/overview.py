"""The projects overview at ``GET /`` (SPEC §10): one row per board the session's
token may see, built for a server with many boards.

Each row shows the board's slug and project code, its tickets (non-archived
tasks), the tickets updated in the last 24 hours, a 14-day activity mark, and
the board's last edit (the newest ``updated_at`` of any of its tasks, archived
included). ``?sort=edited`` (the default, newest first) or ``?sort=name``
orders the rows; both are plain links, so the page needs no script.

Per board, :func:`summarize_board` reads the task authorities once per head:
the summary is memoized in the board's dashboard read memo (keyed by load and
``(epoch, head_seq)``), and computed inside that memo's authority cache, so the
overview and the hosted dashboard share replays. Everything that depends on
the clock (the 24-hour count, the windows, the relative ages) is derived at
request time from the memoized timestamps, with :func:`now`.
"""

from __future__ import annotations

import bisect
import html
import json
import math
import time
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

DAY = 86400.0
HOUR = 3600.0
#: Rolling 24-hour windows in a row's activity mark; the newest is the 24h count.
MARK_DAYS = 14
#: Rolling one-hour windows in the server chart.
CHART_HOURS = 24
SORTS = ("edited", "name")
MEMO_KEY = ("overview", "")


def now() -> float:
    """The request's clock (seconds since the epoch); tests replace it."""
    return time.time()


# ---------------------------------------------------------------------------
# Per-board summary (memoized per head)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class BoardSummary:
    """What the overview needs from one board at one head."""

    tickets: int
    #: ``updated_at`` of every non-archived task, in seconds, ascending.
    updated: tuple[float, ...]
    #: The newest ``updated_at`` of any task, archived included.
    last_edit: float | None


def parse_ts(value: object) -> float | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.timestamp()


def summarize_board(board: Path) -> BoardSummary:
    """Read *board*'s task authorities (call under the project's work lock)."""
    from lattice.storage.operations import discover_task_authorities

    updated: list[float] = []
    tickets = 0
    last_edit: float | None = None
    for authority in discover_task_authorities(board, include_archived=True):
        stamp = parse_ts(authority.snapshot.get("updated_at"))
        if stamp is not None and (last_edit is None or stamp > last_edit):
            last_edit = stamp
        if authority.location != "active":
            continue
        tickets += 1
        if stamp is not None:
            updated.append(stamp)
    updated.sort()
    return BoardSummary(tickets=tickets, updated=tuple(updated), last_edit=last_edit)


def project_code(board: Path) -> str | None:
    try:
        config = json.loads((board / "config.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    code = config.get("project_code") if isinstance(config, dict) else None
    return code if isinstance(code, str) else None


# ---------------------------------------------------------------------------
# Request-time arithmetic
# ---------------------------------------------------------------------------


def window_counts(stamps: Sequence[float], at: float, width: float, count: int) -> list[int]:
    """Counts of *stamps* (ascending) in *count* rolling windows of *width* seconds
    ending at *at*, oldest first. The newest window also takes stamps after *at*
    (a clock a little ahead), so it always equals :func:`updated_since`."""
    out = []
    for index in range(count):
        start = at - (count - index) * width
        lo = bisect.bisect_left(stamps, start)
        if index == count - 1:
            out.append(len(stamps) - lo)
        else:
            out.append(bisect.bisect_left(stamps, start + width) - lo)
    return out


def updated_since(stamps: Sequence[float], since: float) -> int:
    return len(stamps) - bisect.bisect_left(stamps, since)


def relative_age(seconds: float | None) -> str:
    """``now``, ``12 min ago``, ``5 h ago``, ``3 d ago``, ``6 wk ago``, ``4 mo ago``."""
    if seconds is None:
        return "—"
    minutes = int(max(0.0, seconds) // 60)
    if minutes < 1:
        return "now"
    if minutes < 60:
        return f"{minutes} min ago"
    hours = minutes // 60
    if hours < 24:
        return f"{hours} h ago"
    days = hours // 24
    if days < 14:
        return f"{days} d ago"
    if days < 120:
        return f"{days // 7} wk ago"
    return f"{days // 30} mo ago"


def bar_height(count: int) -> int:
    """A bar's height in percent on the fixed log scale every mark shares: 0 for
    none, at least 12 so one update shows, 100 from 31 updates."""
    if count <= 0:
        return 0
    return max(12, round(min(1.0, math.log2(count + 1) / 5) * 100))


@dataclass(frozen=True)
class Row:
    slug: str
    code: str | None
    summary: BoardSummary | None  # None: the board is unavailable


def sort_rows(rows: Sequence[Row], sort: str) -> list[Row]:
    if sort == "name":
        return sorted(rows, key=lambda r: r.slug)

    def edited(row: Row) -> tuple[int, float, str]:
        """Newest edit first, then boards with no edit, then unavailable boards;
        ties by slug."""
        if row.summary is None:
            return (2, 0.0, row.slug)
        if row.summary.last_edit is None:
            return (1, 0.0, row.slug)
        return (0, -row.summary.last_edit, row.slug)

    return sorted(rows, key=edited)


def normalize_sort(value: str | None) -> str:
    return value if value in SORTS else "edited"


# ---------------------------------------------------------------------------
# The page
# ---------------------------------------------------------------------------

_TK = '<span class="tk" aria-hidden="true"><i></i><i></i><i></i><i></i></span>'


def _iso(seconds: float) -> str:
    return datetime.fromtimestamp(seconds, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _mark(counts: Sequence[int], label: str, cls: str = "mk") -> str:
    last = len(counts) - 1
    bars = []
    for index, count in enumerate(counts):
        height = bar_height(count)
        classes = []
        if not height:
            classes.append("z")
        if index == last and count:
            classes.append("now")
        attr = f' class="{" ".join(classes)}"' if classes else ""
        style = f' style="height:{height}%"' if height else ""
        bars.append(f"<b{attr}{style}></b>")
    aria = html.escape(f"{label}: {', '.join(str(c) for c in counts)}")
    return f'<span class="{cls}" role="img" aria-label="{aria}">{"".join(bars)}</span>'


def _n(value: int | None, unit: str, cls: str, *, hot: bool = False) -> str:
    """A count cell; its unit label shows only where the rows stack (phones)."""
    if value is None:
        return f'<span class="n {cls} zero">—<small>{unit}</small></span>'
    extra = " zero" if value == 0 else " hot" if hot else ""
    return f'<span class="n {cls}{extra}">{value:,}<small>{unit}</small></span>'


def _row(index: int, row: Row, at: float) -> str:
    slug = html.escape(row.slug)
    code = html.escape(row.code or "")
    summary = row.summary
    if summary is None:
        data = f'data-slug="{slug}" data-state="unavailable"'
        cells = (
            _n(None, "tickets", "tot")
            + _n(None, "24h", "day")
            + '<span class="mkc"><span class="pl">Unavailable</span></span>'
            + '<span class="ago">—</span>'
        )
    else:
        days = window_counts(summary.updated, at, DAY, MARK_DAYS)
        day = days[-1]
        live = summary.last_edit is not None and at - summary.last_edit < HOUR
        last = "" if summary.last_edit is None else _iso(summary.last_edit)
        data = (
            f'data-slug="{slug}" data-tickets="{summary.tickets}" data-updated-24h="{day}" '
            f'data-last-edit="{last}"'
        )
        day_cell = _n(day, "24h", "day", hot=live)  # orange: edited within the hour
        age = relative_age(None if summary.last_edit is None else at - summary.last_edit)
        ago = (
            f'<time class="ago" datetime="{last}">{age}</time>'
            if last
            else '<span class="ago">—</span>'
        )
        cells = (
            _n(summary.tickets, "tickets", "tot")
            + day_cell
            + '<span class="mkc">'
            + _mark(days, "Tickets updated per day, last 14 days, oldest first")
            + "</span>"
            + ago
        )
    return (
        f'<a class="row bk" href="/p/{slug}/" {data}>{_TK}'
        f'<span class="ix">{index:02d}</span>'
        f'<span class="name">{slug}</span>'
        f'<span class="code">{code}</span>'
        f"{cells}</a>"
    )


def render(
    rows: Sequence[Row],
    *,
    sort: str,
    signed_in: str,
    logout: bool,
    version: str,
    at: float,
) -> str:
    """The whole page. *rows* are already filtered to the token's projects and
    sorted; *signed_in* is the plain "Signed in as ..." sentence."""
    summaries = [r.summary for r in rows if r.summary is not None]
    tickets = sum(s.tickets for s in summaries)
    day = sum(updated_since(s.updated, at - DAY) for s in summaries)
    hours = [0] * CHART_HOURS
    for s in summaries:
        for index, count in enumerate(window_counts(s.updated, at, HOUR, CHART_HOURS)):
            hours[index] += count
    hot = " hot" if day else ""
    title_block = (
        '<div class="tb">'
        f'<div><div class="pl">Boards</div><div class="v" data-stat="boards">{len(rows):,}</div></div>'
        f'<div><div class="pl">Tickets</div><div class="v" data-stat="tickets">{tickets:,}</div></div>'
        f'<div><div class="pl">Updated 24h</div><div class="v{hot}" data-stat="updated-24h">'
        f"{day:,}</div></div>"
        '<div class="chart">'
        + _mark(hours, "Tickets updated per hour across these boards, last 24 hours, oldest first")
        + '<div class="axis pl"><span>−24 h</span><span>Per hour, all boards</span>'
        "<span>Now</span></div></div></div>"
    )
    links = []
    for key, label in (("edited", "Last edit"), ("name", "Name")):
        if key == sort:
            links.append(f'<a href="/?sort={key}" aria-current="true"><span>{label}</span></a>')
        else:
            links.append(f'<a class="bk" href="/?sort={key}">{_TK}<span>{label}</span></a>')
    header = (
        '<div class="th" aria-hidden="true"><span class="ix">#</span><span class="name">Project</span>'
        '<span class="code">Code</span><span class="tot r">Tickets</span>'
        '<span class="day r">24h</span><span class="mkc">Updated · per day · 14 d</span>'
        '<span class="ago r">Last edit</span></div>'
    )
    body_rows = "".join(_row(i + 1, row, at) for i, row in enumerate(rows))
    if not rows:
        body_rows = '<p class="empty">No projects.</p>'
    logout_html = (
        f'<button type="button" id="logout" class="btn bk">{_TK}<span>Log out</span></button>'
        '<script src="/web/logout.js"></script>'
        if logout
        else ""
    )
    stamp = _iso(at)
    return (
        '<!DOCTYPE html>\n<html lang="en">\n<head>\n<meta charset="utf-8">\n'
        '<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">\n'
        "<title>Lattice projects</title>\n"
        '<link rel="icon" type="image/svg+xml" href="/web/favicon.svg">\n'
        f"<style>\n{OVERVIEW_CSS}</style>\n</head>\n<body>\n"
        '<main class="sheet">\n'
        '<header class="head"><div>'
        '<div class="pl">Lattice <b>/</b> Projects</div>'
        "<h1>Projects</h1>"
        f'<p class="lede">{html.escape(signed_in)}</p>'
        f"</div>{title_block}</header>\n"
        '<div class="zone"><span class="sq">A</span><span class="pl"><b>Boards</b></span>'
        '<span class="line"></span>'
        f'<nav class="seg" aria-label="Sort"><span class="pl">Sort</span>'
        f'<span class="cells">{"".join(links)}</span></nav></div>\n'
        f'<div class="tbl" data-sort="{sort}">{header}{body_rows}</div>\n'
        '<footer class="bottom">'
        f'<span class="pl">As of <time datetime="{stamp}" data-local>{stamp}</time>'
        f" · Lattice {html.escape(version)}</span>{logout_html}</footer>\n"
        "</main>\n"
        '<script src="/web/overview.js"></script>\n'
        "</body>\n</html>\n"
    )


OVERVIEW_JS = """\
// Local time for the overview's <time> elements: a tooltip on each, and the
// visible text where data-local asks for it. The page reads without it.
(function () {
  var options = { dateStyle: "medium", timeStyle: "short" };
  document.querySelectorAll("time[datetime]").forEach(function (el) {
    var when = new Date(el.getAttribute("datetime"));
    if (isNaN(when)) return;
    var text = when.toLocaleString(undefined, options);
    el.title = text;
    if (el.hasAttribute("data-local")) el.textContent = text;
  });
})();
"""


OVERVIEW_CSS = """\
:root{--accent:#FF4F1F;--bg:#0C0D0F;--surface:#111214;--rule:#2C2E33;--rule-soft:#1F2024;
--ink:#FFFFFF;--text:rgba(255,255,255,.92);--text-2:rgba(255,255,255,.58);--text-dim:rgba(255,255,255,.46);
--inv-bg:#FFFFFF;--inv-fg:#111214;--bracket:rgba(255,255,255,.78);--bar-off:#2C2E33;
--mono:ui-monospace,"SF Mono",Menlo,Consolas,monospace;--sans:system-ui,-apple-system,"Segoe UI",sans-serif;
--tk-len:6px;--tk-w:1.5px;--tk-gap:3px;color-scheme:dark}
@media (prefers-color-scheme:light){:root{--bg:#F7F7F8;--surface:#FFFFFF;--rule:#111214;--rule-soft:#D3D4D8;
--ink:#111214;--text:#111214;--text-2:#111214;--text-dim:rgba(17,18,20,.5);
--inv-bg:#111214;--inv-fg:#FFFFFF;--bracket:#111214;--bar-off:#D3D4D8;color-scheme:light}}
*{box-sizing:border-box;border-radius:0}
html,body{margin:0}
body{background:var(--bg);color:var(--text);font:400 13px/18px var(--sans);-webkit-font-smoothing:antialiased;
-webkit-text-size-adjust:100%;font-variant-numeric:tabular-nums;padding:40px 16px 64px}
a{color:inherit;text-decoration:none;-webkit-tap-highlight-color:transparent}
.sheet{max-width:1000px;margin:0 auto;background:var(--surface);border:1px solid var(--rule);padding:32px 40px 28px}
.pl{font:500 10px/14px var(--mono);letter-spacing:.06em;text-transform:uppercase;color:var(--text-2)}
.pl b{font-weight:600;color:var(--text)}
.bk{position:relative}
.tk{position:absolute;inset:calc(-1*(var(--tk-gap) + var(--tk-w)));pointer-events:none;opacity:0;
transition:opacity 60ms linear;color:var(--bracket)}
.tk i{position:absolute;width:var(--tk-len);height:var(--tk-len);border:0 solid currentColor}
.tk i:nth-child(1){top:0;left:0;border-top-width:var(--tk-w);border-left-width:var(--tk-w)}
.tk i:nth-child(2){top:0;right:0;border-top-width:var(--tk-w);border-right-width:var(--tk-w)}
.tk i:nth-child(3){bottom:0;left:0;border-bottom-width:var(--tk-w);border-left-width:var(--tk-w)}
.tk i:nth-child(4){bottom:0;right:0;border-bottom-width:var(--tk-w);border-right-width:var(--tk-w)}
@media (hover:hover){.bk:hover>.tk{opacity:1}}
.bk:focus-visible>.tk{opacity:1}
.bk:focus-visible{outline:none}
.bk:active>:not(.tk){opacity:.55}
@media (prefers-reduced-motion:reduce){.tk{transition:none}}
.head{display:grid;grid-template-columns:minmax(0,1fr) 290px;gap:32px;align-items:end;padding-bottom:20px;
border-bottom:1px solid var(--rule)}
h1{font:600 28px/32px var(--sans);margin:6px 0 8px;letter-spacing:-.01em}
.lede{color:var(--text-2);margin:0;max-width:520px;overflow-wrap:anywhere}
.tb{display:grid;grid-template-columns:repeat(3,1fr);border:1px solid var(--rule)}
.tb>div{padding:6px 8px 7px;border-right:1px solid var(--rule);min-width:0}
.tb>div:nth-child(3){border-right:0}
.tb .v{font:600 18px/22px var(--mono);margin-top:2px;white-space:nowrap}
.tb .v.hot{color:var(--accent)}
.tb .chart{grid-column:1/-1;border-top:1px solid var(--rule);border-right:0;padding:8px 8px 6px}
.axis{display:flex;justify-content:space-between;margin-top:4px}
.zone{display:flex;align-items:center;gap:10px;margin:22px 0 10px}
.zone .sq{width:16px;height:16px;border:1px solid var(--rule);display:grid;place-items:center;font:600 10px/1 var(--mono)}
.zone .line{flex:1;height:1px;background:var(--rule)}
.seg{display:flex;align-items:center;gap:8px}
.seg .cells{display:flex;border:1px solid var(--rule)}
.seg a{display:block;width:92px;text-align:center;font:500 10px/24px var(--mono);letter-spacing:.06em;
text-transform:uppercase;color:var(--text-2)}
.seg a+a{border-left:1px solid var(--rule)}
.seg a[aria-current="true"]{background:var(--inv-bg);color:var(--inv-fg)}
.tbl{margin:0 8px}
.row,.th{display:grid;column-gap:16px;align-items:center;
grid-template-columns:28px minmax(0,1fr) 56px 72px 48px 146px 92px;
grid-template-areas:"ix name code tot day mark ago"}
.th{padding:0 0 6px;border-bottom:1px solid var(--rule)}
.th>span{font:500 10px/14px var(--mono);letter-spacing:.06em;text-transform:uppercase;color:var(--text-2);white-space:nowrap}
.th .r{text-align:right}
.row{height:44px;border-bottom:1px solid var(--rule-soft)}
.ix{grid-area:ix;font:500 10.5px/1 var(--mono);color:var(--text-dim)}
.name{grid-area:name;font:500 14px/20px var(--sans);white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.code{grid-area:code;font:500 10.5px/1 var(--mono);letter-spacing:.06em;color:var(--text-2);white-space:nowrap;overflow:hidden}
.tot{grid-area:tot}.day{grid-area:day}
.n{font:600 14px/18px var(--mono);text-align:right;white-space:nowrap}
.n small{display:none;font:500 10px/1 var(--mono);letter-spacing:.06em;text-transform:uppercase;color:var(--text-2);margin-left:4px}
.n.zero{color:var(--text-dim);font-weight:500}
.n.hot{color:var(--accent)}
.mkc{grid-area:mark}
.ago{grid-area:ago;font:500 12px/18px var(--mono);text-align:right;color:var(--text-2);white-space:nowrap}
.mk{display:flex;align-items:flex-end;gap:2px;height:20px}
.mk b{display:block;flex:none;width:8px;background:var(--ink)}
.mk b.z{height:1px;background:var(--bar-off)}
.mk b.now{background:var(--accent)}
.chart .mk{height:44px;gap:3px}
.chart .mk b{flex:1 1 0;width:auto}
.empty{margin:0;padding:14px 0;color:var(--text-2);border-bottom:1px solid var(--rule-soft)}
.bottom{display:flex;justify-content:space-between;align-items:center;gap:16px;margin-top:22px;padding-top:12px;
border-top:1px solid var(--rule)}
.btn{font:500 10px/24px var(--mono);letter-spacing:.06em;text-transform:uppercase;border:1px solid var(--rule);
padding:0 12px;background:none;color:var(--text);cursor:pointer}
@media (max-width:720px){
body{padding:0 0 calc(76px + env(safe-area-inset-bottom))}
.sheet{border:0;padding:20px 16px 24px}
.head{grid-template-columns:1fr;gap:16px;padding-bottom:16px}
h1{font-size:24px;line-height:28px}
.zone{margin:16px 0 4px}
.seg{position:fixed;left:0;right:0;bottom:0;z-index:5;gap:12px;background:var(--surface);border-top:1px solid var(--rule);
padding:8px 16px calc(8px + env(safe-area-inset-bottom))}
.seg .cells{flex:1}
.seg a{flex:1 1 0;width:auto;font-size:11px;line-height:44px}
.tbl{margin:0}
.th{display:none}
.row{height:64px;row-gap:6px;align-content:center;grid-template-columns:auto auto auto minmax(0,1fr);
grid-template-areas:"name name name ago" "code tot day mark"}
.ix{display:none}
.name{font-size:15px}
.code{font-size:10px;min-width:40px}
.n{font-size:12px;line-height:16px;text-align:left}
.n small{display:inline}
.ago{font-size:11px}
.mkc{justify-self:end}
.row .mk{height:16px}
.row .mk b{width:5px}
.bottom{flex-wrap:wrap}
.btn{line-height:44px;padding:0 16px}
}
@media (max-width:360px){.row .mk b{width:4px}.row{column-gap:12px}}
"""
