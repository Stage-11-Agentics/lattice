"""MCP resource registrations for Lattice — read-only auto-surfaced context."""

from __future__ import annotations

import functools
import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

from lattice.core.ids import is_short_id, validate_id
from lattice.mcp.server import mcp
from lattice.storage.operations import (
    discover_task_authorities,
    read_task_authority,
    resolve_task_prose_path,
)
from lattice.storage.short_ids import resolve_short_id


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _resource(uri: str) -> Callable[[Callable[..., str]], Callable[..., str]]:
    """Register a resource whose read is one command, as a tool call is
    (``lattice.mcp.tools._call``): caught up first, read lock released after."""

    def register(fn: Callable[..., str]) -> Callable[..., str]:
        @functools.wraps(fn)
        def read(*args: Any, **kwargs: Any) -> str:
            from lattice.mcp.tools import _call

            with _call():
                return fn(*args, **kwargs)

        return mcp.resource(uri)(read)

    return register


def _find_root_dir() -> Path:
    """Resolve the .lattice/ directory, caught up before it is read (SPEC §12)."""
    from lattice.boards import resolve_board
    from lattice.ops import OpError

    try:
        return resolve_board().lattice_dir
    except OpError as exc:
        raise ValueError(exc.message) from exc


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
    return [
        authority.snapshot
        for authority in discover_task_authorities(lattice_dir, include_archived=False)
    ]


# ---------------------------------------------------------------------------
# Resources
# ---------------------------------------------------------------------------


@_resource("lattice://tasks")
def resource_all_tasks() -> str:
    """All active task snapshots as a JSON array."""
    lattice_dir = _find_root_dir()
    snapshots = _load_all_snapshots(lattice_dir)
    return json.dumps(snapshots, sort_keys=True, indent=2)


@_resource("lattice://tasks/{task_id}")
def resource_task_detail(task_id: str) -> str:
    """Full task detail including events as a JSON object."""
    lattice_dir = _find_root_dir()
    task_id = _resolve_task_id(lattice_dir, task_id)

    authority = read_task_authority(lattice_dir, task_id, allow_missing=True)
    if authority is None:
        raise ValueError(f"Task {task_id} not found.")
    snapshot = authority.snapshot
    is_archived = authority.location == "archived"

    result = dict(snapshot)
    if is_archived:
        result["archived"] = True
    result["events"] = list(authority.events)
    return json.dumps(result, sort_keys=True, indent=2)


@_resource("lattice://tasks/status/{status}")
def resource_tasks_by_status(status: str) -> str:
    """Tasks filtered by status as a JSON array."""
    lattice_dir = _find_root_dir()
    snapshots = _load_all_snapshots(lattice_dir)
    filtered = [s for s in snapshots if s.get("status") == status]
    return json.dumps(filtered, sort_keys=True, indent=2)


@_resource("lattice://tasks/assigned/{actor}")
def resource_tasks_by_assignee(actor: str) -> str:
    """Tasks filtered by assignee as a JSON array."""
    lattice_dir = _find_root_dir()
    snapshots = _load_all_snapshots(lattice_dir)
    filtered = [s for s in snapshots if s.get("assigned_to") == actor]
    return json.dumps(filtered, sort_keys=True, indent=2)


@_resource("lattice://config")
def resource_config() -> str:
    """The project config.json contents."""
    lattice_dir = _find_root_dir()
    return (lattice_dir / "config.json").read_text()


@_resource("lattice://notes/{task_id}")
def resource_notes(task_id: str) -> str:
    """The task's notes markdown file contents."""
    lattice_dir = _find_root_dir()
    task_id = _resolve_task_id(lattice_dir, task_id)

    notes_path, _authority = resolve_task_prose_path(lattice_dir, task_id, "notes")
    if notes_path is not None:
        return notes_path.read_text(encoding="utf-8")

    raise ValueError(f"No notes file found for task {task_id}.")


@_resource("lattice://plans/{task_id}")
def resource_plans(task_id: str) -> str:
    """The task's plan markdown file contents."""
    lattice_dir = _find_root_dir()
    task_id = _resolve_task_id(lattice_dir, task_id)

    plan_path, _authority = resolve_task_prose_path(lattice_dir, task_id, "plan")
    if plan_path is not None:
        return plan_path.read_text(encoding="utf-8")

    raise ValueError(f"No plan file found for task {task_id}.")
