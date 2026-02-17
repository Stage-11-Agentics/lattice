"""Task write commands: create, update, status, assign, comment, react."""

from __future__ import annotations

import json

import click

from lattice.cli.helpers import (
    common_options,
    load_project_config,
    output_error,
    output_result,
    read_snapshot_or_exit,
    require_root,
    resolve_task_id,
    validate_actor_or_exit,
    write_task_event,
)
from lattice.storage.operations import scaffold_plan
from lattice.cli.main import cli
from lattice.core.comments import (
    materialize_comments,
    validate_comment_body,
    validate_comment_for_delete,
    validate_comment_for_edit,
    validate_comment_for_react,
    validate_comment_for_reply,
    validate_emoji,
)
from lattice.core.config import (
    VALID_COMPLEXITIES,
    VALID_PRIORITIES,
    VALID_URGENCIES,
    get_valid_transitions,
    validate_completion_policy,
    validate_status,
    validate_task_type,
    validate_transition,
)
from lattice.core.events import create_event, utc_now
from lattice.core.ids import generate_task_id, validate_actor, validate_id
from lattice.core.tasks import apply_event_to_snapshot
from lattice.storage.readers import read_task_events
from lattice.storage.short_ids import allocate_short_id


# ---------------------------------------------------------------------------
# Idempotency comparison fields for create
# ---------------------------------------------------------------------------

_CREATE_COMPARE_FIELDS = (
    "title",
    "type",
    "priority",
    "urgency",
    "complexity",
    "status",
    "description",
    "tags",
    "assigned_to",
)


# ---------------------------------------------------------------------------
# lattice create
# ---------------------------------------------------------------------------


