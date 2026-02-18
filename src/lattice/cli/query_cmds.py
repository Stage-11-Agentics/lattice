"""Query and display commands: comments, event, list, next, show."""

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
from lattice.core.comments import materialize_comments
from lattice.core.config import get_valid_transitions, validate_status
from lattice.core.events import (
    BUILTIN_EVENT_TYPES,
    create_event,
    get_actor_display,
    validate_custom_event_type,
)
from lattice.core.ids import extract_short_ids, validate_actor, validate_id
from lattice.core.next import compute_claim_transitions, select_next
from lattice.core.stats import load_all_snapshots
from lattice.core.tasks import (
    apply_event_to_snapshot,
    compact_snapshot,
    is_backward_status_transition,
)
from lattice.storage.locks import multi_lock
from lattice.storage.readers import read_task_events


# ---------------------------------------------------------------------------
# lattice comments
# ---------------------------------------------------------------------------


@cli.command("comments")
@click.argument("task_id")
@click.option("--json", "output_json", is_flag=True, help="Output structured JSON.")
@click.option("--quiet", is_flag=True, help="Print one comment ID per line (top-level only).")
def comments_cmd(
    task_id: str,
    output_json: bool,
    quiet: bool,
) -> None:
    """Display threaded comments for a task."""
    is_json = output_json

    lattice_dir = require_root(is_json)

    task_id = resolve_task_id(lattice_dir, task_id, is_json, allow_archived=True)

    # Read task snapshot for context header
    snapshot = read_snapshot(lattice_dir, task_id)
    if snapshot is None:
        # Check archive
        archive_path = lattice_dir / "archive" / "tasks" / f"{task_id}.json"
        if archive_path.exists():
            try:
                snapshot = json.loads(archive_path.read_text())
            except (json.JSONDecodeError, OSError):
                pass

    # Try active first, then archive
    events = read_task_events(lattice_dir, task_id, is_archived=False)
    if not events:
        events = read_task_events(lattice_dir, task_id, is_archived=True)

    comments = materialize_comments(events)

    if is_json:
        result_obj: dict = {"ok": True, "data": comments}
        if snapshot:
            result_obj["task_context"] = {
                "id": snapshot.get("short_id") or task_id,
                "title": snapshot.get("title"),
                "status": snapshot.get("status"),
            }
        click.echo(json.dumps(result_obj, sort_keys=True, indent=2) + "\n")
    elif quiet:
        for comment in comments:
            click.echo(comment["id"])
    else:
        # Print task context header
        if snapshot:
            display_id = snapshot.get("short_id") or task_id
            title = snapshot.get("title", "?")
            status = snapshot.get("status", "?")
            click.echo(f'{display_id} "{title}" ({status})')
            click.echo("---")
        if not comments:
            click.echo("No comments.")
            return
        for i, comment in enumerate(comments):
            _print_comment(comment, indent=0)
            if i < len(comments) - 1:
                click.echo("")


