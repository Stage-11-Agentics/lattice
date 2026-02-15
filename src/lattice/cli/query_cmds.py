"""Query and display commands: event, list, show."""

from __future__ import annotations

import json
from pathlib import Path

import click

from lattice.cli.helpers import (
    common_options,
    json_envelope,
    load_project_config,
    output_error,
    output_result,
    read_snapshot,
    read_snapshot_or_exit,
    require_root,
    resolve_task_id,
    validate_actor_or_exit,
    write_task_event,
)
from lattice.cli.main import cli
from lattice.core.events import (
    BUILTIN_EVENT_TYPES,
    create_event,
    validate_custom_event_type,
)
from lattice.core.ids import validate_id
from lattice.core.tasks import apply_event_to_snapshot, compact_snapshot


# ---------------------------------------------------------------------------
# lattice event
# ---------------------------------------------------------------------------


@cli.command("event")
@click.argument("task_id")
@click.argument("event_type")
@click.option("--data", "data_str", default=None, help="JSON string for event data.")
@click.option("--id", "ev_id", default=None, help="Caller-supplied event ID.")
@common_options
def event_cmd(
    task_id: str,
    event_type: str,
    data_str: str | None,
    ev_id: str | None,
    actor: str,
    model: str | None,
    session: str | None,
    output_json: bool,
    quiet: bool,
) -> None:
    """Record a custom event on a task.

    Custom event types must start with 'x_' (e.g., x_deployment_started).
    Built-in types like status_changed or task_created are reserved.
    """
    is_json = output_json

    lattice_dir = require_root(is_json)
    config = load_project_config(lattice_dir)
    validate_actor_or_exit(actor, is_json)

    task_id = resolve_task_id(lattice_dir, task_id, is_json)

    # Validate event type is custom (x_ prefix)
    if event_type in BUILTIN_EVENT_TYPES:
        output_error(
            f"Event type '{event_type}' is reserved. Custom types must start with 'x_'.",
            "VALIDATION_ERROR",
            is_json,
        )
    if not validate_custom_event_type(event_type):
        output_error(
            f"Invalid custom event type: '{event_type}'. Custom types must start with 'x_'.",
            "VALIDATION_ERROR",
            is_json,
        )

    # Parse --data
    event_data: dict = {}
    if data_str is not None:
        try:
            event_data = json.loads(data_str)
        except json.JSONDecodeError as exc:
            output_error(
                f"Invalid JSON in --data: {exc}",
                "VALIDATION_ERROR",
                is_json,
            )
        if not isinstance(event_data, dict):
            output_error(
                "--data must be a JSON object.",
                "VALIDATION_ERROR",
                is_json,
            )

    # Validate task exists
    snapshot = read_snapshot_or_exit(lattice_dir, task_id, is_json)

    # Validate --id if provided
    if ev_id is not None:
        if not validate_id(ev_id, "ev"):
            output_error(
                f"Invalid event ID format: '{ev_id}'.",
                "INVALID_ID",
                is_json,
            )

        # Idempotency check: scan event log for matching ID
        event_path = lattice_dir / "events" / f"{task_id}.jsonl"
        if event_path.exists():
            for line in event_path.read_text().splitlines():
                line = line.strip()
                if not line:
                    continue
                try:
                    existing = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if existing.get("id") == ev_id:
                    # Same ID found — check if payload matches
                    if existing.get("type") == event_type and existing.get("data") == event_data:
                        output_result(
                            data=existing,
                            human_message=f"Event {ev_id} already exists (idempotent).",
                            quiet_value=ev_id,
                            is_json=is_json,
                            is_quiet=quiet,
                        )
                        return
                    else:
                        output_error(
                            f"Conflict: event {ev_id} exists with different data.",
                            "CONFLICT",
                            is_json,
                        )

    # Build event and apply to snapshot
    event = create_event(
        type=event_type,
        task_id=task_id,
        actor=actor,
        data=event_data,
        event_id=ev_id,
        model=model,
        session=session,
    )
    updated_snapshot = apply_event_to_snapshot(snapshot, event)

    # Write (event-first, then snapshot, under lock)
    # Custom events do NOT go to _lifecycle.jsonl — write_task_event handles
    # this automatically since the type is x_* (not in LIFECYCLE_EVENT_TYPES).
    write_task_event(lattice_dir, task_id, [event], updated_snapshot, config)

    output_result(
        data=event,
        human_message=f"Recorded {event_type} on {task_id}",
        quiet_value=event["id"],
        is_json=is_json,
        is_quiet=quiet,
    )