@cli.command()
@click.argument("title")
@click.option("--type", "task_type", default=None, help="Task type (task, epic, bug, spike, chore).")
@click.option("--priority", default=None, help="Priority (critical, high, medium, low).")
@click.option("--urgency", default=None, help="Urgency (immediate, high, normal, low).")
@click.option("--complexity", default=None, help="Agentic complexity (low, medium, high).")
@click.option("--status", default=None, help="Initial status (default: backlog).")
@click.option("--description", default=None, help="Task description.")
@click.option("--tags", default=None, help="Comma-separated tags.")
@click.option("--assigned-to", default=None, help="Assignee (actor format).")
@click.option("--id", "task_id", default=None, help="Caller-supplied task ID.")
@common_options
def create(
    title: str,
    task_type: str | None,
    priority: str | None,
    urgency: str | None,
    complexity: str | None,
    status: str | None,
    description: str | None,
    tags: str | None,
    assigned_to: str | None,
    task_id: str | None,
    actor: str,
    model: str | None,
    session: str | None,
    output_json: bool,
    quiet: bool,
    triggered_by: str | None,
    on_behalf_of: str | None,
    provenance_reason: str | None,
) -> None:
    """Create a new task."""
    is_json = output_json

    lattice_dir = require_root(is_json)
    config = load_project_config(lattice_dir)

    validate_actor_or_exit(actor, is_json)
    if on_behalf_of is not None:
        validate_actor_or_exit(on_behalf_of, is_json)

    # Apply defaults
    if status is None:
        status = config.get("default_status", "backlog")
    if priority is None:
        priority = config.get("default_priority", "medium")
    if task_type is None:
        task_type = "task"

    # Validate inputs
    if not validate_status(config, status):
        valid = ", ".join(config.get("workflow", {}).get("statuses", []))
        output_error(
            f"Invalid status: '{status}'. Valid statuses: {valid}.", "VALIDATION_ERROR", is_json
        )
    if not validate_task_type(config, task_type):
        valid = ", ".join(config.get("task_types", []))
        output_error(
            f"Invalid task type: '{task_type}'. Valid types: {valid}.", "VALIDATION_ERROR", is_json
        )
    if priority not in VALID_PRIORITIES:
        valid = ", ".join(VALID_PRIORITIES)
        output_error(
            f"Invalid priority: '{priority}'. Valid priorities: {valid}.",
            "VALIDATION_ERROR",
            is_json,
        )
    if urgency is not None and urgency not in VALID_URGENCIES:
        valid = ", ".join(VALID_URGENCIES)
        output_error(
            f"Invalid urgency: '{urgency}'. Valid urgencies: {valid}.", "VALIDATION_ERROR", is_json
        )
    if complexity is not None and complexity not in VALID_COMPLEXITIES:
        valid = ", ".join(VALID_COMPLEXITIES)
        output_error(
            f"Invalid complexity: '{complexity}'. Valid complexities: {valid}.",
            "VALIDATION_ERROR",
            is_json,
        )
    if assigned_to is not None and not validate_actor(assigned_to):
        output_error(f"Invalid assigned-to format: '{assigned_to}'.", "INVALID_ACTOR", is_json)

    # Parse tags
    tag_list = [t.strip() for t in tags.split(",") if t.strip()] if tags else []

    # Generate or validate task ID
    if task_id is not None:
        if not validate_id(task_id, "task"):
            output_error(f"Invalid task ID format: '{task_id}'.", "INVALID_ID", is_json)
        # Idempotency check
        existing_path = lattice_dir / "tasks" / f"{task_id}.json"
        if existing_path.exists():
            existing = json.loads(existing_path.read_text())
            new_data = {
                "title": title,
                "type": task_type,
                "priority": priority,
                "urgency": urgency,
                "complexity": complexity,
                "status": status,
                "description": description,
                "tags": tag_list,
                "assigned_to": assigned_to,
            }
            existing_data = {field: existing.get(field) for field in _CREATE_COMPARE_FIELDS}
            # Normalize: snapshot stores tags as list, default is None
            if existing_data.get("tags") is None:
                existing_data["tags"] = []
            if new_data == existing_data:
                output_result(
                    data=existing,
                    human_message=f"Task {task_id} already exists (idempotent).",
                    quiet_value=task_id,
                    is_json=is_json,
                    is_quiet=quiet,
                )
                return
            else:
                output_error(
                    f"Conflict: task {task_id} exists with different data.",
                    "CONFLICT",
                    is_json,
                )
    else:
        task_id = generate_task_id()

    # Allocate short ID if project code is configured
    project_code = config.get("project_code")
    subproject_code = config.get("subproject_code")
    short_id: str | None = None
    if project_code:
        prefix = f"{project_code}-{subproject_code}" if subproject_code else project_code
        short_id, _idx = allocate_short_id(lattice_dir, prefix, task_ulid=task_id)

    # Build event data
    event_data: dict = {
        "title": title,
        "status": status,
        "type": task_type,
        "priority": priority,
    }
    if urgency is not None:
        event_data["urgency"] = urgency
    if complexity is not None:
        event_data["complexity"] = complexity
    if description is not None:
        event_data["description"] = description
    if tag_list:
        event_data["tags"] = tag_list
    if assigned_to is not None:
        event_data["assigned_to"] = assigned_to
    if short_id is not None:
        event_data["short_id"] = short_id

    # Build event and snapshot
    event = create_event(
        type="task_created",
        task_id=task_id,
        actor=actor,
        data=event_data,
        model=model,
        session=session,
        triggered_by=triggered_by,
        on_behalf_of=on_behalf_of,
        reason=provenance_reason,
    )
    snapshot = apply_event_to_snapshot(None, event)

    # Write (event-first, then snapshot, under lock)
    write_task_event(lattice_dir, task_id, [event], snapshot, config)

    # Scaffold plan file (notes are created lazily, not on task creation)
    scaffold_plan(lattice_dir, task_id, title, short_id, description)

    # Output: prefer short_id when available
    display_id = short_id if short_id else task_id
    output_result(
        data=snapshot,
        human_message=(
            f'Created task {display_id} ({task_id}) "{title}"\n'
            f"  status: {status}  priority: {priority}  type: {task_type}"
            if short_id
            else f'Created task {task_id} "{title}"\n'
            f"  status: {status}  priority: {priority}  type: {task_type}"
        ),
        quiet_value=display_id,
        is_json=is_json,
        is_quiet=quiet,
    )


