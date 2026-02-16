"""Weather report command — daily project digest."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import click

from lattice.cli.helpers import json_envelope, load_project_config, require_root
from lattice.cli.main import cli
from lattice.core.stats import (
    build_stats,
    days_ago,
    format_days,
    load_all_snapshots,
    parse_ts,
)

# Future config shape for scheduling:
# "schedule": {
#     "weather_report": {
#         "enabled": true,
#         "cron": "0 8 * * 1-5",  # 8am weekdays
#         "format": "markdown",
#         "delivery": "stdout"  # or "file:/path" or future: "email:addr" / "webhook:url"
#     }
# }


# ---------------------------------------------------------------------------
# Weather metaphor logic
# ---------------------------------------------------------------------------

_WEATHER_CLEAR = "Clear skies"
_WEATHER_FAIR = "Fair weather"
_WEATHER_PARTLY_CLOUDY = "Partly cloudy"
_WEATHER_OVERCAST = "Overcast"
_WEATHER_STORMY = "Stormy"


def _determine_weather(
    stale_count: int,
    active_count: int,
    wip_breaches: int,
    recently_completed: int,
) -> str:
    """Determine the weather metaphor based on project health heuristics.

    Rules (evaluated top-to-bottom, first match wins):
    - "Stormy" — >50% of active tasks are stale, or critical WIP breaches (3+)
    - "Overcast" — many stale tasks (5+), or multiple WIP breaches (2+)
    - "Partly cloudy" — some stale tasks (3-5), or 1 WIP breach
    - "Fair weather" — minor staleness (<3 stale tasks), no WIP breaches
    - "Clear skies" — no stale tasks, no WIP breaches, tasks completing
    """
    if active_count > 0 and stale_count > active_count * 0.5:
        return _WEATHER_STORMY
    if wip_breaches >= 3:
        return _WEATHER_STORMY
    if stale_count >= 5 or wip_breaches >= 2:
        return _WEATHER_OVERCAST
    if 3 <= stale_count < 5 or wip_breaches == 1:
        return _WEATHER_PARTLY_CLOUDY
    if 0 < stale_count < 3:
        return _WEATHER_FAIR
    # No stale, no WIP breaches
    return _WEATHER_CLEAR


# ---------------------------------------------------------------------------
# Data gathering
# ---------------------------------------------------------------------------


def _load_recent_events(lattice_dir: Path, hours: float = 24.0) -> list[dict]:
    """Load all events from the last *hours* hours."""
    now = datetime.now(timezone.utc)
    cutoff_seconds = hours * 3600
    recent: list[dict] = []

    events_dir = lattice_dir / "events"
    if not events_dir.is_dir():
        return recent

    for f in events_dir.glob("*.jsonl"):
        if f.name.startswith("_"):
            continue
        for line in f.read_text().splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                ev = json.loads(line)
            except json.JSONDecodeError:
                continue
            ts = parse_ts(ev.get("ts", ""))
            if ts is not None:
                delta = (now - ts).total_seconds()
                if delta <= cutoff_seconds:
                    recent.append(ev)

    return recent


def _find_recently_completed(
    lattice_dir: Path,
    active: list[dict],
    archived: list[dict],
    hours: float = 24.0,
    fallback_hours: float = 72.0,
) -> list[dict]:
    """Find tasks moved to 'done' recently.

    Checks last *hours* first; if none found, expands to *fallback_hours*.
    Returns list of dicts with id, title, completed_ago.
    """
    now = datetime.now(timezone.utc)
    all_tasks = active + archived

    done_tasks: list[dict] = []
    for snap in all_tasks:
        if snap.get("status") != "done":
            continue
        updated = snap.get("updated_at", "")
        days = days_ago(updated, now)
        if days is not None:
            done_tasks.append({
                "id": snap.get("short_id") or snap.get("id", "?"),
                "title": snap.get("title", "?"),
                "days_ago": days,
                "completed_ago": format_days(days),
            })

    # Sort by most recent first
    done_tasks.sort(key=lambda t: t["days_ago"])

    cutoff_days = hours / 24.0
    recent = [t for t in done_tasks if t["days_ago"] <= cutoff_days]
    if recent:
        return recent

    fallback_days = fallback_hours / 24.0
    return [t for t in done_tasks if t["days_ago"] <= fallback_days]


def _find_up_next(active: list[dict]) -> list[dict]:
    """Find backlog/planned tasks ready to pick up, ordered by priority."""
    priority_order = {"critical": 0, "high": 1, "medium": 2, "low": 3, "unset": 4}
    ready_statuses = {"backlog", "planned"}

    candidates: list[dict] = []
    for snap in active:
        if snap.get("status") in ready_statuses:
            pri = snap.get("priority", "unset")
            candidates.append({
                "id": snap.get("short_id") or snap.get("id", "?"),
                "title": snap.get("title", "?"),
                "status": snap.get("status", "?"),
                "priority": pri,
                "assigned_to": snap.get("assigned_to"),
                "_sort": priority_order.get(pri, 99),
            })

    candidates.sort(key=lambda c: c["_sort"])
    # Strip sort key from output
    for c in candidates:
        del c["_sort"]
    return candidates[:10]  # Cap at 10


def _find_attention_needed(
    active: list[dict],
    stale: list[dict],
    wip_status: list[dict],
) -> list[dict]:
    """Compile items needing attention.

    Returns a list of attention items, each with 'type' and 'detail'.
    """
    items: list[dict] = []

    # Stale tasks
    for t in stale:
        items.append({
            "type": "stale",
            "id": t["id"],
            "title": t["title"],
            "detail": f"Idle for {format_days(t['days_stale'])}",
        })

    # WIP breaches
    for w in wip_status:
        if w["over"]:
            items.append({
                "type": "wip_breach",
                "status": w["status"],
                "detail": f"{w['current']}/{w['limit']} (over by {w['current'] - w['limit']})",
            })

    # Unassigned in-progress tasks
    in_progress_statuses = {"in_planning", "in_implementation", "in_review"}
    for snap in active:
        if snap.get("status") in in_progress_statuses and not snap.get("assigned_to"):
            items.append({
                "type": "unassigned_active",
                "id": snap.get("short_id") or snap.get("id", "?"),
                "title": snap.get("title", "?"),
                "status": snap.get("status", "?"),
                "detail": "In progress but unassigned",
            })

    return items


def _build_weather(lattice_dir: Path, config: dict) -> dict:
    """Build the full weather report data structure."""
    now = datetime.now(timezone.utc)
    stats = build_stats(lattice_dir, config)
    active, archived = load_all_snapshots(lattice_dir)

    # Recent events
    recent_events = _load_recent_events(lattice_dir, hours=24.0)

    # In-progress count
    in_progress_statuses = {"in_planning", "in_implementation", "in_review"}
    in_progress_count = sum(
        1 for snap in active if snap.get("status") in in_progress_statuses
    )

    # Recently completed
    recently_completed = _find_recently_completed(lattice_dir, active, archived)

    # Attention needed
    attention = _find_attention_needed(active, stats["stale"], stats["wip"])

    # Up next
    up_next = _find_up_next(active)

    # WIP breaches
    wip_breaches = sum(1 for w in stats["wip"] if w["over"])

    # Weather metaphor
    weather = _determine_weather(
        stale_count=len(stats["stale"]),
        active_count=stats["summary"]["active_tasks"],
        wip_breaches=wip_breaches,
        recently_completed=len(recently_completed),
    )

    project_code = config.get("project_code", "")
    instance_name = config.get("instance_name", "")
    project_name = instance_name or project_code or "Lattice"

    return {
        "headline": {
            "project": project_name,
            "date": now.strftime("%Y-%m-%d"),
            "weather": weather,
        },
        "vital_signs": {
            "active_tasks": stats["summary"]["active_tasks"],
            "in_progress": in_progress_count,
            "done_recently": len(recently_completed),
            "events_24h": len(recent_events),
        },
        "attention": attention,
        "recently_completed": recently_completed,
        "up_next": up_next,
    }


# ---------------------------------------------------------------------------
# Output formatting — plain text
# ---------------------------------------------------------------------------


def _weather_emoji(weather: str) -> str:
    """Return a simple ASCII indicator for the weather state."""
    mapping = {
        _WEATHER_CLEAR: "[OK]",
        _WEATHER_FAIR: "[--]",
        _WEATHER_PARTLY_CLOUDY: "[~~]",
        _WEATHER_OVERCAST: "[##]",
        _WEATHER_STORMY: "[!!]",
    }
    return mapping.get(weather, "[??]")


def _print_text_weather(data: dict) -> None:
    """Print weather report as plain text."""
    h = data["headline"]
    v = data["vital_signs"]
    indicator = _weather_emoji(h["weather"])

    click.echo(f"=== {h['project']} Weather Report — {h['date']} ===")
    click.echo(f"{indicator} {h['weather']}")
    click.echo("")

    # Vital signs
    click.echo("Vital Signs:")
    click.echo(f"  Active tasks:      {v['active_tasks']}")
    click.echo(f"  In progress:       {v['in_progress']}")
    click.echo(f"  Completed recently:{v['done_recently']:>3d}")
    click.echo(f"  Events (24h):      {v['events_24h']}")
    click.echo("")

    # Attention needed
    if data["attention"]:
        click.echo(f"Attention Needed ({len(data['attention'])} items):")
        for item in data["attention"]:
            if item["type"] == "stale":
                click.echo(f"  [STALE] {item['id']} — {item['detail']} — \"{item['title']}\"")
            elif item["type"] == "wip_breach":
                click.echo(f"  [WIP]   {item['status']} — {item['detail']}")
            elif item["type"] == "unassigned_active":
                click.echo(
                    f"  [UNASGN] {item['id']} — {item['status']} — \"{item['title']}\""
                )
        click.echo("")
    else:
        click.echo("Attention Needed: None")
        click.echo("")

    # Recently completed
    if data["recently_completed"]:
        click.echo(f"Recently Completed ({len(data['recently_completed'])}):")
        for t in data["recently_completed"]:
            click.echo(f"  {t['id']:<10s} {t['completed_ago']:>5s} ago  \"{t['title']}\"")
        click.echo("")
    else:
        click.echo("Recently Completed: None")
        click.echo("")

    # Up next
    if data["up_next"]:
        click.echo(f"Up Next ({len(data['up_next'])}):")
        for t in data["up_next"]:
            assigned = f" ({t['assigned_to']})" if t.get("assigned_to") else ""
            click.echo(
                f"  {t['id']:<10s} [{t['priority']}] {t['status']:<10s} "
                f"\"{t['title']}\"{assigned}"
            )
    else:
        click.echo("Up Next: Nothing in backlog/planned")


# ---------------------------------------------------------------------------
# Output formatting — markdown
# ---------------------------------------------------------------------------


def _print_markdown_weather(data: dict) -> None:
    """Print weather report as markdown."""
    h = data["headline"]
    v = data["vital_signs"]

    click.echo(f"# {h['project']} Weather Report")
    click.echo(f"**Date:** {h['date']}  ")
    click.echo(f"**Forecast:** {h['weather']}")
    click.echo("")

    # Vital signs
    click.echo("## Vital Signs")
    click.echo("| Metric | Value |")
    click.echo("|--------|-------|")
    click.echo(f"| Active tasks | {v['active_tasks']} |")
    click.echo(f"| In progress | {v['in_progress']} |")
    click.echo(f"| Completed recently | {v['done_recently']} |")
    click.echo(f"| Events (24h) | {v['events_24h']} |")
    click.echo("")

    # Attention needed
    click.echo("## Attention Needed")
    if data["attention"]:
        for item in data["attention"]:
            if item["type"] == "stale":
                click.echo(f"- **STALE** `{item['id']}` — {item['detail']} — {item['title']}")
            elif item["type"] == "wip_breach":
                click.echo(f"- **WIP BREACH** `{item['status']}` — {item['detail']}")
            elif item["type"] == "unassigned_active":
                click.echo(
                    f"- **UNASSIGNED** `{item['id']}` — {item['status']} — {item['title']}"
                )
    else:
        click.echo("Nothing needs attention.")
    click.echo("")

    # Recently completed
    click.echo("## Recently Completed")
    if data["recently_completed"]:
        for t in data["recently_completed"]:
            click.echo(f"- `{t['id']}` — {t['title']} ({t['completed_ago']} ago)")
    else:
        click.echo("No recent completions.")
    click.echo("")

    # Up next
    click.echo("## Up Next")
    if data["up_next"]:
        for t in data["up_next"]:
            assigned = f" (assigned: {t['assigned_to']})" if t.get("assigned_to") else ""
            click.echo(f"- `{t['id']}` [{t['priority']}] {t['title']}{assigned}")
    else:
        click.echo("Nothing in backlog/planned.")


# ---------------------------------------------------------------------------
# lattice weather
# ---------------------------------------------------------------------------


@cli.command("weather")
@click.option("--json", "output_json", is_flag=True, help="Output structured JSON.")
@click.option("--markdown", "output_markdown", is_flag=True, help="Output formatted markdown.")
def weather_cmd(output_json: bool, output_markdown: bool) -> None:
    """Generate a daily project weather report / digest."""
    is_json = output_json

    lattice_dir = require_root(is_json)
    config = load_project_config(lattice_dir)

    data = _build_weather(lattice_dir, config)

    if is_json:
        click.echo(json_envelope(True, data=data))
    elif output_markdown:
        _print_markdown_weather(data)
    else:
        _print_text_weather(data)
