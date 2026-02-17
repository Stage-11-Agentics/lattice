"""Shared write-path operations used by both CLI and dashboard."""

from __future__ import annotations

from pathlib import Path

from lattice.core.events import LIFECYCLE_EVENT_TYPES, serialize_event
from lattice.core.tasks import serialize_snapshot
from lattice.storage.fs import atomic_write, jsonl_append
from lattice.storage.hooks import execute_hooks
from lattice.storage.locks import multi_lock


def scaffold_notes(
    lattice_dir: Path,
    task_id: str,
    title: str,
    short_id: str | None,
    description: str | None,
) -> None:
    """Create the initial notes markdown file for a new task.

    Non-authoritative — this is a convenience scaffold for humans and agents
    to use as a working document. Skipped silently if the file already exists
    (idempotent create).
    """
    notes_path = lattice_dir / "notes" / f"{task_id}.md"
    if notes_path.exists():
        return

    heading = f"# {short_id}: {title}" if short_id else f"# {title}"
    lines = [heading, ""]

    lines.append("## Summary")
    lines.append("")
    if description:
        lines.append(description)
    else:
        lines.append("<!-- Human-readable summary of what this task is and why it matters. -->")
    lines.append("")

    lines.append("## Technical Plan")
    lines.append("")
    lines.append("<!-- Implementation approach, design decisions, open questions. -->")
    lines.append("")

    notes_path.write_text("\n".join(lines), encoding="utf-8")


def write_task_event(
    lattice_dir: Path,
    task_id: str,
    events: list[dict],
    snapshot: dict,
    config: dict | None = None,
) -> None:
    """Write event(s) and snapshot atomically with proper locking.

    This is the canonical write path for all task mutations. Both the CLI
    and dashboard route through this function.

    Steps:
    1. Acquire locks in sorted order
    2. Append events to per-task JSONL
    3. Append lifecycle events to _lifecycle.jsonl
    4. Atomic-write snapshot
    5. Release locks
    6. Fire hooks (after locks released, data is durable)
    """
    locks_dir = lattice_dir / "locks"

    # Determine which events go to lifecycle log
    lifecycle_events = [e for e in events if e["type"] in LIFECYCLE_EVENT_TYPES]

    # Build lock keys
    lock_keys = [f"events_{task_id}", f"tasks_{task_id}"]
    if lifecycle_events:
        lock_keys.append("events__lifecycle")
    lock_keys.sort()

    with multi_lock(locks_dir, lock_keys):
        # Event-first: append to per-task log
        event_path = lattice_dir / "events" / f"{task_id}.jsonl"
        for event in events:
            jsonl_append(event_path, serialize_event(event))

        # Lifecycle events go to lifecycle log
        if lifecycle_events:
            lifecycle_path = lattice_dir / "events" / "_lifecycle.jsonl"
            for event in lifecycle_events:
                jsonl_append(lifecycle_path, serialize_event(event))

        # Then materialize snapshot
        snapshot_path = lattice_dir / "tasks" / f"{task_id}.json"
        atomic_write(snapshot_path, serialize_snapshot(snapshot))

    # Fire hooks after locks are released (data is durable)
    if config:
        for event in events:
            execute_hooks(config, lattice_dir, task_id, event)


def write_resource_event(
    lattice_dir: Path,
    resource_id: str,
    resource_name: str,
    events: list[dict],
    snapshot: dict,
    config: dict | None = None,
) -> None:
    """Write resource event(s) and snapshot atomically with proper locking.

    This is the canonical write path for all resource mutations.

    Steps:
    1. Ensure resource directory exists
    2. Acquire locks in sorted order
    3. Append events to per-resource JSONL (in events/ dir, keyed by resource_id)
    4. Atomic-write resource snapshot
    5. Release locks
    6. Fire hooks (after locks released, data is durable)
    """
    from lattice.core.resources import serialize_resource_snapshot

    locks_dir = lattice_dir / "locks"

    # Ensure resource directory exists
    resource_dir = lattice_dir / "resources" / resource_name
    resource_dir.mkdir(parents=True, exist_ok=True)

    # Build lock keys
    lock_keys = [f"events_{resource_id}", f"resources_{resource_name}"]
    lock_keys.sort()

    with multi_lock(locks_dir, lock_keys):
        # Event-first: append to per-resource event log
        event_path = lattice_dir / "events" / f"{resource_id}.jsonl"
        for event in events:
            jsonl_append(event_path, serialize_event(event))

        # Then materialize snapshot
        snapshot_path = resource_dir / "resource.json"
        atomic_write(snapshot_path, serialize_resource_snapshot(snapshot))

    # Fire hooks after locks are released (data is durable)
    if config:
        from lattice.storage.hooks import execute_resource_hooks

        for event in events:
            execute_resource_hooks(config, lattice_dir, resource_id, resource_name, event)