# ---------------------------------------------------------------------------
# Updatable field names for `lattice update`
# ---------------------------------------------------------------------------

_UPDATABLE_FIELDS = frozenset(
    {"title", "description", "priority", "urgency", "complexity", "type", "tags"}
)

_REDIRECT_FIELDS = {
    "status": "Use 'lattice status' to change status.",
    "assigned_to": "Use 'lattice assign' to change assignment.",
}


# ---------------------------------------------------------------------------
# lattice update
# ---------------------------------------------------------------------------


@cli.command()
@click.argument("task_id")
@click.argument("pairs", nargs=-1)
@common_options
def update(
    task_id: str,
    pairs: tuple[str, ...],
    actor: str,
    model: str | None,
    session: str | None,
    output_json: bool,
    quiet: bool,
    triggered_by: str | None,
    on_behalf_of: str | None,
    provenance_reason: str | None,
) -> None:
    """Update task fields.  Pass field=value pairs."""
    is_json = output_json

    lattice_dir = require_root(is_json)
    config = load_project_config(lattice_dir)
    validate_actor_or_exit(actor, is_json)
    if on_behalf_of is not None:
        validate_actor_or_exit(on_behalf_of, is_json)

    task_id = resolve_task_id(lattice_dir, task_id, is_json)

    snapshot = read_snapshot_or_exit(lattice_dir, task_id, is_json)

    if not pairs:
        output_error("No field=value pairs provided.", "VALIDATION_ERROR", is_json)

    # Parse field=value pairs — split on first '=' only
    parsed: list[tuple[str, str]] = []
    for pair in pairs:
        if "=" not in pair:
            output_error(
                f"Invalid field=value pair: '{pair}'. Expected format: field=value.",
                "VALIDATION_ERROR",
                is_json,
            )
        field, value = pair.split("=", 1)
        parsed.append((field, value))

    # Validate and build events
    shared_ts = utc_now()
    events: list[dict] = []

    for field, value in parsed:
        # Reject status and assigned_to with helpful messages
        if field in _REDIRECT_FIELDS:
            output_error(_REDIRECT_FIELDS[field], "VALIDATION_ERROR", is_json)

        # Handle custom_fields.* dot notation
        if field.startswith("custom_fields."):
            key = field[len("custom_fields.") :]
            if not key:
                output_error(
                    "Invalid custom field: 'custom_fields.' requires a key name.",
                    "VALIDATION_ERROR",
                    is_json,
                )
            old_value = (snapshot.get("custom_fields") or {}).get(key)
            new_value = value
            if old_value == new_value:
                continue
            events.append(
                create_event(
                    type="field_updated",
                    task_id=task_id,
                    actor=actor,
                    data={"field": field, "from": old_value, "to": new_value},
                    ts=shared_ts,
                    model=model,
                    session=session,
                    triggered_by=triggered_by,
                    on_behalf_of=on_behalf_of,
                    reason=provenance_reason,
                )
            )
            continue

        if field not in _UPDATABLE_FIELDS:
            valid = ", ".join(sorted(_UPDATABLE_FIELDS))
            output_error(
                f"Unknown or non-updatable field: '{field}'. "
                f"Updatable fields: {valid}. Use custom_fields.<key> for custom data.",
                "VALIDATION_ERROR",
                is_json,
            )

        # Validate enum fields
        if field == "priority" and value not in VALID_PRIORITIES:
            valid = ", ".join(VALID_PRIORITIES)
            output_error(
                f"Invalid priority: '{value}'. Valid priorities: {valid}.",
                "VALIDATION_ERROR",
                is_json,
            )
        if field == "urgency" and value not in VALID_URGENCIES:
            valid = ", ".join(VALID_URGENCIES)
            output_error(
                f"Invalid urgency: '{value}'. Valid urgencies: {valid}.",
                "VALIDATION_ERROR",
                is_json,
            )
        if field == "complexity" and value not in VALID_COMPLEXITIES:
            valid = ", ".join(VALID_COMPLEXITIES)
            output_error(
                f"Invalid complexity: '{value}'. Valid complexities: {valid}.",
                "VALIDATION_ERROR",
                is_json,
            )
        if field == "type" and not validate_task_type(config, value):
            valid = ", ".join(config.get("task_types", []))
            output_error(
                f"Invalid task type: '{value}'. Valid types: {valid}.", "VALIDATION_ERROR", is_json
            )

        # Get old value and compute new value
        if field == "tags":
            new_value = [t.strip() for t in value.split(",") if t.strip()]
            old_value = snapshot.get("tags") or []
        else:
            new_value = value
            old_value = snapshot.get(field)

        if old_value == new_value:
            continue

        events.append(
            create_event(
                type="field_updated",
                task_id=task_id,
                actor=actor,
                data={"field": field, "from": old_value, "to": new_value},
                ts=shared_ts,
                model=model,
                session=session,
                triggered_by=triggered_by,
                on_behalf_of=on_behalf_of,
                reason=provenance_reason,
            )
        )

    if not events:
        if is_json:
            click.echo(
                json.dumps(
                    {"ok": True, "data": {"message": "No changes"}}, sort_keys=True, indent=2
                )
                + "\n"
            )
        elif quiet:
            click.echo("ok")
        else:
            click.echo("No changes")
        return

    # Apply events to snapshot incrementally
    updated_snapshot = snapshot
    for event in events:
        updated_snapshot = apply_event_to_snapshot(updated_snapshot, event)

    # Write all events + updated snapshot
    write_task_event(lattice_dir, task_id, events, updated_snapshot, config)

    field_names = [e["data"]["field"] for e in events]
    output_result(
        data=updated_snapshot,
        human_message=f"Updated task {task_id}: {', '.join(field_names)}",
        quiet_value="ok",
        is_json=is_json,
        is_quiet=quiet,
    )


