"""Migration commands: backfill-ids."""

from __future__ import annotations

import json
from pathlib import Path

import click

from lattice.cli.helpers import (
    load_project_config,
    output_error,
    require_root,
    validate_actor_or_exit,
)
from lattice.cli.main import cli
from lattice.core.config import serialize_config, validate_project_code
from lattice.core.events import create_event
from lattice.core.tasks import apply_event_to_snapshot, serialize_snapshot
from lattice.storage.fs import atomic_write, jsonl_append
from lattice.storage.locks import multi_lock
from lattice.storage.short_ids import load_id_index, register_short_id, save_id_index


def _collect_tasks_missing_short_id(lattice_dir: Path) -> list[dict]:
    """Collect all task snapshots (active + archived) that lack a short_id.

    Returns a list of (snapshot, is_archived) sorted by created_at then id.
    """
    tasks: list[tuple[str, dict, bool]] = []

    for directory, is_archived in [
        (lattice_dir / "tasks", False),
        (lattice_dir / "archive" / "tasks", True),
    ]:
        if not directory.is_dir():
            continue
        for snap_file in directory.glob("*.json"):
            try:
                snap = json.loads(snap_file.read_text())
            except (json.JSONDecodeError, OSError):
                continue
            if snap.get("short_id") is None:
                tasks.append((snap.get("created_at", ""), snap, is_archived))

    # Sort deterministically: created_at, then id
    tasks.sort(key=lambda t: (t[0], t[1].get("id", "")))
    return [(snap, is_archived) for _, snap, is_archived in tasks]


@cli.command("backfill-ids")
@click.option("--code", default=None, help="Project code (sets it if not already configured).")
@click.option("--force", is_flag=True, help="Allow overriding an existing project code.")
@click.option("--json", "output_json", is_flag=True, help="Output structured JSON.")
@click.option("--actor", default="agent:lattice-migration", help="Actor for backfill events.")
def backfill_ids(
    code: str | None,
    force: bool,
    output_json: bool,
    actor: str,
) -> None:
    """Assign short IDs to existing tasks that don't have one."""
    is_json = output_json
    lattice_dir = require_root(is_json)
    config = load_project_config(lattice_dir)

    validate_actor_or_exit(actor, is_json)

    # Resolve project code
    existing_code = config.get("project_code")
    if code:
        code = code.upper()
        if not validate_project_code(code):
            output_error(
                f"Invalid project code: '{code}'. Must be 1-5 uppercase ASCII letters.",
                "VALIDATION_ERROR",
                is_json,
            )
        if existing_code and existing_code != code and not force:
            output_error(
                f"Project code is already set to '{existing_code}'. Use --force to override.",
                "CONFLICT",
                is_json,
            )
        if not existing_code or (existing_code != code and force):
            config["project_code"] = code
            atomic_write(lattice_dir / "config.json", serialize_config(config))
    elif existing_code:
        code = existing_code
    else:
        output_error(
            "No project code configured. Use --code to set one.",
            "VALIDATION_ERROR",
            is_json,
        )

    # Collect tasks missing short_id
    tasks = _collect_tasks_missing_short_id(lattice_dir)
    if not tasks:
        if is_json:
            click.echo(
                json.dumps(
                    {
                        "ok": True,
                        "data": {"assigned": 0, "message": "All tasks already have short IDs"},
                    },
                    sort_keys=True,
                    indent=2,
                )
                + "\n"
            )
        else:
            click.echo("All tasks already have short IDs.")
        return

    # Allocate and assign short IDs
    index = load_id_index(lattice_dir)
    assigned: list[str] = []

    # Compute prefix from project code + optional subproject code
    subproject_code = config.get("subproject_code")
    prefix = f"{code}-{subproject_code}" if subproject_code else code

    for snap, is_archived in tasks:
        task_ulid = snap["id"]
        next_seqs = index.get("next_seqs", {})
        seq = next_seqs.get(prefix, 1)
        short_id = f"{prefix}-{seq}"
        next_seqs[prefix] = seq + 1
        index["next_seqs"] = next_seqs

        # Emit task_short_id_assigned event
        from lattice.core.events import serialize_event

        event = create_event(
            type="task_short_id_assigned",
            task_id=task_ulid,
            actor=actor,
            data={"short_id": short_id},
        )

        # Apply to snapshot
        updated_snap = apply_event_to_snapshot(snap, event)

        # Determine paths
        if is_archived:
            event_path = lattice_dir / "archive" / "events" / f"{task_ulid}.jsonl"
            snap_path = lattice_dir / "archive" / "tasks" / f"{task_ulid}.json"
        else:
            event_path = lattice_dir / "events" / f"{task_ulid}.jsonl"
            snap_path = lattice_dir / "tasks" / f"{task_ulid}.json"

        # Write event and snapshot under lock
        locks_dir = lattice_dir / "locks"
        with multi_lock(locks_dir, sorted([f"events_{task_ulid}", f"tasks_{task_ulid}"])):
            jsonl_append(event_path, serialize_event(event))
            atomic_write(snap_path, serialize_snapshot(updated_snap))

        # Register in index
        register_short_id(index, short_id, task_ulid)
        assigned.append(short_id)

    # Save index
    save_id_index(lattice_dir, index)

    first_id = assigned[0] if assigned else "?"
    last_id = assigned[-1] if assigned else "?"
    count = len(assigned)

    if is_json:
        click.echo(
            json.dumps(
                {
                    "ok": True,
                    "data": {
                        "assigned": count,
                        "first": first_id,
                        "last": last_id,
                    },
                },
                sort_keys=True,
                indent=2,
            )
            + "\n"
        )
    else:
        click.echo(f"Assigned {first_id} through {last_id} to {count} existing tasks.")