# ---------------------------------------------------------------------------
# lattice list
# ---------------------------------------------------------------------------


@cli.command("list")
@click.option("--status", default=None, help="Filter by status.")
@click.option("--assigned", default=None, help="Filter by assigned actor.")
@click.option("--tag", default=None, help="Filter by tag.")
@click.option("--type", "task_type", default=None, help="Filter by task type.")
@click.option("--compact", is_flag=True, help="Compact JSON output.")
@click.option("--json", "output_json", is_flag=True, help="Output structured JSON.")
@click.option("--quiet", is_flag=True, help="Print one task ID per line.")
def list_cmd(
    status: str | None,
    assigned: str | None,
    tag: str | None,
    task_type: str | None,
    compact: bool,
    output_json: bool,
    quiet: bool,
) -> None:
    """List tasks with optional filters."""
    is_json = output_json

    lattice_dir = require_root(is_json)

    # Scan all .json files in tasks/ directory
    tasks_dir = lattice_dir / "tasks"
    snapshots: list[dict] = []

    if tasks_dir.is_dir():
        for task_file in sorted(tasks_dir.glob("*.json")):
            try:
                snap = json.loads(task_file.read_text())
            except (json.JSONDecodeError, OSError):
                continue
            snapshots.append(snap)

    # Apply filters (AND combination)
    filtered: list[dict] = []
    for snap in snapshots:
        if status is not None and snap.get("status") != status:
            continue
        if assigned is not None and snap.get("assigned_to") != assigned:
            continue
        if tag is not None and tag not in (snap.get("tags") or []):
            continue
        if task_type is not None and snap.get("type") != task_type:
            continue
        filtered.append(snap)

    # Sort by task ID (ULID = chronological order)
    filtered.sort(key=lambda s: s.get("id", ""))

    # Output
    if is_json:
        if compact:
            data = [compact_snapshot(s) for s in filtered]
        else:
            data = filtered
        click.echo(json_envelope(True, data=data))
    elif quiet:
        for snap in filtered:
            short_id = snap.get("short_id")
            click.echo(short_id if short_id else snap.get("id", ""))
    else:
        # Human output: compact one-line-per-task table
        for snap in filtered:
            short_id = snap.get("short_id")
            display_id = short_id if short_id else snap.get("id", "?")
            s = snap.get("status", "?")
            p = snap.get("priority", "?")
            t = snap.get("type", "?")
            title = snap.get("title", "?")
            assigned_to = snap.get("assigned_to") or "unassigned"
            click.echo(f'{display_id}  {s}  {p}  {t}  "{title}"  {assigned_to}')


# ---------------------------------------------------------------------------
# lattice show
# ---------------------------------------------------------------------------


@cli.command("show")
@click.argument("task_id")
@click.option("--full", is_flag=True, help="Include complete event data.")
@click.option("--compact", is_flag=True, help="Compact output only.")
@click.option("--json", "output_json", is_flag=True, help="Output structured JSON.")
def show_cmd(
    task_id: str,
    full: bool,
    compact: bool,
    output_json: bool,
) -> None:
    """Show detailed task information."""
    is_json = output_json

    lattice_dir = require_root(is_json)

    task_id = resolve_task_id(lattice_dir, task_id, is_json, allow_archived=True)

    # Try to read task snapshot from tasks/
    snapshot = read_snapshot(lattice_dir, task_id)
    is_archived = False

    if snapshot is None:
        # Check archive
        archive_path = lattice_dir / "archive" / "tasks" / f"{task_id}.json"
        if archive_path.exists():
            try:
                snapshot = json.loads(archive_path.read_text())
                is_archived = True
            except (json.JSONDecodeError, OSError):
                pass

    if snapshot is None:
        output_error(f"Task {task_id} not found.", "NOT_FOUND", is_json)

    # Compact mode: just show compact fields, no events/relationships/artifacts
    if compact:
        if is_json:
            data = compact_snapshot(snapshot)
            if is_archived:
                data["archived"] = True
            click.echo(json_envelope(True, data=data))
        else:
            _print_compact_show(snapshot, is_archived)
        return

    # Read event log
    events = _read_events(lattice_dir, task_id, is_archived)

    # Check for notes file
    if is_archived:
        notes_path = lattice_dir / "archive" / "notes" / f"{task_id}.md"
    else:
        notes_path = lattice_dir / "notes" / f"{task_id}.md"
    has_notes = notes_path.exists()

    # Read outgoing relationship target titles (best effort)
    relationships_out = _enrich_relationships(lattice_dir, snapshot)

    # Derive incoming relationships by scanning all task snapshots
    relationships_in = _find_incoming_relationships(lattice_dir, task_id)

    # Read artifact metadata (best effort)
    artifact_info = _read_artifact_info(lattice_dir, snapshot)

    if is_json:
        data: dict = dict(snapshot)
        data["events"] = events
        if is_archived:
            data["archived"] = True
        if has_notes:
            data["notes_path"] = f"notes/{task_id}.md"
        data["relationships_enriched"] = relationships_out
        data["relationships_in"] = relationships_in
        data["artifact_info"] = artifact_info
        if full:
            data["_full"] = True
        click.echo(json_envelope(True, data=data))
    else:
        _print_human_show(
            snapshot,
            events,
            relationships_out,
            relationships_in,
            artifact_info,
            has_notes,
            task_id,
            is_archived,
            full,
        )