def _print_comment(comment: dict, indent: int) -> None:
    """Render a single comment with optional indentation for threading."""
    prefix = "  " * indent
    comment_id = comment["id"]
    author = comment.get("author", "?")
    created_at = comment.get("created_at", "?")

    badges = ""
    if comment.get("deleted"):
        badges += " [deleted]"
    if comment.get("edited"):
        badges += " [edited]"

    click.echo(f"{prefix}[{comment_id}] {author} ({created_at}){badges}")

    if comment.get("deleted"):
        # Don't show body for deleted comments
        pass
    else:
        body = comment.get("body", "")
        for line in body.splitlines():
            click.echo(f"{prefix}  {line}")

        # Reactions
        reactions = comment.get("reactions", {})
        for emoji, actors in reactions.items():
            click.echo(f"{prefix}  :{emoji}: {', '.join(actors)}")

    # Replies
    for j, reply in enumerate(comment.get("replies", [])):
        click.echo("")
        _print_comment(reply, indent=indent + 1)


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
    triggered_by: str | None,
    on_behalf_of: str | None,
    provenance_reason: str | None,
) -> None:
    """Record a custom event on a task.

    Custom event types must start with 'x_' (e.g., x_deployment_started).
    Built-in types like status_changed or task_created are reserved.
    """
    is_json = output_json

    lattice_dir = require_root(is_json)
    config = load_project_config(lattice_dir)
    actor = validate_actor_or_exit(actor, is_json)
    if on_behalf_of is not None:
        validate_actor_or_exit(on_behalf_of, is_json)

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
        triggered_by=triggered_by,
        on_behalf_of=on_behalf_of,
        reason=provenance_reason,
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
@click.option(
    "--priority",
    default=None,
    help="Filter by priority (critical, high, medium, low).",
)
@click.option("--include-archived", is_flag=True, help="Include archived tasks.")
@click.option("--compact", is_flag=True, help="Compact JSON output.")
@click.option("--json", "output_json", is_flag=True, help="Output structured JSON.")
@click.option("--quiet", is_flag=True, help="Print one task ID per line.")
def list_cmd(
    status: str | None,
    assigned: str | None,
    tag: str | None,
    task_type: str | None,
    priority: str | None,
    include_archived: bool,
    compact: bool,
    output_json: bool,
    quiet: bool,
) -> None:
    """List tasks with optional filters."""
    is_json = output_json

    lattice_dir = require_root(is_json)
    config = load_project_config(lattice_dir)

    # Resolve display name to slug for --status filter
    from lattice.core.config import resolve_status_input

    if status is not None:
        status = resolve_status_input(config, status)

    # Validate --status filter value against configured statuses
    status_warning: str | None = None
    if status is not None and not validate_status(config, status):
        valid = ", ".join(config.get("workflow", {}).get("statuses", []))
        status_warning = (
            f"'{status}' is not a configured status. Valid statuses: {valid}."
        )

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

    # Include archived tasks if requested
    if include_archived:
        archive_dir = lattice_dir / "archive" / "tasks"
        if archive_dir.is_dir():
            for task_file in sorted(archive_dir.glob("*.json")):
                try:
                    snap = json.loads(task_file.read_text())
                except (json.JSONDecodeError, OSError):
                    continue
                snap["_archived"] = True
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
        if priority is not None and snap.get("priority") != priority:
            continue
        filtered.append(snap)

    # Sort by task ID (ULID = chronological order)
    filtered.sort(key=lambda s: s.get("id", ""))

    # Output
    if is_json:
        if compact:
            data = [compact_snapshot(s) for s in filtered]
            for i, snap in enumerate(filtered):
                if snap.get("_archived"):
                    data[i]["archived"] = True
        else:
            data = []
            for snap in filtered:
                item = dict(snap)
                is_archived = item.pop("_archived", False)
                if is_archived:
                    item["archived"] = True
                data.append(item)
        result: dict = {"ok": True, "data": data}
        if status_warning:
            result["warnings"] = [status_warning]
        click.echo(json.dumps(result, sort_keys=True, indent=2) + "\n")
    elif quiet:
        if status_warning:
            click.echo(f"Warning: {status_warning}", err=True)
        for snap in filtered:
            short_id = snap.get("short_id")
            click.echo(short_id if short_id else snap.get("id", ""))
    else:
        if status_warning:
            click.echo(f"Warning: {status_warning}", err=True)
        # Human output: compact one-line-per-task table
        from lattice.core.config import get_display_name

        for snap in filtered:
            short_id = snap.get("short_id")
            display_id = short_id if short_id else snap.get("id", "?")
            s = snap.get("status", "?")
            s_display = get_display_name(config, s)
            p = snap.get("priority", "?")
            t = snap.get("type", "?")
            title = snap.get("title", "?")
            assigned_to = snap.get("assigned_to") or "unassigned"
            prefix = ">>> " if s == "needs_human" else ""
            archived_marker = " [A]" if snap.get("_archived") else ""
            click.echo(f'{prefix}{display_id}  {s_display}  {p}  {t}  "{title}"  {assigned_to}{archived_marker}')


# ---------------------------------------------------------------------------
# lattice next
# ---------------------------------------------------------------------------


