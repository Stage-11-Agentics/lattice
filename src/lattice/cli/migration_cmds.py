"""Migration commands: backfill-ids."""

from __future__ import annotations

import json
from pathlib import Path

import click

from lattice.cli.helpers import (
    load_project_config,
    output_error,
    require_root,
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

    # Resolve project code
    existing_code = config.get("project_code")
    if code:
        code = code.upper()
        if not validate_project_code(code):
            output_error(
                f"Invalid project code: '{code}'. Must be 1-5 uppercase ASCII letters/digits, starting with a letter.",
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


# ---------------------------------------------------------------------------
# lattice migrate — instance migrations
# ---------------------------------------------------------------------------


@cli.group("migrate")
def migrate_group() -> None:
    """Instance migrations (schema/workflow changes for existing .lattice dirs)."""


@migrate_group.command("needs-human")
@click.option("--dry-run", is_flag=True, help="Report what would change without writing.")
@click.option("--json", "output_json", is_flag=True, help="Output structured JSON.")
@click.option("--actor", default="agent:lattice-migration", help="Actor for migration events.")
def migrate_needs_human(
    dry_run: bool,
    output_json: bool,
    actor: str,
) -> None:
    """Convert the needs_human STATUS to the orthogonal flag.

    For every task sitting in the needs_human status: set the needs_human
    flag (reason: the task's latest comment, or a generic migration note)
    and route the task back to the status it was in before entering
    needs_human (fallback: backlog). Then strip needs_human from the
    workflow config (statuses, transitions, universal_targets,
    descriptions, display_names). Idempotent — re-running on a migrated
    instance is a no-op.
    """
    from lattice.cli.helpers import write_task_event
    from lattice.core.comments import materialize_comments

    is_json = output_json
    lattice_dir = require_root(is_json)
    config = load_project_config(lattice_dir)

    # ---- Phase 1: tasks sitting in the needs_human status -----------------
    tasks_dir = lattice_dir / "tasks"
    migrated: list[dict] = []
    statuses_after = [
        s for s in config.get("workflow", {}).get("statuses", []) if s != "needs_human"
    ]

    snap_files = sorted(tasks_dir.glob("*.json")) if tasks_dir.is_dir() else []
    for snap_file in snap_files:
        try:
            snap = json.loads(snap_file.read_text())
        except (json.JSONDecodeError, OSError):
            continue
        if snap.get("status") != "needs_human":
            continue
        task_id = snap["id"]

        # Return status: the `from` of the latest transition INTO needs_human.
        events_path = lattice_dir / "events" / f"{task_id}.jsonl"
        events: list[dict] = []
        if events_path.exists():
            for line in events_path.read_text().splitlines():
                stripped = line.strip()
                if stripped:
                    events.append(json.loads(stripped))
        return_status = "backlog"
        for ev in reversed(events):
            if (
                ev.get("type") == "status_changed"
                and ev.get("data", {}).get("to") == "needs_human"
            ):
                candidate = ev.get("data", {}).get("from")
                if isinstance(candidate, str) and candidate in statuses_after:
                    return_status = candidate
                break

        # Flag reason: latest non-deleted comment, else a generic note.
        reason = "Migrated from needs_human status"
        comments = materialize_comments(events)
        for c in reversed(comments):
            if not c.get("deleted") and c.get("body"):
                reason = c["body"]
                break

        migrated.append(
            {
                "task_id": task_id,
                "short_id": snap.get("short_id"),
                "return_status": return_status,
                "reason": reason,
            }
        )

        if dry_run:
            continue

        new_events: list[dict] = []
        updated = snap
        if not updated.get("needs_human"):
            flag_event = create_event(
                type="needs_human_flagged",
                task_id=task_id,
                actor=actor,
                data={"reason": reason},
                reason="LAT-232 migration: needs_human status converted to flag",
            )
            new_events.append(flag_event)
            updated = apply_event_to_snapshot(updated, flag_event)
        status_event = create_event(
            type="status_changed",
            task_id=task_id,
            actor=actor,
            data={"from": "needs_human", "to": return_status},
            reason="LAT-232 migration: needs_human status converted to flag",
        )
        new_events.append(status_event)
        updated = apply_event_to_snapshot(updated, status_event)
        write_task_event(lattice_dir, task_id, new_events, updated, config)

    # ---- Phase 2: strip needs_human from the workflow config ---------------
    workflow = config.get("workflow", {})
    config_changes: list[str] = []

    if "needs_human" in workflow.get("statuses", []):
        config_changes.append("statuses")
    transitions = workflow.get("transitions", {})
    if "needs_human" in transitions:
        config_changes.append("transitions (source)")
    if any("needs_human" in targets for targets in transitions.values()):
        config_changes.append("transitions (targets)")
    if "needs_human" in workflow.get("universal_targets", []):
        config_changes.append("universal_targets")
    if "needs_human" in workflow.get("descriptions", {}):
        config_changes.append("descriptions")
    if "needs_human" in workflow.get("display_names", {}):
        config_changes.append("display_names")

    if config_changes and not dry_run:
        workflow["statuses"] = statuses_after
        transitions.pop("needs_human", None)
        workflow["transitions"] = {
            source: [t for t in targets if t != "needs_human"]
            for source, targets in transitions.items()
        }
        workflow["universal_targets"] = [
            t for t in workflow.get("universal_targets", []) if t != "needs_human"
        ]
        workflow.get("descriptions", {}).pop("needs_human", None)
        workflow.get("display_names", {}).pop("needs_human", None)
        atomic_write(lattice_dir / "config.json", serialize_config(config))

    # ---- Report -------------------------------------------------------------
    data = {
        "dry_run": dry_run,
        "tasks_migrated": migrated,
        "config_changes": config_changes,
    }
    if is_json:
        click.echo(json.dumps({"ok": True, "data": data}, sort_keys=True, indent=2) + "\n")
        return
    prefix = "[dry-run] " if dry_run else ""
    if not migrated and not config_changes:
        click.echo(f"{prefix}Nothing to migrate — instance already uses the needs_human flag.")
        return
    for item in migrated:
        label = item["short_id"] or item["task_id"]
        click.echo(f"{prefix}{label}: flagged + routed needs_human -> {item['return_status']}")
    if config_changes:
        click.echo(f"{prefix}config.json: stripped needs_human from {', '.join(config_changes)}")
