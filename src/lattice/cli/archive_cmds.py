"""Archive commands: archive and unarchive."""

from __future__ import annotations

import shutil
import sys
from pathlib import Path

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


def _parse_task_ids(raw_ids: tuple[str, ...]) -> list[str]:
    """Expand comma-separated and space-separated task IDs into a flat list."""
    result: list[str] = []
    for raw in raw_ids:
        for part in raw.split(","):
            stripped = part.strip()
            if stripped:
                result.append(stripped)
    return result


def _archive_one(
    task_id: str,
    *,
    lattice_dir: Path,
    config: dict,
    actor: str,
    model: str | None,
    session: str | None,
    triggered_by: str | None,
    on_behalf_of: str | None,
    provenance_reason: str | None,
    is_json: bool,
) -> dict | str:
    """Archive a single task. Returns the event dict on success or an error string on failure."""
    snapshot = read_snapshot(lattice_dir, task_id)

    if snapshot is None:
        archive_path = lattice_dir / "archive" / "tasks" / f"{task_id}.json"
        if archive_path.exists():
            return f"Task {task_id} is already archived."
        return f"Task {task_id} not found."

    event = create_event(
        type="task_archived",
        task_id=task_id,
        actor=actor,
        data={},
        model=model,
        session=session,
        triggered_by=triggered_by,
        on_behalf_of=on_behalf_of,
        reason=provenance_reason,
    )

    updated_snapshot = apply_event_to_snapshot(snapshot, event)

    locks_dir = lattice_dir / "locks"
    lock_keys = sorted([f"events_{task_id}", f"tasks_{task_id}", "events__lifecycle"])

    with multi_lock(locks_dir, lock_keys):
        event_path = lattice_dir / "events" / f"{task_id}.jsonl"
        jsonl_append(event_path, serialize_event(event))

        lifecycle_path = lattice_dir / "events" / "_lifecycle.jsonl"
        jsonl_append(lifecycle_path, serialize_event(event))

        atomic_write(
            lattice_dir / "archive" / "tasks" / f"{task_id}.json",
            serialize_snapshot(updated_snapshot),
        )

        snapshot_path = lattice_dir / "tasks" / f"{task_id}.json"
        if snapshot_path.exists():
            snapshot_path.unlink()

        shutil.move(
            str(event_path),
            str(lattice_dir / "archive" / "events" / f"{task_id}.jsonl"),
        )

        notes_path = lattice_dir / "notes" / f"{task_id}.md"
        if notes_path.exists():
            shutil.move(
                str(notes_path),
                str(lattice_dir / "archive" / "notes" / f"{task_id}.md"),
            )

    execute_hooks(config, lattice_dir, task_id, event)
    return event


@cli.command()
@click.argument("task_ids", nargs=-1, required=True)
@common_options
def archive(
    task_ids: tuple[str, ...],
    actor: str,
    model: str | None,
    session: str | None,
    output_json: bool,
    quiet: bool,
    triggered_by: str | None,
    on_behalf_of: str | None,
    provenance_reason: str | None,
) -> None:
    """Archive one or more completed tasks.

    Accepts multiple task IDs separated by spaces or commas:

      lattice archive LAT-1 LAT-2 LAT-3 --actor human:atin

      lattice archive LAT-1,LAT-2,LAT-3 --actor human:atin
    """
    is_json = output_json

    lattice_dir = require_root(is_json)
    config = load_project_config(lattice_dir)
    validate_actor_or_exit(actor, is_json)
    if on_behalf_of is not None:
        validate_actor_or_exit(on_behalf_of, is_json)

    parsed_ids = _parse_task_ids(task_ids)

    # Single task: preserve original behavior (errors exit immediately)
    if len(parsed_ids) == 1:
        resolved = resolve_task_id(lattice_dir, parsed_ids[0], is_json)
        result = _archive_one(
            resolved,
            lattice_dir=lattice_dir,
            config=config,
            actor=actor,
            model=model,
            session=session,
            triggered_by=triggered_by,
            on_behalf_of=on_behalf_of,
            provenance_reason=provenance_reason,
            is_json=is_json,
        )
        if isinstance(result, str):
            code = "CONFLICT" if "already archived" in result else "NOT_FOUND"
            output_error(result, code, is_json)
        output_result(
            data=result,
            human_message=f"Archived task {resolved}",
            quiet_value=resolved,
            is_json=is_json,
            is_quiet=quiet,
        )
        return

    # Multiple tasks: process all, collect results
    succeeded: list[str] = []
    failed: list[tuple[str, str]] = []

    for raw_id in parsed_ids:
        try:
            resolved = resolve_task_id(lattice_dir, raw_id, is_json=False)
        except SystemExit:
            failed.append((raw_id, f"Invalid or unresolvable task ID: {raw_id}"))
            continue

        result = _archive_one(
            resolved,
            lattice_dir=lattice_dir,
            config=config,
            actor=actor,
            model=model,
            session=session,
            triggered_by=triggered_by,
            on_behalf_of=on_behalf_of,
            provenance_reason=provenance_reason,
            is_json=False,
        )
        if isinstance(result, str):
            failed.append((raw_id, result))
        else:
            succeeded.append(raw_id)

    if is_json:
        import json

        envelope = {
            "ok": len(failed) == 0,
            "data": {
                "archived": succeeded,
                "failed": [{"id": fid, "error": msg} for fid, msg in failed],
            },
        }
        click.echo(json.dumps(envelope, sort_keys=True, indent=2))
        if failed:
            sys.exit(1)
        return

    if quiet:
        for tid in succeeded:
            click.echo(tid)
        if failed:
            sys.exit(1)
        return

    # Human-friendly output
    if succeeded:
        click.echo(f"Archived {len(succeeded)} task(s): {', '.join(succeeded)}")
    for fid, msg in failed:
        click.echo(f"  Failed {fid}: {msg}", err=True)
    if failed:
        sys.exit(1)