# ---------------------------------------------------------------------------
# lattice status
# ---------------------------------------------------------------------------


@cli.command("status")
@click.argument("task_id")
@click.argument("new_status")
@click.option("--force", is_flag=True, help="Force an invalid transition.")
@common_options
def status_cmd(
    task_id: str,
    new_status: str,
    force: bool,
    actor: str,
    model: str | None,
    session: str | None,
    output_json: bool,
    quiet: bool,
    triggered_by: str | None,
    on_behalf_of: str | None,
    provenance_reason: str | None,
) -> None:
    """Change a task's status."""
    is_json = output_json

    lattice_dir = require_root(is_json)
    config = load_project_config(lattice_dir)
    validate_actor_or_exit(actor, is_json)
    if on_behalf_of is not None:
        validate_actor_or_exit(on_behalf_of, is_json)

    task_id = resolve_task_id(lattice_dir, task_id, is_json)

    snapshot = read_snapshot_or_exit(lattice_dir, task_id, is_json)

    # Epics cannot have their status set directly — it's derived from subtasks
    if snapshot.get("type") == "epic":
        output_error(
            "Cannot set status on epics. Epic status is derived from subtask completion.",
            "EPIC_STATUS_REJECTED",
            is_json,
        )

    current_status = snapshot["status"]

    # Resolve display name to slug (e.g. "on it" → "in_progress")
    from lattice.core.config import resolve_status_input

    new_status = resolve_status_input(config, new_status)

    # Validate new_status is a known status
    if not validate_status(config, new_status):
        valid = ", ".join(config.get("workflow", {}).get("statuses", []))
        output_error(
            f"Invalid status: '{new_status}'. Valid statuses: {valid}.",
            "VALIDATION_ERROR",
            is_json,
        )

    # Already at the target status
    if current_status == new_status:
        if is_json:
            click.echo(
                json.dumps(
                    {"ok": True, "data": {"message": f"Already at status {new_status}"}},
                    sort_keys=True,
                    indent=2,
                )
                + "\n"
            )
        elif quiet:
            click.echo("ok")
        else:
            click.echo(f"Already at status {new_status}")
        return

    # Check transition validity
    if not validate_transition(config, current_status, new_status):
        if not force:
            valid_targets = get_valid_transitions(config, current_status)
            valid_list = ", ".join(valid_targets) if valid_targets else "(none)"
            msg = (
                f"Invalid transition from {current_status} to {new_status}. "
                f"Valid transitions from {current_status}: {valid_list}. "
                "Use --force --reason to override."
            )
            if is_json:
                from lattice.cli.helpers import json_envelope, json_error_obj

                error_obj = json_error_obj("INVALID_TRANSITION", msg)
                error_obj["current_status"] = current_status
                error_obj["requested_status"] = new_status
                error_obj["valid_transitions"] = valid_targets
                click.echo(json_envelope(False, error=error_obj))
                raise SystemExit(1)
            else:
                click.echo(f"Error: {msg}", err=True)
                raise SystemExit(1)
        if not provenance_reason:
            output_error(
                "--reason is required with --force.",
                "VALIDATION_ERROR",
                is_json,
            )

    # Check completion policies (evidence gating)
    policy_ok, policy_failures = validate_completion_policy(config, snapshot, new_status)
    if not policy_ok:
        if not force:
            failure_msg = "; ".join(policy_failures)
            output_error(
                f"Completion policy not satisfied: {failure_msg}. "
                "Use --force --reason to override.",
                "COMPLETION_BLOCKED",
                is_json,
            )
        if not provenance_reason:
            output_error(
                "--reason is required with --force.",
                "VALIDATION_ERROR",
                is_json,
            )

    event_data: dict = {
        "from": current_status,
        "to": new_status,
    }
    if force:
        event_data["force"] = True
        event_data["reason"] = provenance_reason

    event = create_event(
        type="status_changed",
        task_id=task_id,
        actor=actor,
        data=event_data,
        model=model,
        session=session,
        triggered_by=triggered_by,
        on_behalf_of=on_behalf_of,
        reason=provenance_reason,
    )
    updated_snapshot = apply_event_to_snapshot(snapshot, event)
    write_task_event(lattice_dir, task_id, [event], updated_snapshot, config)

    display_id = updated_snapshot.get("short_id") or task_id
    output_result(
        data=updated_snapshot,
        human_message=f"Status: {current_status} -> {new_status} ({display_id})",
        quiet_value="ok",
        is_json=is_json,
        is_quiet=quiet,
    )


