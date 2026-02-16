"""Statistics and insight commands."""

from __future__ import annotations

import click

from lattice.cli.helpers import json_envelope, load_project_config, require_root
from lattice.cli.main import cli
from lattice.core.stats import build_stats, format_days


# ---------------------------------------------------------------------------
# Human-readable output
# ---------------------------------------------------------------------------


def _bar(count: int, total: int, width: int = 20) -> str:
    """Render a proportional bar."""
    if total == 0:
        return ""
    filled = round(count / total * width)
    return "#" * filled + "." * (width - filled)


def _print_human_stats(stats: dict, config: dict) -> None:
    """Print stats as a human-readable dashboard."""
    s = stats["summary"]
    project_code = config.get("project_code", "")
    instance_name = config.get("instance_name", "")
    header = instance_name or project_code or "Lattice"

    click.echo(f"=== {header} Stats ===")
    click.echo("")
    click.echo(
        f"Tasks: {s['active_tasks']} active, "
        f"{s['archived_tasks']} archived "
        f"({s['total_tasks']} total)"
    )
    click.echo(f"Events: {s['total_events']} total ({s['active_events']} on active tasks)")
    click.echo("")

    # Status
    if stats["by_status"]:
        click.echo("Status:")
        total = s["active_tasks"]
        for status, count in stats["by_status"]:
            bar = _bar(count, total)
            click.echo(f"  {status:<20s} {count:>3d}  {bar}")
        click.echo("")

    # WIP limits
    wip_alerts = [w for w in stats["wip"] if w["over"]]
    if wip_alerts:
        click.echo("WIP Limit Exceeded:")
        for w in wip_alerts:
            click.echo(
                f"  {w['status']}: {w['current']}/{w['limit']}"
            )
        click.echo("")

    # Priority
    if stats["by_priority"]:
        click.echo("Priority:")
        for priority, count in stats["by_priority"]:
            click.echo(f"  {priority:<12s} {count:>3d}")
        click.echo("")

    # Type
    if stats["by_type"]:
        click.echo("Type:")
        for task_type, count in stats["by_type"]:
            click.echo(f"  {task_type:<12s} {count:>3d}")
        click.echo("")

    # Assignees
    if stats["by_assignee"]:
        click.echo("Assigned:")
        for assignee, count in stats["by_assignee"]:
            click.echo(f"  {assignee:<30s} {count:>3d}")
        click.echo("")

    # Tags
    if stats["by_tag"]:
        click.echo("Tags:")
        for tag, count in stats["by_tag"]:
            click.echo(f"  {tag:<20s} {count:>3d}")
        click.echo("")

    # Recently active
    if stats["recently_active"]:
        click.echo("Recently Active:")
        for t in stats["recently_active"]:
            click.echo(
                f"  {t['id']:<10s} {t['status']:<20s} {t['updated_ago']:>5s} ago  "
                f"\"{t['title']}\""
            )
        click.echo("")

    # Stale
    if stats["stale"]:
        click.echo(f"Stale (7+ days idle): {len(stats['stale'])} tasks")
        for t in stats["stale"][:10]:  # cap display at 10
            click.echo(
                f"  {t['id']:<10s} {t['status']:<20s} {format_days(t['days_stale']):>5s}  "
                f"\"{t['title']}\""
            )
        if len(stats["stale"]) > 10:
            click.echo(f"  ... and {len(stats['stale']) - 10} more")
        click.echo("")

    # Busiest tasks
    if stats["busiest"]:
        click.echo("Most Active (by event count):")
        for t in stats["busiest"]:
            click.echo(
                f"  {t['id']:<10s} {t['event_count']:>4d} events  \"{t['title']}\""
            )


# ---------------------------------------------------------------------------
# lattice stats
# ---------------------------------------------------------------------------


@cli.command("stats")
@click.option("--json", "output_json", is_flag=True, help="Output structured JSON.")
def stats_cmd(output_json: bool) -> None:
    """Show project statistics and insights."""
    is_json = output_json

    lattice_dir = require_root(is_json)
    config = load_project_config(lattice_dir)

    stats = build_stats(lattice_dir, config)

    if is_json:
        click.echo(json_envelope(True, data=stats))
    else:
        _print_human_stats(stats, config)
