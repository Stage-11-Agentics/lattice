"""Shared statistics logic for CLI and dashboard."""

from __future__ import annotations

import json
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path


def load_all_snapshots(lattice_dir: Path) -> tuple[list[dict], list[dict]]:
    """Load all active and archived task snapshots.

    Returns (active, archived) lists.
    """
    active: list[dict] = []
    archived: list[dict] = []

    tasks_dir = lattice_dir / "tasks"
    if tasks_dir.is_dir():
        for f in tasks_dir.glob("*.json"):
            try:
                active.append(json.loads(f.read_text()))
            except (json.JSONDecodeError, OSError):
                continue

    archive_dir = lattice_dir / "archive" / "tasks"
    if archive_dir.is_dir():
        for f in archive_dir.glob("*.json"):
            try:
                archived.append(json.loads(f.read_text()))
            except (json.JSONDecodeError, OSError):
                continue

    return active, archived


def count_events(lattice_dir: Path, archived: bool = False) -> tuple[int, Counter]:
    """Count total events and events per task.

    Returns (total_events, per_task_counter).
    """
    if archived:
        events_dir = lattice_dir / "archive" / "events"
    else:
        events_dir = lattice_dir / "events"

    total = 0
    per_task: Counter = Counter()

    if not events_dir.is_dir():
        return total, per_task

    for f in events_dir.glob("*.jsonl"):
        if f.name.startswith("_"):
            continue  # skip _lifecycle.jsonl
        task_id = f.stem
        count = 0
        for line in f.read_text().splitlines():
            if line.strip():
                count += 1
        total += count
        per_task[task_id] = count

    return total, per_task


def parse_ts(ts_str: str) -> datetime | None:
    """Parse an RFC 3339 timestamp string."""
    try:
        return datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
    except (ValueError, AttributeError):
        return None


def days_ago(ts_str: str, now: datetime) -> float | None:
    """Return how many days ago a timestamp was."""
    dt = parse_ts(ts_str)
    if dt is None:
        return None
    delta = now - dt
    return delta.total_seconds() / 86400


def format_days(days: float) -> str:
    """Format a day count as a human string."""
    if days < 1:
        hours = days * 24
        if hours < 1:
            return f"{int(hours * 60)}m"
        return f"{hours:.0f}h"
    if days < 30:
        return f"{days:.0f}d"
    return f"{days / 30:.1f}mo"


def build_stats(lattice_dir: Path, config: dict) -> dict:
    """Build the full stats data structure."""
    now = datetime.now(timezone.utc)
    active, archived = load_all_snapshots(lattice_dir)

    # Event counts
    active_events, active_per_task = count_events(lattice_dir, archived=False)
    archived_events, _ = count_events(lattice_dir, archived=True)

    # --- Distributions (active tasks only) ---
    status_counts: Counter = Counter()
    priority_counts: Counter = Counter()
    type_counts: Counter = Counter()
    assignee_counts: Counter = Counter()
    tag_counts: Counter = Counter()

    for snap in active:
        status_counts[snap.get("status", "unknown")] += 1
        priority_counts[snap.get("priority", "unset")] += 1
        type_counts[snap.get("type", "unset")] += 1
        assignee_counts[snap.get("assigned_to") or "unassigned"] += 1
        for tag in snap.get("tags") or []:
            tag_counts[tag] += 1

    # --- Staleness (active tasks only) ---
    stale: list[dict] = []  # tasks not updated in 7+ days
    recently_active: list[dict] = []

    for snap in active:
        updated_at = snap.get("updated_at", "")
        d = days_ago(updated_at, now)
        if d is not None and d >= 7:
            stale.append({
                "id": snap.get("short_id") or snap.get("id", "?"),
                "full_id": snap.get("id", ""),
                "title": snap.get("title", "?"),
                "status": snap.get("status", "?"),
                "days_stale": round(d, 1),
            })

    # Sort stale by stalest first
    stale.sort(key=lambda s: s["days_stale"], reverse=True)

    # Recently active: 5 most recently updated
    active_sorted = sorted(
        active,
        key=lambda s: s.get("updated_at", ""),
        reverse=True,
    )
    for snap in active_sorted[:5]:
        d = days_ago(snap.get("updated_at", ""), now)
        recently_active.append({
            "id": snap.get("short_id") or snap.get("id", "?"),
            "full_id": snap.get("id", ""),
            "title": snap.get("title", "?"),
            "status": snap.get("status", "?"),
            "updated_ago": format_days(d) if d is not None else "?",
        })

    # --- Busiest tasks (by event count) ---
    busiest: list[dict] = []
    for task_id, count in active_per_task.most_common(5):
        # Look up title from active snapshots
        title = "?"
        short_id = None
        full_id = task_id
        for snap in active:
            if snap.get("id") == task_id:
                title = snap.get("title", "?")
                short_id = snap.get("short_id")
                full_id = snap.get("id", task_id)
                break
        busiest.append({
            "id": short_id or task_id,
            "full_id": full_id,
            "title": title,
            "event_count": count,
        })

    # --- Workflow config info ---
    workflow = config.get("workflow", {})
    wip_limits = workflow.get("wip_limits", {})
    wip_status: list[dict] = []
    for status_name, limit in wip_limits.items():
        current = status_counts.get(status_name, 0)
        wip_status.append({
            "status": status_name,
            "current": current,
            "limit": limit,
            "over": current > limit,
        })

    # Order status counts by workflow order
    defined_statuses = workflow.get("statuses", [])
    ordered_status: list[tuple[str, int]] = []
    for s in defined_statuses:
        if status_counts[s] > 0:
            ordered_status.append((s, status_counts[s]))
    # Add any statuses not in workflow (shouldn't happen, but safety)
    for s, c in status_counts.items():
        if s not in defined_statuses and c > 0:
            ordered_status.append((s, c))

    return {
        "summary": {
            "active_tasks": len(active),
            "archived_tasks": len(archived),
            "total_tasks": len(active) + len(archived),
            "total_events": active_events + archived_events,
            "active_events": active_events,
        },
        "by_status": ordered_status,
        "by_priority": priority_counts.most_common(),
        "by_type": type_counts.most_common(),
        "by_assignee": assignee_counts.most_common(),
        "by_tag": tag_counts.most_common(),
        "wip": wip_status,
        "recently_active": recently_active,
        "stale": stale,
        "busiest": busiest,
    }