# ---------------------------------------------------------------------------
# Show helpers
# ---------------------------------------------------------------------------


def _read_events(lattice_dir: Path, task_id: str, is_archived: bool) -> list[dict]:
    """Read all events for a task from the JSONL log."""
    if is_archived:
        event_path = lattice_dir / "archive" / "events" / f"{task_id}.jsonl"
    else:
        event_path = lattice_dir / "events" / f"{task_id}.jsonl"

    events: list[dict] = []
    if event_path.exists():
        for line in event_path.read_text().splitlines():
            line = line.strip()
            if line:
                try:
                    events.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
    return events


def _enrich_relationships(lattice_dir: Path, snapshot: dict) -> list[dict]:
    """Enrich relationships with target task titles (best effort)."""
    relationships: list[dict] = []
    for rel in snapshot.get("relationships_out", []):
        enriched = dict(rel)
        target_id = rel.get("target_task_id", "")
        # Try to read target task title
        target_snap = read_snapshot(lattice_dir, target_id)
        if target_snap is None:
            # Check archive
            archive_path = lattice_dir / "archive" / "tasks" / f"{target_id}.json"
            if archive_path.exists():
                try:
                    target_snap = json.loads(archive_path.read_text())
                except (json.JSONDecodeError, OSError):
                    pass
        if target_snap is not None:
            enriched["target_title"] = target_snap.get("title")
        relationships.append(enriched)
    return relationships


def _find_incoming_relationships(lattice_dir: Path, task_id: str) -> list[dict]:
    """Find all tasks that have outgoing relationships pointing at *task_id*.

    Scans active and archived snapshots. Returns a list of dicts with
    ``source_task_id``, ``source_title``, ``type``, and ``note``.
    """
    incoming: list[dict] = []

    for directory in [lattice_dir / "tasks", lattice_dir / "archive" / "tasks"]:
        if not directory.is_dir():
            continue
        for snap_file in directory.glob("*.json"):
            if snap_file.stem == task_id:
                continue  # skip self
            try:
                snap = json.loads(snap_file.read_text())
            except (json.JSONDecodeError, OSError):
                continue
            for rel in snap.get("relationships_out", []):
                if rel.get("target_task_id") == task_id:
                    incoming.append(
                        {
                            "source_task_id": snap.get("id", snap_file.stem),
                            "source_title": snap.get("title"),
                            "type": rel.get("type"),
                            "note": rel.get("note"),
                        }
                    )

    return incoming


def _read_artifact_info(lattice_dir: Path, snapshot: dict) -> list[dict]:
    """Read artifact metadata for each artifact ref (best effort)."""
    artifacts: list[dict] = []
    for art_id in snapshot.get("artifact_refs", []):
        meta_path = lattice_dir / "artifacts" / "meta" / f"{art_id}.json"
        info: dict = {"id": art_id}
        if meta_path.exists():
            try:
                meta = json.loads(meta_path.read_text())
                info["title"] = meta.get("title")
                info["type"] = meta.get("type")
            except (json.JSONDecodeError, OSError):
                pass
        artifacts.append(info)
    return artifacts


def _print_compact_show(snapshot: dict, is_archived: bool) -> None:
    """Print compact human-readable show output."""
    task_id = snapshot.get("id", "?")
    short_id = snapshot.get("short_id")
    title = snapshot.get("title", "?")
    status = snapshot.get("status", "?")
    priority = snapshot.get("priority", "?")
    task_type = snapshot.get("type", "?")
    assigned_to = snapshot.get("assigned_to") or "unassigned"

    archived_note = "  [ARCHIVED]" if is_archived else ""
    header = f"{short_id} ({task_id})" if short_id else task_id
    click.echo(f'{header}  "{title}"{archived_note}')
    click.echo(f"Status: {status}  Priority: {priority}  Type: {task_type}")
    click.echo(f"Assigned: {assigned_to}")


