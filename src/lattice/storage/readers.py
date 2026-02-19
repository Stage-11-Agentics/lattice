"""Shared read helpers for task events and snapshots."""

from __future__ import annotations

import json
from pathlib import Path


def read_task_events(lattice_dir: Path, task_id: str, *, is_archived: bool = False) -> list[dict]:
    """Read all events for a task from its JSONL log.

    Returns an empty list if the event file does not exist.
    """
    if is_archived:
        event_path = lattice_dir / "archive" / "events" / f"{task_id}.jsonl"
    else:
        event_path = lattice_dir / "events" / f"{task_id}.jsonl"

    events: list[dict] = []
    if event_path.exists():
        try:
            for line in event_path.read_text().splitlines():
                line = line.strip()
                if line:
                    try:
                        events.append(json.loads(line))
                    except json.JSONDecodeError:
                        continue
        except OSError:
            pass
    return events
