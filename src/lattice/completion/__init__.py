"""Shell completion callbacks for the Lattice CLI."""

from __future__ import annotations

import json
from pathlib import Path

import click
from click.shell_completion import CompletionItem


def _find_lattice_root() -> Path | None:
    """Locate .lattice/ root independently of Click context."""
    from lattice.storage.fs import find_root  # deferred to avoid import-time side effects

    return find_root()


def complete_task_id(
    ctx: click.Context, param: click.Parameter, incomplete: str
) -> list[CompletionItem]:
    """Complete task short IDs (e.g. LAT-1) from .lattice/ids.json."""
    try:
        from lattice.storage.short_ids import load_id_index

        root = _find_lattice_root()
        if root is None:
            return []
        lattice_dir = root / ".lattice"
        index = load_id_index(lattice_dir)
        id_map: dict[str, str] = index.get("map", {})
        return [
            CompletionItem(short_id)
            for short_id in sorted(id_map)
            if short_id.startswith(incomplete)
        ]
    except Exception:
        return []


_DEFAULT_STATUSES = [
    "backlog",
    "ready",
    "in_progress",
    "in_review",
    "done",
    "cancelled",
    "needs_human",
]


def complete_status(
    ctx: click.Context, param: click.Parameter, incomplete: str
) -> list[CompletionItem]:
    """Complete task status values from config or defaults."""
    statuses = _DEFAULT_STATUSES
    try:
        root = _find_lattice_root()
        if root is not None:
            config_file = root / ".lattice" / "config.json"
            if config_file.exists():
                data = json.loads(config_file.read_text())
                statuses = data.get("workflow", {}).get("statuses", statuses)
    except Exception:
        pass
    return [CompletionItem(s) for s in statuses if s.startswith(incomplete)]


def complete_actor(
    ctx: click.Context, param: click.Parameter, incomplete: str
) -> list[CompletionItem]:
    """Complete actor IDs from recent task snapshots."""
    try:
        root = _find_lattice_root()
        if root is None:
            return []
        tasks_dir = root / ".lattice" / "tasks"
        if not tasks_dir.exists():
            return []
        actors: set[str] = set()
        snapshot_files = sorted(
            tasks_dir.glob("*.json"),
            key=lambda p: p.stat().st_mtime,
            reverse=True,
        )
        for snap_file in snapshot_files[:50]:
            try:
                data = json.loads(snap_file.read_text())
                actor = data.get("assigned_to")
                if actor and isinstance(actor, str):
                    actors.add(actor)
            except Exception:
                continue
        return [CompletionItem(a) for a in sorted(actors) if a.startswith(incomplete)]
    except Exception:
        return []


def complete_resource_name(
    ctx: click.Context, param: click.Parameter, incomplete: str
) -> list[CompletionItem]:
    """Complete resource names from .lattice/resources/."""
    try:
        root = _find_lattice_root()
        if root is None:
            return []
        res_dir = root / ".lattice" / "resources"
        if not res_dir.exists():
            return []
        names = [p.stem for p in res_dir.glob("*.json") if p.stem.startswith(incomplete)]
        return [CompletionItem(n) for n in sorted(names)]
    except Exception:
        return []


def complete_session_name(
    ctx: click.Context, param: click.Parameter, incomplete: str
) -> list[CompletionItem]:
    """Complete session names from .lattice/sessions/."""
    try:
        root = _find_lattice_root()
        if root is None:
            return []
        sess_dir = root / ".lattice" / "sessions"
        if not sess_dir.exists():
            return []
        names = [p.stem for p in sess_dir.glob("*.json") if p.stem.startswith(incomplete)]
        return [CompletionItem(n) for n in sorted(names)]
    except Exception:
        return []


_RELATIONSHIP_TYPES = [
    "blocks",
    "blocked_by",
    "subtask_of",
    "parent_of",
    "related_to",
    "depends_on",
]


def complete_relationship_type(
    ctx: click.Context, param: click.Parameter, incomplete: str
) -> list[CompletionItem]:
    """Complete relationship types for link/unlink commands."""
    return [CompletionItem(t) for t in _RELATIONSHIP_TYPES if t.startswith(incomplete)]
