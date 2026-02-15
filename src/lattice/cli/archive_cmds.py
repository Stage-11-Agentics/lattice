"""Archive commands: archive and unarchive."""

from __future__ import annotations

import shutil

import click

from lattice.cli.helpers import (
    common_options,
    load_project_config,
    output_error,
    output_result,
    read_snapshot,
    require_root,
    resolve_task_id,
    validate_actor_or_exit,
)
from lattice.cli.main import cli
from lattice.core.events import create_event, serialize_event
from lattice.core.tasks import apply_event_to_snapshot, serialize_snapshot
from lattice.storage.fs import atomic_write, jsonl_append
from lattice.storage.hooks import execute_hooks
from lattice.storage.locks import multi_lock


@cli.command()
@click.argument("task_id")
@common_options
def archive(
    task_id: str,
    actor: str,
    model: str | None,
    session: str | None,
    output_json: bool,
    quiet: bool,
) -> None:
    """Archive a completed task."""
    is_json = output_json

    lattice_dir = require_root(is_json)
    config = load_project_config(lattice_dir)
    validate_actor_or_exit(actor, is_json)

    task_id = resolve_task_id(lattice_dir, task_id, is_json)

    # Check if task exists in active tasks
    snapshot = read_snapshot(lattice_dir, task_id)

    if snapshot is None:
        # Check if already archived
        archive_path = lattice_dir / "archive" / "tasks" / f"{task_id}.json"
        if archive_path.exists():
            output_error(
                f"Task {task_id} is already archived.",
                "CONFLICT",
                is_json,
            )
        output_error(f"Task {task_id} not found.", "NOT_FOUND", is_json)

    # Build event
    event = create_event(
        type="task_archived",
        task_id=task_id,
        actor=actor,
        data={},
        model=model,
        session=session,
    )

    # Apply event to snapshot
    updated_snapshot = apply_event_to_snapshot(snapshot, event)

    # Custom write path: event-first, then move files, all under lock
    locks_dir = lattice_dir / "locks"
    lock_keys = sorted([f"events_{task_id}", f"tasks_{task_id}", "events__lifecycle"])

    with multi_lock(locks_dir, lock_keys):
        # 1. Append event to per-task log
        event_path = lattice_dir / "events" / f"{task_id}.jsonl"
        jsonl_append(event_path, serialize_event(event))

        # 2. Append event to lifecycle log
        lifecycle_path = lattice_dir / "events" / "_lifecycle.jsonl"
        jsonl_append(lifecycle_path, serialize_event(event))

        # 3. Write updated snapshot directly to archive (avoids redundant write+move)
        atomic_write(
            lattice_dir / "archive" / "tasks" / f"{task_id}.json",
            serialize_snapshot(updated_snapshot),
        )

        # 4. Remove active snapshot
        snapshot_path = lattice_dir / "tasks" / f"{task_id}.json"
        if snapshot_path.exists():
            snapshot_path.unlink()

        # 5. Move event log to archive
        shutil.move(
            str(event_path),
            str(lattice_dir / "archive" / "events" / f"{task_id}.jsonl"),
        )

        # 6. Move notes if they exist
        notes_path = lattice_dir / "notes" / f"{task_id}.md"
        if notes_path.exists():
            shutil.move(
                str(notes_path),
                str(lattice_dir / "archive" / "notes" / f"{task_id}.md"),
            )

    # Fire hooks after locks released
    execute_hooks(config, lattice_dir, task_id, event)

    output_result(
        data=event,
        human_message=f"Archived task {task_id}",
        quiet_value=task_id,
        is_json=is_json,
        is_quiet=quiet,
    )


@cli.command()
@click.argument("task_id")
@common_options
def unarchive(
    task_id: str,
    actor: str,
    model: str | None,
    session: str | None,
    output_json: bool,
    quiet: bool,
) -> None:
    """Restore an archived task to active status."""
    is_json = output_json

    lattice_dir = require_root(is_json)
    config = load_project_config(lattice_dir)
    validate_actor_or_exit(actor, is_json)

    task_id = resolve_task_id(lattice_dir, task_id, is_json, allow_archived=True)

    # Check if task exists in active tasks (already active = CONFLICT)
    active_path = lattice_dir / "tasks" / f"{task_id}.json"
    if active_path.exists():
        output_error(
            f"Task {task_id} is already active.",
            "CONFLICT",
            is_json,
        )

    # Read snapshot from archive
    archive_snapshot_path = lattice_dir / "archive" / "tasks" / f"{task_id}.json"
    if not archive_snapshot_path.exists():
        output_error(f"Task {task_id} not found in archive.", "NOT_FOUND", is_json)

    import json

    snapshot = json.loads(archive_snapshot_path.read_text())

    # Build event
    event = create_event(
        type="task_unarchived",
        task_id=task_id,
        actor=actor,
        data={},
        model=model,
        session=session,
    )

    # Apply event to snapshot (updates bookkeeping only)
    updated_snapshot = apply_event_to_snapshot(snapshot, event)

    # Custom write path: event-first, then move files, all under lock
    locks_dir = lattice_dir / "locks"
    lock_keys = sorted([f"events_{task_id}", f"tasks_{task_id}", "events__lifecycle"])

    with multi_lock(locks_dir, lock_keys):
        # 1. Append event to per-task log (still in archive)
        archive_event_path = lattice_dir / "archive" / "events" / f"{task_id}.jsonl"
        jsonl_append(archive_event_path, serialize_event(event))

        # 2. Append event to lifecycle log
        lifecycle_path = lattice_dir / "events" / "_lifecycle.jsonl"
        jsonl_append(lifecycle_path, serialize_event(event))

        # 3. Move event log from archive to active
        shutil.move(
            str(archive_event_path),
            str(lattice_dir / "events" / f"{task_id}.jsonl"),
        )

        # 4. Write updated snapshot to active tasks
        atomic_write(
            lattice_dir / "tasks" / f"{task_id}.json",
            serialize_snapshot(updated_snapshot),
        )

        # 5. Remove archived snapshot
        archive_snapshot_path.unlink()

        # 6. Move notes back if they exist
        archive_notes_path = lattice_dir / "archive" / "notes" / f"{task_id}.md"
        if archive_notes_path.exists():
            shutil.move(
                str(archive_notes_path),
                str(lattice_dir / "notes" / f"{task_id}.md"),
            )

    # Fire hooks after locks released
    execute_hooks(config, lattice_dir, task_id, event)

    output_result(
        data=event,
        human_message=f"Unarchived task {task_id}",
        quiet_value=task_id,
        is_json=is_json,
        is_quiet=quiet,
    )