def _print_human_show(
    snapshot: dict,
    events: list[dict],
    relationships: list[dict],
    relationships_in: list[dict],
    artifact_info: list[dict],
    has_notes: bool,
    task_id: str,
    is_archived: bool,
    full: bool,
) -> None:
    """Print full human-readable show output."""
    short_id = snapshot.get("short_id")
    title = snapshot.get("title", "?")
    status = snapshot.get("status", "?")
    priority = snapshot.get("priority", "?")
    task_type = snapshot.get("type", "?")
    assigned_to = snapshot.get("assigned_to") or "unassigned"
    created_by = snapshot.get("created_by", "?")
    created_at = snapshot.get("created_at", "?")
    updated_at = snapshot.get("updated_at", "?")
    description = snapshot.get("description")

    archived_note = "  [ARCHIVED]" if is_archived else ""
    header = f"{short_id} ({task_id})" if short_id else task_id
    click.echo(f'{header}  "{title}"{archived_note}')
    click.echo(f"Status: {status}  Priority: {priority}  Type: {task_type}")
    click.echo(f"Assigned: {assigned_to}  Created by: {created_by}")
    click.echo(f"Created: {created_at}  Updated: {updated_at}")

    if description:
        click.echo("")
        click.echo("Description:")
        for line in description.splitlines():
            click.echo(f"  {line}")

    if relationships:
        click.echo("")
        click.echo("Relationships (outgoing):")
        for rel in relationships:
            rel_type = rel.get("type", "?")
            target_id = rel.get("target_task_id", "?")
            target_title = rel.get("target_title")
            if target_title:
                click.echo(f'  {rel_type} -> {target_id} "{target_title}"')
            else:
                click.echo(f"  {rel_type} -> {target_id}")

    if relationships_in:
        click.echo("")
        click.echo("Relationships (incoming):")
        for rel in relationships_in:
            rel_type = rel.get("type", "?")
            source_id = rel.get("source_task_id", "?")
            source_title = rel.get("source_title")
            if source_title:
                click.echo(f'  {source_id} "{source_title}" --[{rel_type}]--> this')
            else:
                click.echo(f"  {source_id} --[{rel_type}]--> this")

    if artifact_info:
        click.echo("")
        click.echo("Artifacts:")
        for art in artifact_info:
            art_id = art.get("id", "?")
            art_title = art.get("title")
            art_type = art.get("type")
            if art_title and art_type:
                click.echo(f'  {art_id} "{art_title}" ({art_type})')
            elif art_title:
                click.echo(f'  {art_id} "{art_title}"')
            else:
                click.echo(f"  {art_id}")

    if has_notes:
        click.echo("")
        click.echo(f"Notes: notes/{task_id}.md")

    if events:
        click.echo("")
        click.echo("Events (latest first):")
        # Show events in reverse chronological order
        for ev in reversed(events):
            ts = ev.get("ts", "?")
            etype = ev.get("type", "?")
            ev_actor = ev.get("actor", "?")
            summary = _event_summary(ev, full)
            click.echo(f"  {ts}  {etype}  {summary}  by {ev_actor}")


def _event_summary(event: dict, full: bool) -> str:
    """Build a short summary string for an event in human output."""
    etype = event.get("type", "")
    data = event.get("data", {})

    if full:
        return json.dumps(data, sort_keys=True)

    if etype == "status_changed":
        return f"{data.get('from', '?')} -> {data.get('to', '?')}"
    elif etype == "assignment_changed":
        from_val = data.get("from") or "unassigned"
        return f"{from_val} -> {data.get('to', '?')}"
    elif etype == "field_updated":
        return f"{data.get('field', '?')}: {data.get('from', '?')} -> {data.get('to', '?')}"
    elif etype == "comment_added":
        body = data.get("body", "")
        if len(body) > 60:
            body = body[:57] + "..."
        return f'"{body}"'
    elif etype == "task_created":
        return ""
    elif etype == "task_short_id_assigned":
        return f"assigned {data.get('short_id', '?')}"
    elif etype == "relationship_added":
        return f"{data.get('type', '?')} -> {data.get('target_task_id', '?')}"
    elif etype == "relationship_removed":
        return f"{data.get('type', '?')} -x- {data.get('target_task_id', '?')}"
    elif etype == "artifact_attached":
        return f"artifact {data.get('artifact_id', '?')}"
    elif etype.startswith("x_"):
        if data:
            return json.dumps(data, sort_keys=True)
        return ""
    else:
        return ""