@cli.command("next")
@click.option(
    "--actor", default=None, help="Who is asking (filters by assignment, required for --claim)."
)
@click.option(
    "--name", "session_name", default=None,
    help="Session name (e.g., Argus-3). Resolves to full identity.",
)
@click.option(
    "--status",
    "status_csv",
    default=None,
    help="Comma-separated statuses to consider (default: backlog,planned).",
)
@click.option("--claim", is_flag=True, help="Atomically assign + move to in_progress.")
@click.option("--json", "output_json", is_flag=True, help="Output structured JSON.")
@click.option("--quiet", is_flag=True, help="Print only the task ID.")
def next_cmd(
    actor: str | None,
    session_name: str | None,
    status_csv: str | None,
    claim: bool,
    output_json: bool,
    quiet: bool,
) -> None:
    """Pick the highest-priority task to work on next.

    Returns the top task from the ready pool (backlog/planned by default).
    If --actor/--name is specified, resumes in-progress work first.
    Use --claim to atomically assign and start the task.
    """
    is_json = output_json

    lattice_dir = require_root(is_json)
    config = load_project_config(lattice_dir)

    # Resolve --name to structured actor
    resolved_actor: str | dict | None = None
    if session_name is not None:
        from lattice.storage.sessions import resolve_session, touch_session

        session_data = resolve_session(lattice_dir, session_name)
        if session_data is None:
            output_error(
                f"No active session named '{session_name}'. "
                "Start one with 'lattice session start'.",
                "SESSION_NOT_FOUND",
                is_json,
            )
        touch_session(lattice_dir, session_name)
        resolved_actor = {
            "name": session_data["name"],
            "base_name": session_data["base_name"],
            "serial": session_data["serial"],
            "session": session_data["session"],
            "model": session_data["model"],
        }
        if session_data.get("framework"):
            resolved_actor["framework"] = session_data["framework"]
        if session_data.get("agent_type"):
            resolved_actor["agent_type"] = session_data["agent_type"]
    elif actor is not None:
        resolved_actor = actor

    # Validate --claim requires identity
    if claim and resolved_actor is None:
        output_error(
            "--claim requires --actor or --name.",
            "VALIDATION_ERROR",
            is_json,
        )

    # Validate legacy actor format if provided
    if actor and not session_name and not validate_actor(actor):
        output_error(
            f"Invalid actor format: '{actor}'. "
            "Expected prefix:identifier (e.g., human:atin, agent:claude).",
            "INVALID_ACTOR",
            is_json,
        )

    # Parse --status override
    ready_statuses: frozenset[str] | None = None
    if status_csv is not None:
        ready_statuses = frozenset(s.strip() for s in status_csv.split(",") if s.strip())

    # Load all active snapshots
    active, _archived = load_all_snapshots(lattice_dir)

    # Select next task
    selected = select_next(active, actor=resolved_actor, ready_statuses=ready_statuses)

    if selected is None:
        if is_json:
            # json_envelope skips data=None, so build manually
            click.echo(json.dumps({"ok": True, "data": None}, sort_keys=True, indent=2) + "\n")
        elif quiet:
            pass  # no output
        else:
            click.echo("No tasks available.")
        return

    task_id = selected["id"]

    # --claim: atomically assign + move to in_progress with valid transitions
    if claim:
        locks_dir = lattice_dir / "locks"
        lock_keys = sorted([f"events_{task_id}", f"tasks_{task_id}"])

        with multi_lock(locks_dir, lock_keys):
            # Re-read snapshot under lock to prevent TOCTOU race
            snapshot = read_snapshot(lattice_dir, task_id)
            if snapshot is None:
                output_error(f"Task {task_id} not found.", "NOT_FOUND", is_json)

            events = []

            # Assignment event (if not already assigned to this actor)
            from lattice.core.next import _actors_match

            current_assigned = snapshot.get("assigned_to")
            if not _actors_match(current_assigned, resolved_actor):
                assign_event = create_event(
                    type="assignment_changed",
                    task_id=task_id,
                    actor=resolved_actor,
                    data={"from": current_assigned, "to": resolved_actor},
                )
                events.append(assign_event)
                snapshot = apply_event_to_snapshot(snapshot, assign_event)

            # Status transitions — compute valid path to in_progress
            current_status = snapshot.get("status")
            if current_status != "in_progress":
                transitions = config.get("workflow", {}).get("transitions", {})
                path = compute_claim_transitions(current_status, "in_progress", transitions)
                if path is None:
                    output_error(
                        f"No valid transition path from {current_status} to in_progress.",
                        "INVALID_TRANSITION",
                        is_json,
                    )
                # Emit a status_changed event for each step in the path
                prev_status = current_status
                for next_status in path:
                    status_event = create_event(
                        type="status_changed",
                        task_id=task_id,
                        actor=resolved_actor,
                        data={"from": prev_status, "to": next_status},
                    )
                    events.append(status_event)
                    snapshot = apply_event_to_snapshot(snapshot, status_event)
                    prev_status = next_status

            if events:
                # Write directly under the already-held lock (bypass write_task_event
                # which would try to acquire its own locks)
                from lattice.core.events import serialize_event
                from lattice.core.tasks import serialize_snapshot
                from lattice.storage.fs import atomic_write, jsonl_append

                event_path = lattice_dir / "events" / f"{task_id}.jsonl"
                for event in events:
                    jsonl_append(event_path, serialize_event(event))

                snapshot_path = lattice_dir / "tasks" / f"{task_id}.json"
                atomic_write(snapshot_path, serialize_snapshot(snapshot))

            selected = snapshot

        # Fire hooks after lock release
        if events and config:
            from lattice.storage.hooks import execute_hooks

            for event in events:
                execute_hooks(config, lattice_dir, task_id, event)

    display_id = selected.get("short_id") or task_id
    result_data = selected
    if is_json:
        result_data = dict(selected)
        result_data["plan_content"] = _read_plan_content_for_next(lattice_dir, task_id)
    output_result(
        data=result_data,
        human_message=(
            f"{display_id}  {selected.get('status', '?')}  "
            f'{selected.get("priority", "?")}  "{selected.get("title", "?")}"'
        ),
        quiet_value=display_id,
        is_json=is_json,
        is_quiet=quiet,
    )