# ---------------------------------------------------------------------------
# lattice assign
# ---------------------------------------------------------------------------


_UNASSIGN_SENTINELS = frozenset({"none", "unassigned", "-"})


@cli.command()
@click.argument("task_id")
@click.argument("actor_id")
@common_options
def assign(
    task_id: str,
    actor_id: str,
    actor: str,
    model: str | None,
    session: str | None,
    output_json: bool,
    quiet: bool,
    triggered_by: str | None,
    on_behalf_of: str | None,
    provenance_reason: str | None,
) -> None:
    """Assign a task to an actor. Use 'none', 'unassigned', or '-' to unassign."""
    is_json = output_json

    lattice_dir = require_root(is_json)
    config = load_project_config(lattice_dir)
    validate_actor_or_exit(actor, is_json)
    if on_behalf_of is not None:
        validate_actor_or_exit(on_behalf_of, is_json)

    task_id = resolve_task_id(lattice_dir, task_id, is_json)

    # Check for unassignment sentinel values
    is_unassign = actor_id.lower() in _UNASSIGN_SENTINELS
    target_actor: str | None = None if is_unassign else actor_id

    # Validate assignee actor format (skip for unassignment)
    if not is_unassign and not validate_actor(actor_id):
        output_error(
            f"Invalid actor format: '{actor_id}'. "
            "Expected prefix:identifier (e.g., human:atin, agent:claude). "
            "Use 'none', 'unassigned', or '-' to unassign.",
            "INVALID_ACTOR",
            is_json,
        )

    snapshot = read_snapshot_or_exit(lattice_dir, task_id, is_json)
    current_assigned = snapshot.get("assigned_to")

    if current_assigned == target_actor:
        if is_unassign:
            label = "Already unassigned"
        else:
            label = f"Already assigned to {target_actor}"
        if is_json:
            click.echo(
                json.dumps(
                    {"ok": True, "data": {"message": label}},
                    sort_keys=True,
                    indent=2,
                )
                + "\n"
            )
        elif quiet:
            click.echo("ok")
        else:
            click.echo(label)
        return

    event = create_event(
        type="assignment_changed",
        task_id=task_id,
        actor=actor,
        data={"from": current_assigned, "to": target_actor},
        model=model,
        session=session,
        triggered_by=triggered_by,
        on_behalf_of=on_behalf_of,
        reason=provenance_reason,
    )
    updated_snapshot = apply_event_to_snapshot(snapshot, event)
    write_task_event(lattice_dir, task_id, [event], updated_snapshot, config)

    from_label = current_assigned or "unassigned"
    to_label = target_actor or "unassigned"
    output_result(
        data=updated_snapshot,
        human_message=f"Assigned: {from_label} -> {to_label}",
        quiet_value="ok",
        is_json=is_json,
        is_quiet=quiet,
    )