def _unarchive_one(
    task_id: str,
    *,
    lattice_dir: Path,
    config: dict,
    actor: str,
    model: str | None,
    session: str | None,
    triggered_by: str | None,
    on_behalf_of: str | None,
    provenance_reason: str | None,
) -> dict | str:
    """Unarchive a single task. Returns the event dict on success or an error string on failure."""
    import json

    active_path = lattice_dir / "tasks" / f"{task_id}.json"
    if active_path.exists():
        return f"Task {task_id} is already active."

    archive_snapshot_path = lattice_dir / "archive" / "tasks" / f"{task_id}.json"
    if not archive_snapshot_path.exists():
        return f"Task {task_id} not found in archive."

    snapshot = json.loads(archive_snapshot_path.read_text())

    event = create_event(
        type="task_unarchived",
        task_id=task_id,
        actor=actor,
        data={},
        model=model,
        session=session,
        triggered_by=triggered_by,
        on_behalf_of=on_behalf_of,
        reason=provenance_reason,
    )

    updated_snapshot = apply_event_to_snapshot(snapshot, event)

    locks_dir = lattice_dir / "locks"
    lock_keys = sorted([f"events_{task_id}", f"tasks_{task_id}", "events__lifecycle"])

    with multi_lock(locks_dir, lock_keys):
        archive_event_path = lattice_dir / "archive" / "events" / f"{task_id}.jsonl"
        jsonl_append(archive_event_path, serialize_event(event))

        lifecycle_path = lattice_dir / "events" / "_lifecycle.jsonl"
        jsonl_append(lifecycle_path, serialize_event(event))

        shutil.move(
            str(archive_event_path),
            str(lattice_dir / "events" / f"{task_id}.jsonl"),
        )

        atomic_write(
            lattice_dir / "tasks" / f"{task_id}.json",
            serialize_snapshot(updated_snapshot),
        )

        archive_snapshot_path.unlink()

        archive_notes_path = lattice_dir / "archive" / "notes" / f"{task_id}.md"
        if archive_notes_path.exists():
            shutil.move(
                str(archive_notes_path),
                str(lattice_dir / "notes" / f"{task_id}.md"),
            )

    execute_hooks(config, lattice_dir, task_id, event)
    return event


@cli.command()
@click.argument("task_ids", nargs=-1, required=True)
@common_options
def unarchive(
    task_ids: tuple[str, ...],
    actor: str,
    model: str | None,
    session: str | None,
    output_json: bool,
    quiet: bool,
    triggered_by: str | None,
    on_behalf_of: str | None,
    provenance_reason: str | None,
) -> None:
    """Restore one or more archived tasks to active status.

    Accepts multiple task IDs separated by spaces or commas:

      lattice unarchive LAT-1 LAT-2 LAT-3 --actor human:atin

      lattice unarchive LAT-1,LAT-2,LAT-3 --actor human:atin
    """
    is_json = output_json

    lattice_dir = require_root(is_json)
    config = load_project_config(lattice_dir)
    validate_actor_or_exit(actor, is_json)
    if on_behalf_of is not None:
        validate_actor_or_exit(on_behalf_of, is_json)

    parsed_ids = _parse_task_ids(task_ids)

    # Single task: preserve original behavior
    if len(parsed_ids) == 1:
        resolved = resolve_task_id(lattice_dir, parsed_ids[0], is_json, allow_archived=True)
        result = _unarchive_one(
            resolved,
            lattice_dir=lattice_dir,
            config=config,
            actor=actor,
            model=model,
            session=session,
            triggered_by=triggered_by,
            on_behalf_of=on_behalf_of,
            provenance_reason=provenance_reason,
        )
        if isinstance(result, str):
            code = "CONFLICT" if "already active" in result else "NOT_FOUND"
            output_error(result, code, is_json)
        output_result(
            data=result,
            human_message=f"Unarchived task {resolved}",
            quiet_value=resolved,
            is_json=is_json,
            is_quiet=quiet,
        )
        return

    # Multiple tasks: process all, collect results
    succeeded: list[str] = []
    failed: list[tuple[str, str]] = []

    for raw_id in parsed_ids:
        try:
            resolved = resolve_task_id(
                lattice_dir, raw_id, is_json=False, allow_archived=True
            )
        except SystemExit:
            failed.append((raw_id, f"Invalid or unresolvable task ID: {raw_id}"))
            continue

        result = _unarchive_one(
            resolved,
            lattice_dir=lattice_dir,
            config=config,
            actor=actor,
            model=model,
            session=session,
            triggered_by=triggered_by,
            on_behalf_of=on_behalf_of,
            provenance_reason=provenance_reason,
        )
        if isinstance(result, str):
            failed.append((raw_id, result))
        else:
            succeeded.append(raw_id)

    if is_json:
        import json

        envelope = {
            "ok": len(failed) == 0,
            "data": {
                "unarchived": succeeded,
                "failed": [{"id": fid, "error": msg} for fid, msg in failed],
            },
        }
        click.echo(json.dumps(envelope, sort_keys=True, indent=2))
        if failed:
            sys.exit(1)
        return

    if quiet:
        for tid in succeeded:
            click.echo(tid)
        if failed:
            sys.exit(1)
        return

    if succeeded:
        click.echo(f"Unarchived {len(succeeded)} task(s): {', '.join(succeeded)}")
    for fid, msg in failed:
        click.echo(f"  Failed {fid}: {msg}", err=True)
    if failed:
        sys.exit(1)