def _read_plan_content_for_next(lattice_dir: Path, task_id: str) -> str | None:
    """Return plan markdown for *task_id* if present and non-scaffold; else None."""
    plan_path = lattice_dir / "plans" / f"{task_id}.md"
    if not plan_path.exists():
        return None
    try:
        content = plan_path.read_text()
    except OSError:
        return None
    if _is_scaffold_plan_content(content):
        return None
    stripped = content.strip()
    if not stripped:
        return None
    return content


def _is_scaffold_plan_content(content: str) -> bool:
    """Return True when plan content still matches the default scaffold placeholders."""
    if not content.strip():
        return True
    return (
        "<!-- Implementation approach, design decisions, open questions. -->" in content
        and "<!-- What must be true for this task to be done? -->" in content
    )


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

    # Load config for valid_transitions
    config = load_project_config(lattice_dir)
    current_status = snapshot.get("status", "")
    valid_transitions = get_valid_transitions(config, current_status)

    # Compact mode: just show compact fields, no events/relationships/artifacts
    if compact:
        if is_json:
            data = compact_snapshot(snapshot)
            data["valid_transitions"] = valid_transitions
            if is_archived:
                data["archived"] = True
            click.echo(json_envelope(True, data=data))
        else:
            _print_compact_show(snapshot, is_archived, valid_transitions)
        return

    # Read event log
    events = _read_events(lattice_dir, task_id, is_archived)
    status_rank = _status_rank_from_config(config)
    backward_count, latest_reopen = _scan_backward_status_transitions(events, status_rank)
    reopened_count = snapshot.get("reopened_count", 0)
    if not isinstance(reopened_count, int):
        reopened_count = 0
    # Legacy snapshots may not have this field; derive from events as fallback.
    if reopened_count == 0 and backward_count > 0:
        reopened_count = backward_count
    snapshot["reopened_count"] = reopened_count

    reopened_warning: str | None = None
    if latest_reopen is not None:
        reopened_warning = (
            f"Previously completed, reset on {latest_reopen['date']} "
            f"by {latest_reopen['actor']}"
        )

    # Check for notes and plan files
    if is_archived:
        notes_path = lattice_dir / "archive" / "notes" / f"{task_id}.md"
        plan_path = lattice_dir / "archive" / "plans" / f"{task_id}.md"
    else:
        notes_path = lattice_dir / "notes" / f"{task_id}.md"
        plan_path = lattice_dir / "plans" / f"{task_id}.md"
    has_notes = notes_path.exists()
    has_plan = plan_path.exists()

    # Read outgoing relationship target titles (best effort)
    relationships_out = _enrich_relationships(lattice_dir, snapshot)

    # Derive incoming relationships by scanning all task snapshots
    relationships_in = _find_incoming_relationships(lattice_dir, task_id)

    # Read artifact metadata (best effort)
    artifact_info = _read_artifact_info(lattice_dir, snapshot)

    # Auto-detect branch links from git branches matching the task's short code
    short_id = snapshot.get("short_id")
    explicit_branches = [bl["branch"] for bl in snapshot.get("branch_links", [])]
    all_branches = _get_all_git_branches(lattice_dir)
    auto_branches = _auto_detect_branch_links(short_id, explicit_branches, all_branches)
    auto_commits = _auto_detect_commits(short_id, lattice_dir)

    if is_json:
        data: dict = dict(snapshot)
        data["events"] = events
        data["valid_transitions"] = valid_transitions
        if is_archived:
            data["archived"] = True
        if has_plan:
            data["plan_path"] = f"plans/{task_id}.md"
        if has_notes:
            data["notes_path"] = f"notes/{task_id}.md"
        data["relationships_enriched"] = relationships_out
        data["relationships_in"] = relationships_in
        data["artifact_info"] = artifact_info
        if auto_branches:
            data["auto_detected_branches"] = auto_branches
        if auto_commits:
            data["auto_detected_commits"] = auto_commits
        if reopened_warning:
            data["reopened_warning"] = reopened_warning
            data["latest_reopen"] = latest_reopen
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
            has_plan,
            has_notes,
            task_id,
            is_archived,
            full,
            valid_transitions,
            auto_branches,
            auto_commits,
            config=config,
            reopened_warning=reopened_warning,
        )