# ---------------------------------------------------------------------------
# lattice comment
# ---------------------------------------------------------------------------


@cli.command()
@click.argument("task_id")
@click.argument("text")
@click.option("--reply-to", default=None, help="Event ID of the comment to reply to.")
@common_options
def comment(
    task_id: str,
    text: str,
    reply_to: str | None,
    actor: str,
    model: str | None,
    session: str | None,
    output_json: bool,
    quiet: bool,
    triggered_by: str | None,
    on_behalf_of: str | None,
    provenance_reason: str | None,
) -> None:
    """Add a comment to a task."""
    is_json = output_json

    lattice_dir = require_root(is_json)
    config = load_project_config(lattice_dir)
    validate_actor_or_exit(actor, is_json)
    if on_behalf_of is not None:
        validate_actor_or_exit(on_behalf_of, is_json)

    task_id = resolve_task_id(lattice_dir, task_id, is_json)

    snapshot = read_snapshot_or_exit(lattice_dir, task_id, is_json)

    # Validate reply-to if provided
    if reply_to is not None:
        events = read_task_events(lattice_dir, task_id)
        try:
            validate_comment_for_reply(events, reply_to)
        except ValueError as exc:
            output_error(str(exc), "VALIDATION_ERROR", is_json)

    # Validate and normalize body
    try:
        text = validate_comment_body(text)
    except ValueError as exc:
        output_error(str(exc), "VALIDATION_ERROR", is_json)

    event_data: dict = {"body": text}
    if reply_to is not None:
        event_data["parent_id"] = reply_to

    event = create_event(
        type="comment_added",
        task_id=task_id,
        actor=actor,
        data=event_data,
        model=model,
        session=session,
        triggered_by=triggered_by,
        on_behalf_of=on_behalf_of,
        reason=provenance_reason,
    )
    updated_snapshot = apply_event_to_snapshot(snapshot, event)
    write_task_event(lattice_dir, task_id, [event], updated_snapshot, config)

    msg = f"Reply added to {task_id}" if reply_to else f"Comment added to {task_id}"
    output_result(
        data=updated_snapshot,
        human_message=msg,
        quiet_value="ok",
        is_json=is_json,
        is_quiet=quiet,
    )


# ---------------------------------------------------------------------------
# lattice comment-edit
# ---------------------------------------------------------------------------


