"""Watch command: tail Lattice events in real-time."""

from __future__ import annotations

import json
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

import click

from lattice.cli.helpers import (
    load_project_config,
    output_error,
    require_root,
    resolve_task_id,
)
from lattice.cli.main import cli
from lattice.core.config import resolve_status_input
from lattice.core.errors import OpError
from lattice.core.event_stream import stream_events


def _format_human(event: dict) -> str:
    """Format a single event as a concise human-readable line.

    Example:
        [14:32:05] SUB-55  status_changed  in_progress → review  (agent:claude-impl)
    """
    ts_raw = event.get("ts", "")
    try:
        dt = datetime.fromisoformat(ts_raw.replace("Z", "+00:00"))
        ts = dt.astimezone(timezone.utc).strftime("%H:%M:%S")
    except (ValueError, AttributeError):
        ts = ts_raw[:8] if ts_raw else "?"

    task_id = event.get("task_id", "?")
    etype = event.get("type", "?")
    actor = event.get("actor", "?")
    # actor may be a dict (session identity) or a string
    if isinstance(actor, dict):
        actor = actor.get("name", str(actor))

    data = event.get("data", {})

    # Build a detail string depending on event type
    if etype == "status_changed":
        from_s = data.get("from", "?")
        to_s = data.get("to", "?")
        detail = f"{from_s} \u2192 {to_s}"
    elif etype == "comment_added":
        body = data.get("body", "")
        detail = f'"{body[:60]}"' if body else ""
    elif etype == "assignment_changed":
        from_a = data.get("from") or "(none)"
        to_a = data.get("to") or "(none)"
        detail = f"{from_a} \u2192 {to_a}"
    else:
        # Generic: show first few data keys
        parts = [f"{k}={v}" for k, v in list(data.items())[:3]]
        detail = "  ".join(parts)

    return f"[{ts}] {task_id:<10}  {etype:<20}  {detail:<30}  ({actor})"


def _format_json(event: dict) -> str:
    """Format a single event as a compact JSONL line."""
    data = event.get("data", {})
    actor = event.get("actor", "")
    if isinstance(actor, dict):
        actor = actor.get("name", str(actor))

    out: dict = {
        "ts": event.get("ts", ""),
        "task_id": event.get("task_id", ""),
        "short_code": event.get("short_id", ""),
        "type": event.get("type", ""),
        "data": data,
        "actor": actor,
    }
    return json.dumps(out, sort_keys=True, separators=(",", ":"))


def _build_exec_command(template: str, event: dict) -> str:
    """Substitute template variables into an --exec command string."""
    data = event.get("data", {})
    actor = event.get("actor", "")
    if isinstance(actor, dict):
        actor = actor.get("name", str(actor))

    return template.format(
        task_id=event.get("task_id", ""),
        short_code=event.get("short_id", ""),
        type=event.get("type", ""),
        new_status=data.get("to", ""),
        old_status=data.get("from", ""),
        actor=actor,
        timestamp=event.get("ts", ""),
    )


def _resolve_task_ids(
    lattice_dir: Path,
    task_str: str,
    is_json: bool,
) -> list[str]:
    """Resolve a comma-separated list of short IDs / ULIDs to full ULIDs."""
    raw_ids = [t.strip() for t in task_str.split(",") if t.strip()]
    return [resolve_task_id(lattice_dir, raw, is_json) for raw in raw_ids]


@cli.command("watch")
@click.option(
    "--type",
    "type_filter",
    default=None,
    help="Only show events of this type (e.g. status_changed).",
)
@click.option(
    "--status",
    "status_filter",
    default=None,
    help="Only show status_changed events transitioning TO this status.",
)
@click.option(
    "--task",
    "task_str",
    default=None,
    help="Comma-separated task IDs (short or ULID) to filter.",
)
@click.option(
    "--json",
    "output_json",
    is_flag=True,
    help="Output one JSON object per line (JSONL).",
)
@click.option(
    "--exec",
    "exec_template",
    default=None,
    help=(
        "Shell command to run for each matching event. "
        "Template variables: {task_id}, {short_code}, {type}, "
        "{new_status}, {old_status}, {actor}, {timestamp}. "
        "WARNING: executes arbitrary shell commands."
    ),
)
@click.option(
    "--timeout",
    default=0,
    type=int,
    help="Stop after N seconds (0 = no timeout, default).",
)
@click.option(
    "--poll-fallback",
    default=5,
    type=int,
    help="Poll interval in seconds when fswatch is unavailable (default: 5).",
)
def watch_cmd(
    type_filter: str | None,
    status_filter: str | None,
    task_str: str | None,
    output_json: bool,
    exec_template: str | None,
    timeout: int,
    poll_fallback: int,
) -> None:
    """Tail Lattice events in real-time.

    Streams events as they are written. Uses fswatch when available,
    falls back to polling.

    Examples:

        lattice watch

        lattice watch --type status_changed --status done

        lattice watch --task SUB-55,SUB-56

        lattice watch --json

        lattice watch --type status_changed --status done --exec "echo '{short_code} done'"
    """
    # Deferred: the hosted watch path pulls in the HTTP client, which no
    # other command needs at start-up (LAT-359).
    from lattice.remote.hosted_watch import event_source, hosted_read

    is_json = output_json
    lattice_dir = require_root(is_json)
    task_filter: list[str] | None = None
    try:
        # One read: on a hosted checkout, under the cache's shared read lock.
        with hosted_read(lattice_dir):
            config = load_project_config(lattice_dir)

            # Resolve --status filter to canonical slug
            if status_filter is not None:
                status_filter = resolve_status_input(config, status_filter) or status_filter

            # Resolve --task filter to ULIDs
            if task_str is not None:
                task_filter = _resolve_task_ids(lattice_dir, task_str, is_json)
    except OpError as exc:
        output_error(exc.message, exc.code, is_json)

    # Determine event type filter — if --status is given without --type,
    # implicitly restrict to status_changed.
    type_filter_list: list[str] | None = None
    if type_filter is not None:
        type_filter_list = [type_filter]
    elif status_filter is not None:
        type_filter_list = ["status_changed"]

    if not is_json:
        click.echo("Watching for events... (Ctrl-C to stop)", err=True)

    try:
        for event in event_source(
            stream_events,
            lattice_dir,
            task_filter=task_filter,
            type_filter=type_filter_list,
            poll_interval=poll_fallback,
            timeout=timeout,
        ):
            # Apply --status filter (only on status_changed events)
            if status_filter is not None:
                if event.get("type") == "status_changed":
                    if event.get("data", {}).get("to") != status_filter:
                        continue
                else:
                    continue

            # Output
            if is_json:
                click.echo(_format_json(event))
            else:
                click.echo(_format_human(event))

            # --exec: fire-and-forget (don't block the stream)
            if exec_template is not None:
                try:
                    cmd = _build_exec_command(exec_template, event)
                    subprocess.Popen(cmd, shell=True)  # noqa: S602
                except Exception as exc:  # noqa: BLE001
                    click.echo(f"Warning: --exec failed: {exc}", err=True)

    except OpError as exc:
        output_error(exc.message, exc.code, is_json)
    except KeyboardInterrupt:
        if not is_json:
            click.echo("\nStopped.", err=True)
        sys.exit(0)