# ---------------------------------------------------------------------------
# Show helpers
# ---------------------------------------------------------------------------


def _get_current_git_branch(lattice_dir: Path) -> str | None:
    """Return the current git branch name, or None if unavailable.

    Uses ``git rev-parse --abbrev-ref HEAD`` from the repo root
    (parent of ``.lattice/``).  Silently returns None on any error.
    """
    import shutil
    import subprocess

    if not shutil.which("git"):
        return None
    repo_root = lattice_dir.parent
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--abbrev-ref", "HEAD"],
            cwd=str(repo_root),
            capture_output=True,
            text=True,
            timeout=5,
        )
        if result.returncode == 0:
            name = result.stdout.strip()
            if name and name != "HEAD":
                return name
    except (subprocess.TimeoutExpired, OSError):
        pass
    return None


def _get_all_git_branches(lattice_dir: Path) -> list[str]:
    """Return all local git branch names, or empty list if unavailable."""
    import shutil
    import subprocess

    if not shutil.which("git"):
        return []
    repo_root = lattice_dir.parent
    try:
        result = subprocess.run(
            ["git", "for-each-ref", "--format=%(refname:short)", "refs/heads/"],
            cwd=str(repo_root),
            capture_output=True,
            text=True,
            timeout=5,
        )
        if result.returncode == 0:
            return [b.strip() for b in result.stdout.strip().splitlines() if b.strip()]
    except (subprocess.TimeoutExpired, OSError):
        pass
    return []


def _auto_detect_branch_links(
    short_id: str | None,
    explicit_branches: list[str],
    git_branches: list[str],
) -> list[str]:
    """Find git branches that contain the task's short code but aren't explicitly linked.

    Matching is case-insensitive.  Returns branch names that auto-match.
    """
    if not short_id or not git_branches:
        return []

    explicit_set = {b.lower() for b in explicit_branches}
    matches: list[str] = []
    upper_short = short_id.upper()

    for branch in git_branches:
        if branch.lower() in explicit_set:
            continue
        found_ids = extract_short_ids(branch)
        if upper_short in found_ids:
            matches.append(branch)

    return matches