@cli.command("comment-edit")
@click.argument("task_id")
@click.argument("comment_id")
@click.argument("new_text")
@common_options
def comment_edit(
    task_id: str,
    comment_id: str,
    new_text: str,
    actor: str,
    model: str | None,
    session: str | None,
    output_json: bool,
    quiet: bool,
    triggered_by: str | None,
    on_behalf_of: str | None,
    provenance_reason: str | None,
) -> None:
    """Edit an existing comment on a task."""
    is_json = output_json

    lattice_dir = require_root(is_json)
    config = load_project_config(lattice_dir)
    validate_actor_or_exit(actor, is_json)
    if on_behalf_of is not None:
        validate_actor_or_exit(on_behalf_of, is_json)

    task_id = resolve_task_id(lattice_dir, task_id, is_json)

    snapshot = read_snapshot_or_exit(lattice_dir, task_id, is_json)

    # Validate and normalize new body
    try:
        new_text = validate_comment_body(new_text)
    except ValueError as exc:
        output_error(str(exc), "VALIDATION_ERROR", is_json)

    events = read_task_events(lattice_dir, task_id)
    try:
        previous_body = validate_comment_for_edit(events, comment_id)
    except ValueError as exc:
        output_error(str(exc), "VALIDATION_ERROR", is_json)

    event = create_event(
        type="comment_edited",
        task_id=task_id,
        actor=actor,
        data={
            "comment_id": comment_id,
            "body": new_text,
            "previous_body": previous_body,
        },
        model=model,
        session=session,
        triggered_by=triggered_by,
        on_behalf_of=on_behalf_of,
        reason=provenance_reason,
    )
    updated_snapshot = apply_event_to_snapshot(snapshot, event)
    write_task_event(lattice_dir, task_id, [event], updated_snapshot, config)

    output_result(
        data=updated_snapshot,
        human_message=f"Comment {comment_id} edited on {task_id}",
        quiet_value="ok",
        is_json=is_json,
        is_quiet=quiet,
    )


# ---------------------------------------------------------------------------
# lattice comment-delete
# ---------------------------------------------------------------------------


@cli.command("comment-delete")
@click.argument("task_id")
@click.argument("comment_id")
@common_options
def comment_delete(
    task_id: str,
    comment_id: str,
    actor: str,
    model: str | None,
    session: str | None,
    output_json: bool,
    quiet: bool,
    triggered_by: str | None,
    on_behalf_of: str | None,
    provenance_reason: str | None,
) -> None:
    """Delete a comment from a task."""
    is_json = output_json

    lattice_dir = require_root(is_json)
    config = load_project_config(lattice_dir)
    validate_actor_or_exit(actor, is_json)
    if on_behalf_of is not None:
        validate_actor_or_exit(on_behalf_of, is_json)

    task_id = resolve_task_id(lattice_dir, task_id, is_json)

    snapshot = read_snapshot_or_exit(lattice_dir, task_id, is_json)

    events = read_task_events(lattice_dir, task_id)
    try:
        validate_comment_for_delete(events, comment_id)
    except ValueError as exc:
        output_error(str(exc), "VALIDATION_ERROR", is_json)

    event = create_event(
        type="comment_deleted",
        task_id=task_id,
        actor=actor,
        data={"comment_id": comment_id},
        model=model,
        session=session,
        triggered_by=triggered_by,
        on_behalf_of=on_behalf_of,
        reason=provenance_reason,
    )
    updated_snapshot = apply_event_to_snapshot(snapshot, event)
    write_task_event(lattice_dir, task_id, [event], updated_snapshot, config)

    output_result(
        data=updated_snapshot,
        human_message=f"Comment {comment_id} deleted from {task_id}",
        quiet_value="ok",
        is_json=is_json,
        is_quiet=quiet,
    )


# ---------------------------------------------------------------------------
# lattice react
# ---------------------------------------------------------------------------


