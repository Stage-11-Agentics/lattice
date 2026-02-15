"""Shared write-path operations used by both CLI and dashboard."""

from __future__ import annotations

from pathlib import Path

from lattice.core.events import LIFECYCLE_EVENT_TYPES, serialize_event
from lattice.core.tasks import serialize_snapshot
from lattice.storage.fs import atomic_write, jsonl_append
from lattice.storage.locks import multi_lock


def write_task_event(
    lattice_dir: Path,
    task_id: str,
    events: list[dict],
    snapshot: dict,
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
