"""MCP resource registrations for Lattice — read-only auto-surfaced context."""

from __future__ import annotations

import json
from pathlib import Path

from lattice.core.ids import is_short_id, validate_id
from lattice.mcp.server import mcp
from lattice.storage.fs import find_root
from lattice.storage.short_ids import resolve_short_id


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _find_root_dir() -> Path:
    """Resolve the .lattice/ directory."""
    root = find_root()
    if root is None:
        raise ValueError("No .lattice/ directory found.")
    return root / ".lattice"


def _resolve_task_id(lattice_dir: Path, raw_id: str) -> str:
    """Resolve a short ID or ULID to the canonical task ULID."""
    if validate_id(raw_id, "task"):
        return raw_id
    if is_short_id(raw_id):
        normalized = raw_id.upper()
        ulid = resolve_short_id(lattice_dir, normalized)
        if ulid is not None:
            return ulid
        raise ValueError(f"Short ID '{normalized}' not found.")
    raise ValueError(f"Invalid task ID format: '{raw_id}'.")


def _load_all_snapshots(lattice_dir: Path) -> list[dict]:
    """Load all active task snapshots."""
    tasks_dir = lattice_dir / "tasks"
    snapshots: list[dict] = []
    if tasks_dir.is_dir():
        for task_file in sorted(tasks_dir.glob("*.json")):
            try:
                snapshots.append(json.loads(task_file.read_text()))
            except (json.JSONDecodeError, OSError):
                continue
    return snapshots


def _read_events(lattice_dir: Path, task_id: str, is_archived: bool = False) -> list[dict]:
    """Read all events for a task."""
    if is_archived:
        event_path = lattice_dir / "archive" / "events" / f"{task_id}.jsonl"
    else:
        event_path = lattice_dir / "events" / f"{task_id}.jsonl"
    events: list[dict] = []
    if event_path.exists():
        for line in event_path.read_text().splitlines():
            line = line.strip()
            if line:
                try:
                    events.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
    return events


# ---------------------------------------------------------------------------
# Resources
# ---------------------------------------------------------------------------


@mcp.resource("lattice://tasks")
def resource_all_tasks() -> str:
    """All active task snapshots as a JSON array."""
    lattice_dir = _find_root_dir()
    snapshots = _load_all_snapshots(lattice_dir)
    return json.dumps(snapshots, sort_keys=True, indent=2)


@mcp.resource("lattice://tasks/{task_id}")
def resource_task_detail(task_id: str) -> str:
    """Full task detail including events as a JSON object."""
    lattice_dir = _find_root_dir()
    task_id = _resolve_task_id(lattice_dir, task_id)

    # Try active first
    snap_path = lattice_dir / "tasks" / f"{task_id}.json"
    is_archived = False
    if snap_path.exists():
        snapshot = json.loads(snap_path.read_text())
    else:
        archive_path = lattice_dir / "archive" / "tasks" / f"{task_id}.json"
        if archive_path.exists():
            snapshot = json.loads(archive_path.read_text())
            is_archived = True
        else:
            raise ValueError(f"Task {task_id} not found.")

    result = dict(snapshot)
    if is_archived:
        result["archived"] = True
    result["events"] = _read_events(lattice_dir, task_id, is_archived)
    return json.dumps(result, sort_keys=True, indent=2)


@mcp.resource("lattice://tasks/status/{status}")
def resource_tasks_by_status(status: str) -> str:
    """Tasks filtered by status as a JSON array."""
    lattice_dir = _find_root_dir()
    snapshots = _load_all_snapshots(lattice_dir)
    filtered = [s for s in snapshots if s.get("status") == status]
    return json.dumps(filtered, sort_keys=True, indent=2)


@mcp.resource("lattice://tasks/assigned/{actor}")
def resource_tasks_by_assignee(actor: str) -> str:
    """Tasks filtered by assignee as a JSON array."""
    lattice_dir = _find_root_dir()
    snapshots = _load_all_snapshots(lattice_dir)
    filtered = [s for s in snapshots if s.get("assigned_to") == actor]
    return json.dumps(filtered, sort_keys=True, indent=2)


@mcp.resource("lattice://config")
def resource_config() -> str:
    """The project config.json contents."""
    lattice_dir = _find_root_dir()
    return (lattice_dir / "config.json").read_text()


@mcp.resource("lattice://notes/{task_id}")
def resource_notes(task_id: str) -> str:
    """The task's notes markdown file contents."""
    lattice_dir = _find_root_dir()
    task_id = _resolve_task_id(lattice_dir, task_id)

    notes_path = lattice_dir / "notes" / f"{task_id}.md"
    if notes_path.exists():
        return notes_path.read_text()

    # Check archive
    archive_notes = lattice_dir / "archive" / "notes" / f"{task_id}.md"
    if archive_notes.exists():
        return archive_notes.read_text()

    raise ValueError(f"No notes file found for task {task_id}.")