@cli.command()
@click.argument("task_id")
@click.argument("comment_id")
@click.argument("emoji")
@common_options
def react(
    task_id: str,
    comment_id: str,
    emoji: str,
    actor: str,
    model: str | None,
    session: str | None,
    output_json: bool,
    quiet: bool,
    triggered_by: str | None,
    on_behalf_of: str | None,
    provenance_reason: str | None,
) -> None:
    """Add a reaction to a comment."""
    is_json = output_json

    lattice_dir = require_root(is_json)
    config = load_project_config(lattice_dir)
    validate_actor_or_exit(actor, is_json)
    if on_behalf_of is not None:
        validate_actor_or_exit(on_behalf_of, is_json)

    task_id = resolve_task_id(lattice_dir, task_id, is_json)

    snapshot = read_snapshot_or_exit(lattice_dir, task_id, is_json)

    if not validate_emoji(emoji):
        output_error(
            f"Invalid emoji: '{emoji}'. Must be 1-50 alphanumeric, underscore, or hyphen characters.",
            "VALIDATION_ERROR",
            is_json,
        )

    events = read_task_events(lattice_dir, task_id)
    try:
        validate_comment_for_react(events, comment_id)
    except ValueError as exc:
        output_error(str(exc), "VALIDATION_ERROR", is_json)

    # Idempotency: check if actor already has this reaction
    comments = materialize_comments(events)
    _all_comments = _flatten_comments(comments)
    for c in _all_comments:
        if c["id"] == comment_id:
            if actor in c.get("reactions", {}).get(emoji, []):
                output_result(
                    data=snapshot,
                    human_message=f"Reaction :{emoji}: already exists on {comment_id} (idempotent).",
                    quiet_value="ok",
                    is_json=is_json,
                    is_quiet=quiet,
                )
                return
            break

    event = create_event(
        type="reaction_added",
        task_id=task_id,
        actor=actor,
        data={"comment_id": comment_id, "emoji": emoji},
        model=model,
        session=session,
        triggered_by=triggered_by,
        on_behalf_of=on_behalf_of,
        reason=provenance_reason,
    )
    updated_snapshot = apply_event_to_snapshot(snapshot, event)
    write_task_event(lattice_dir, task_id, [event], updated_snapshot, config)

    output_result(
        data=updated_snapshot,
        human_message=f"Reaction :{emoji}: added to {comment_id}",
        quiet_value="ok",
        is_json=is_json,
        is_quiet=quiet,
    )


# ---------------------------------------------------------------------------
# lattice unreact
# ---------------------------------------------------------------------------


@cli.command()
@click.argument("task_id")
@click.argument("comment_id")
@click.argument("emoji")
@common_options
def unreact(
    task_id: str,
    comment_id: str,
    emoji: str,
    actor: str,
    model: str | None,
    session: str | None,
    output_json: bool,
    quiet: bool,
    triggered_by: str | None,
    on_behalf_of: str | None,
    provenance_reason: str | None,
) -> None:
    """Remove a reaction from a comment."""
    is_json = output_json

    lattice_dir = require_root(is_json)
    config = load_project_config(lattice_dir)
    validate_actor_or_exit(actor, is_json)
    if on_behalf_of is not None:
        validate_actor_or_exit(on_behalf_of, is_json)

    task_id = resolve_task_id(lattice_dir, task_id, is_json)

    snapshot = read_snapshot_or_exit(lattice_dir, task_id, is_json)

    if not validate_emoji(emoji):
        output_error(
            f"Invalid emoji: '{emoji}'. Must be 1-50 alphanumeric, underscore, or hyphen characters.",
            "VALIDATION_ERROR",
            is_json,
        )

    events = read_task_events(lattice_dir, task_id)

    # Validate the target comment exists and is not deleted
    try:
        validate_comment_for_react(events, comment_id)
    except ValueError as exc:
        output_error(str(exc), "VALIDATION_ERROR", is_json)

    # Check the reaction exists for this actor
    comments = materialize_comments(events)
    _all_comments = _flatten_comments(comments)
    found = False
    for c in _all_comments:
        if c["id"] == comment_id:
            if actor in c.get("reactions", {}).get(emoji, []):
                found = True
            break

    if not found:
        output_error(
            f"Reaction :{emoji}: by {actor} not found on comment {comment_id}.",
            "NOT_FOUND",
            is_json,
        )

    event = create_event(
        type="reaction_removed",
        task_id=task_id,
        actor=actor,
        data={"comment_id": comment_id, "emoji": emoji},
        model=model,
        session=session,
        triggered_by=triggered_by,
        on_behalf_of=on_behalf_of,
        reason=provenance_reason,
    )
    updated_snapshot = apply_event_to_snapshot(snapshot, event)
    write_task_event(lattice_dir, task_id, [event], updated_snapshot, config)

    output_result(
        data=updated_snapshot,
        human_message=f"Reaction :{emoji}: removed from {comment_id}",
        quiet_value="ok",
        is_json=is_json,
        is_quiet=quiet,
    )


def _flatten_comments(comments: list[dict]) -> list[dict]:
    """Flatten a threaded comment list into a flat list (top-level + replies)."""
    flat: list[dict] = []
    for c in comments:
        flat.append(c)
        flat.extend(c.get("replies", []))
    return flat