def _auto_detect_commits(short_id: str | None, lattice_dir: Path) -> list[dict[str, str]]:
    """Find commits whose message contains *short_id*.

    Uses ``git log --grep=<short_id>`` and returns commit summaries in reverse
    chronological order. Returns an empty list when git is unavailable, the
    directory is not in a git repo, or any git error occurs.
    """
    import shutil
    import subprocess

    if not short_id or not shutil.which("git"):
        return []

    repo_root = lattice_dir.parent
    fmt = "%h%x1f%ad%x1f%s"

    try:
        result = subprocess.run(
            [
                "git",
                "log",
                f"--grep={short_id}",
                "--fixed-strings",
                "--regexp-ignore-case",
                "--date=short",
                f"--pretty=format:{fmt}",
            ],
            cwd=str(repo_root),
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (subprocess.TimeoutExpired, OSError):
        return []

    if result.returncode != 0:
        return []

    commits: list[dict[str, str]] = []
    for line in result.stdout.splitlines():
        sha, date, subject = (line.split("\x1f", 2) + ["", "", ""])[:3]
        if not sha:
            continue
        commits.append({"sha": sha, "date": date, "subject": subject})
    return commits


def _read_events(lattice_dir: Path, task_id: str, is_archived: bool) -> list[dict]:
    """Read all events for a task from the JSONL log."""
    return read_task_events(lattice_dir, task_id, is_archived=is_archived)


def _status_rank_from_config(config: dict) -> dict[str, int]:
    """Return ``{status: rank}`` using configured workflow order."""
    statuses = config.get("workflow", {}).get("statuses", [])
    if not isinstance(statuses, list):
        return {}
    return {status: idx for idx, status in enumerate(statuses) if isinstance(status, str)}


def _scan_backward_status_transitions(
    events: list[dict],
    status_rank: dict[str, int],
) -> tuple[int, dict | None]:
    """Return backward transition count and latest backward transition metadata."""
    count = 0
    latest: dict | None = None
    for event in events:
        if event.get("type") != "status_changed":
            continue
        data = event.get("data", {})
        from_status = data.get("from")
        to_status = data.get("to")
        if not is_backward_status_transition(from_status, to_status, status_rank):
            continue
        count += 1
        ts = event.get("ts")
        if isinstance(ts, str):
            date = ts.split("T", 1)[0]
        else:
            date = "?"
        latest = {
            "ts": ts,
            "date": date,
            "actor": get_actor_display(event.get("actor", "?")),
            "from": from_status,
            "to": to_status,
        }
    return count, latest


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
    """Read artifact metadata for each artifact evidence ref (best effort).

    Reads from ``evidence_refs`` (source_type=="artifact") first, falls back
    to legacy ``artifact_refs`` for old snapshots.
    """
    artifacts: list[dict] = []
    refs = _get_artifact_evidence_refs(snapshot)
    for art_id, role in refs:
        meta_path = lattice_dir / "artifacts" / "meta" / f"{art_id}.json"
        info: dict = {"id": art_id, "role": role}
        if meta_path.exists():
            try:
                meta = json.loads(meta_path.read_text())
                info["title"] = meta.get("title")
                info["type"] = meta.get("type")
            except (json.JSONDecodeError, OSError):
                pass
        artifacts.append(info)
    return artifacts


def _get_artifact_evidence_refs(snapshot: dict) -> list[tuple[str, str | None]]:
    """Extract (artifact_id, role) pairs from evidence_refs or legacy artifact_refs."""
    evidence_refs = snapshot.get("evidence_refs")
    if evidence_refs is not None:
        return [
            (ref["id"], ref.get("role"))
            for ref in evidence_refs
            if ref.get("source_type") == "artifact"
        ]
    # Legacy fallback
    result = []
    for ref in snapshot.get("artifact_refs", []):
        if isinstance(ref, dict):
            result.append((ref["id"], ref.get("role")))
        else:
            result.append((ref, None))
    return result


def _print_compact_show(
    snapshot: dict, is_archived: bool, valid_transitions: list[str] | None = None
) -> None:
    """Print compact human-readable show output."""
    task_id = snapshot.get("id", "?")
    short_id = snapshot.get("short_id")
    title = snapshot.get("title", "?")
    status = snapshot.get("status", "?")
    priority = snapshot.get("priority", "?")
    task_type = snapshot.get("type", "?")
    raw_assigned = snapshot.get("assigned_to")
    assigned_to = get_actor_display(raw_assigned) if raw_assigned else "unassigned"

    archived_note = "  [ARCHIVED]" if is_archived else ""
    header = f"{short_id} ({task_id})" if short_id else task_id
    click.echo(f'{header}  "{title}"{archived_note}')
    next_str = ""
    if valid_transitions:
        next_str = f"\n  Next: {' | '.join(valid_transitions)}"
    click.echo(f"Status: {status}  Priority: {priority}  Type: {task_type}")
    click.echo(f"Assigned: {assigned_to}{next_str}")


def _print_human_show(
    snapshot: dict,
    events: list[dict],
    relationships: list[dict],
    relationships_in: list[dict],
    artifact_info: list[dict],
    has_plan: bool,
    has_notes: bool,
    task_id: str,
    is_archived: bool,
    full: bool,
    valid_transitions: list[str] | None = None,
    auto_detected_branches: list[str] | None = None,
    auto_detected_commits: list[dict[str, str]] | None = None,
    config: dict | None = None,
    reopened_warning: str | None = None,
) -> None:
    """Print full human-readable show output."""
    from lattice.core.config import get_display_name

    short_id = snapshot.get("short_id")
    title = snapshot.get("title", "?")
    status = snapshot.get("status", "?")
    status_display = get_display_name(config or {}, status)
    priority = snapshot.get("priority", "?")
    task_type = snapshot.get("type", "?")
    raw_assigned = snapshot.get("assigned_to")
    assigned_to = get_actor_display(raw_assigned) if raw_assigned else "unassigned"
    created_by = get_actor_display(snapshot.get("created_by", "?"))
    created_at = snapshot.get("created_at", "?")
    updated_at = snapshot.get("updated_at", "?")
    description = snapshot.get("description")

    archived_note = "  [ARCHIVED]" if is_archived else ""
    header = f"{short_id} ({task_id})" if short_id else task_id
    click.echo(f'{header}  "{title}"{archived_note}')
    click.echo(f"Status: {status_display}  Priority: {priority}  Type: {task_type}")
    if valid_transitions:
        display_transitions = [get_display_name(config or {}, t) for t in valid_transitions]
        click.echo(f"  Next: {' | '.join(display_transitions)}")
    comment_count = snapshot.get("comment_count", 0)
    click.echo(f"Assigned: {assigned_to}  Created by: {created_by}")
    click.echo(f"Created: {created_at}  Updated: {updated_at}")
    if reopened_warning:
        click.echo(f"Warning: {reopened_warning}")
    if comment_count:
        click.echo(f"Comments: {comment_count}")

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
            art_role = art.get("role")
            parts: list[str] = []
            if art_type:
                parts.append(art_type)
            if art_role:
                parts.append(f"role: {art_role}")
            suffix = f" ({', '.join(parts)})" if parts else ""
            if art_title:
                click.echo(f'  {art_id} "{art_title}"{suffix}')
            else:
                click.echo(f"  {art_id}{suffix}")

    # Review evidence summary — roles satisfied by comments or artifacts
    evidence_refs = snapshot.get("evidence_refs", [])
    role_sources: dict[str, list[str]] = {}
    for ref in evidence_refs:
        role = ref.get("role")
        if role:
            source_type = ref.get("source_type", "unknown")
            role_sources.setdefault(role, []).append(source_type)
    if role_sources:
        click.echo("")
        click.echo("Review evidence:")
        for role, sources in sorted(role_sources.items()):
            source_summary = ", ".join(f"{s} x{sources.count(s)}" if sources.count(s) > 1 else s for s in sorted(set(sources)))
            click.echo(f"  {role}: {source_summary}")

    branch_links = snapshot.get("branch_links", [])
    has_branch_section = branch_links or auto_detected_branches
    if has_branch_section:
        click.echo("")
        click.echo("Branch links:")
        for bl in branch_links:
            branch_name = bl.get("branch", "?")
            repo_name = bl.get("repo")
            linked_by = bl.get("linked_by", "?")
            if repo_name:
                click.echo(f"  {branch_name} (repo: {repo_name}) by {linked_by}")
            else:
                click.echo(f"  {branch_name} by {linked_by}")
        if auto_detected_branches:
            for branch in auto_detected_branches:
                click.echo(f"  {branch} (auto-detected)")

    if auto_detected_commits:
        click.echo("")
        click.echo("Commits:")
        for commit in auto_detected_commits:
            sha = commit.get("sha", "?")
            date = commit.get("date", "?")
            subject = commit.get("subject", "")
            click.echo(f"  {sha}  {date}  {subject}")

    if has_plan:
        click.echo("")
        click.echo(f"Plan: plans/{task_id}.md")

    if has_notes:
        if not has_plan:
            click.echo("")
        click.echo(f"Notes: notes/{task_id}.md")

    if events:
        click.echo("")
        click.echo("Events (latest first):")
        # Show events in reverse chronological order
        for ev in reversed(events):
            ts = ev.get("ts", "?")
            etype = ev.get("type", "?")
            ev_actor = get_actor_display(ev.get("actor", "?"))
            summary = _event_summary(ev, full)
            click.echo(f"  {ts}  {etype}  {summary}  by {ev_actor}")
            # Provenance line
            prov = ev.get("provenance")
            if prov:
                parts = []
                if "triggered_by" in prov:
                    parts.append(f"triggered by: {prov['triggered_by']}")
                if "on_behalf_of" in prov:
                    parts.append(f"on behalf of: {prov['on_behalf_of']}")
                if "reason" in prov:
                    parts.append(f"reason: {prov['reason']}")
                if parts:
                    click.echo(f"    {' | '.join(parts)}")


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
        role = data.get("role")
        role_tag = f" [role: {role}]" if role else ""
        return f'"{body}"{role_tag}'
    elif etype == "comment_edited":
        cid = data.get("comment_id", "?")
        return f"edited comment {cid[:20]}..."
    elif etype == "comment_deleted":
        cid = data.get("comment_id", "?")
        return f"deleted comment {cid[:20]}..."
    elif etype == "reaction_added":
        return f':{data.get("emoji", "?")}: on {data.get("comment_id", "?")[:20]}...'
    elif etype == "reaction_removed":
        return f'removed :{data.get("emoji", "?")}: from {data.get("comment_id", "?")[:20]}...'
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
    elif etype == "branch_linked":
        repo = data.get("repo")
        branch = data.get("branch", "?")
        return f"branch '{branch}'" + (f" (repo: {repo})" if repo else "")
    elif etype == "branch_unlinked":
        repo = data.get("repo")
        branch = data.get("branch", "?")
        return f"branch '{branch}' removed" + (f" (repo: {repo})" if repo else "")
    elif etype.startswith("x_"):
        if data:
            return json.dumps(data, sort_keys=True)
        return ""
    else:
        return ""


# ---------------------------------------------------------------------------
# lattice plan
# ---------------------------------------------------------------------------


@cli.command()
@click.argument("task_id")
@click.option("--json", "output_json", is_flag=True, help="Output as JSON.")
def plan(task_id: str, output_json: bool) -> None:
    """Show or open the plan file for a task.

    Prints the plan file path. If the plan file doesn't exist, reports that.
    """
    is_json = output_json
    lattice_dir = require_root(is_json)
    task_id = resolve_task_id(lattice_dir, task_id, is_json)

    # Check active then archive
    plan_path = lattice_dir / "plans" / f"{task_id}.md"
    is_archived = False
    if not plan_path.is_file():
        plan_path = lattice_dir / "archive" / "plans" / f"{task_id}.md"
        is_archived = True
    if not plan_path.is_file():
        output_error(f"No plan file found for task {task_id}.", "NOT_FOUND", is_json)

    if is_json:
        data = {
            "task_id": task_id,
            "plan_path": str(plan_path),
            "archived": is_archived,
            "content": plan_path.read_text(encoding="utf-8"),
        }
        click.echo(json_envelope(True, data=data))
    else:
        # Print content to stdout
        click.echo(plan_path.read_text(encoding="utf-8"))
