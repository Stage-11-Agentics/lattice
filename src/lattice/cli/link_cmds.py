"""Relationship commands: link, unlink."""

from __future__ import annotations

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
from lattice.cli.main import cli
from lattice.core.events import create_event
from lattice.core.relationships import RELATIONSHIP_TYPES, validate_relationship_type
from lattice.core.tasks import apply_event_to_snapshot


# ---------------------------------------------------------------------------
# lattice link
# ---------------------------------------------------------------------------


@cli.command()
@click.argument("task_id")
@click.argument("rel_type", metavar="TYPE")
@click.argument("target_task_id")
@click.option("--note", default=None, help="Optional note for the relationship.")
@common_options
def link(
    task_id: str,
    rel_type: str,
    target_task_id: str,
    note: str | None,
    actor: str,
    model: str | None,
    session: str | None,
    output_json: bool,
    quiet: bool,
) -> None:
    """Create a relationship between two tasks."""
    is_json = output_json

    lattice_dir = require_root(is_json)
    config = load_project_config(lattice_dir)
    validate_actor_or_exit(actor, is_json)

    task_id = resolve_task_id(lattice_dir, task_id, is_json)
    target_task_id = resolve_task_id(lattice_dir, target_task_id, is_json)

    # Validate relationship type
    if not validate_relationship_type(rel_type):
        sorted_types = ", ".join(sorted(RELATIONSHIP_TYPES))
        output_error(
            f"Invalid relationship type: '{rel_type}'. Valid types: {sorted_types}.",
            "VALIDATION_ERROR",
            is_json,
        )

    # Reject self-links
    if task_id == target_task_id:
        output_error(
            "Cannot create a relationship from a task to itself.",
            "VALIDATION_ERROR",
            is_json,
        )

    # Validate both tasks exist
    snapshot = read_snapshot_or_exit(lattice_dir, task_id, is_json)
    # Check target exists (we don't need the snapshot, just existence)
    target_path = lattice_dir / "tasks" / f"{target_task_id}.json"
    if not target_path.exists():
        output_error(
            f"Target task {target_task_id} not found.",
            "NOT_FOUND",
            is_json,
        )

    # Reject duplicates: same type + same target already in relationships_out
    for rel in snapshot.get("relationships_out", []):
        if rel["type"] == rel_type and rel["target_task_id"] == target_task_id:
            output_error(
                f"Duplicate: {rel_type} relationship to {target_task_id} already exists.",
                "CONFLICT",
                is_json,
            )

    # Build event
    event_data: dict = {
        "type": rel_type,
        "target_task_id": target_task_id,
    }
    if note is not None:
        event_data["note"] = note

    event = create_event(
        type="relationship_added",
        task_id=task_id,
        actor=actor,
        data=event_data,
        model=model,
        session=session,
    )
    updated_snapshot = apply_event_to_snapshot(snapshot, event)

    # Write (event-first, then snapshot, under lock)
    write_task_event(lattice_dir, task_id, [event], updated_snapshot, config)

    # Output
    output_result(
        data=updated_snapshot,
        human_message=(f"Linked {task_id} --[{rel_type}]--> {target_task_id}"),
        quiet_value=task_id,
        is_json=is_json,
        is_quiet=quiet,
    )


# ---------------------------------------------------------------------------
# lattice unlink
# ---------------------------------------------------------------------------


@cli.command()
@click.argument("task_id")
@click.argument("rel_type", metavar="TYPE")
@click.argument("target_task_id")
@common_options
def unlink(
    task_id: str,
    rel_type: str,
    target_task_id: str,
    actor: str,
    model: str | None,
    session: str | None,
    output_json: bool,
    quiet: bool,
) -> None:
    """Remove a relationship between two tasks."""
    is_json = output_json

    lattice_dir = require_root(is_json)
    config = load_project_config(lattice_dir)
    validate_actor_or_exit(actor, is_json)

    task_id = resolve_task_id(lattice_dir, task_id, is_json)
    target_task_id = resolve_task_id(lattice_dir, target_task_id, is_json)

    # Validate relationship type
    if not validate_relationship_type(rel_type):
        sorted_types = ", ".join(sorted(RELATIONSHIP_TYPES))
        output_error(
            f"Invalid relationship type: '{rel_type}'. Valid types: {sorted_types}.",
            "VALIDATION_ERROR",
            is_json,
        )

    # Validate source task exists and load snapshot
    snapshot = read_snapshot_or_exit(lattice_dir, task_id, is_json)

    # Validate the relationship exists in snapshot's relationships_out
    found = False
    for rel in snapshot.get("relationships_out", []):
        if rel["type"] == rel_type and rel["target_task_id"] == target_task_id:
            found = True
            break

    if not found:
        output_error(
            f"No {rel_type} relationship to {target_task_id}.",
            "NOT_FOUND",
            is_json,
        )

    # Build event
    event_data: dict = {
        "type": rel_type,
        "target_task_id": target_task_id,
    }

    event = create_event(
        type="relationship_removed",
        task_id=task_id,
        actor=actor,
        data=event_data,
        model=model,
        session=session,
    )
    updated_snapshot = apply_event_to_snapshot(snapshot, event)

    # Write (event-first, then snapshot, under lock)
    write_task_event(lattice_dir, task_id, [event], updated_snapshot, config)

    # Output
    output_result(
        data=updated_snapshot,
        human_message=(f"Unlinked {task_id} --[{rel_type}]--> {target_task_id}"),
        quiet_value=task_id,
        is_json=is_json,
        is_quiet=quiet,
    )
