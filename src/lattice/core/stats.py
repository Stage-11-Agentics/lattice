"""Shared statistics logic for CLI and dashboard."""

from __future__ import annotations

import json
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
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


def load_all_events(lattice_dir: Path) -> list[dict]:
    """Load and parse all events from active task event logs.

    Returns a flat list of event dicts, sorted by timestamp.
    """
    events_dir = lattice_dir / "events"
    events: list[dict] = []
    if not events_dir.is_dir():
        return events
    for f in events_dir.glob("*.jsonl"):
        if f.name.startswith("_"):
            continue
        for line in f.read_text().splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                events.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    events.sort(key=lambda e: e.get("ts", ""))
    return events


def _compute_velocity(events: list[dict], now: datetime, weeks: int = 8) -> list[dict]:
    """Compute tasks completed per week for the last N weeks.

    Returns list of {week_label, count} dicts, oldest first.
    """
    # Find status_changed events where to == "done"
    done_events: list[datetime] = []
    for ev in events:
        if ev.get("type") == "status_changed" and ev.get("data", {}).get("to") == "done":
            dt = parse_ts(ev.get("ts", ""))
            if dt:
                done_events.append(dt)

    # Bucket by ISO week
    cutoff = now - timedelta(weeks=weeks)
    buckets: Counter = Counter()
    for dt in done_events:
        if dt >= cutoff:
            # Use ISO year-week as key
            iso_year, iso_week, _ = dt.isocalendar()
            buckets[f"{iso_year}-W{iso_week:02d}"] = (
                buckets.get(f"{iso_year}-W{iso_week:02d}", 0) + 1
            )

    # Build ordered list for the last N weeks
    result: list[dict] = []
    for i in range(weeks - 1, -1, -1):
        week_start = now - timedelta(weeks=i)
        iso_year, iso_week, _ = week_start.isocalendar()
        label = f"{iso_year}-W{iso_week:02d}"
        result.append({"week": label, "count": buckets.get(label, 0)})

    return result


def _compute_time_in_status(events: list[dict], now: datetime) -> list[dict]:
    """Compute average time spent in each status across all tasks.

    Returns list of {status, avg_hours, sample_count} dicts, sorted by avg_hours desc.
    """
    # Group events by task_id
    task_events: dict[str, list[dict]] = defaultdict(list)
    for ev in events:
        tid = ev.get("task_id")
        if tid:
            task_events[tid].append(ev)

    # For each task, walk status transitions and accumulate durations
    status_durations: defaultdict[str, list[float]] = defaultdict(list)  # status -> list of hours

    for tid, tevs in task_events.items():
        tevs.sort(key=lambda e: e.get("ts", ""))

        # Find initial status from task_created event
        current_status = None
        current_ts = None

        for ev in tevs:
            etype = ev.get("type")
            ts = parse_ts(ev.get("ts", ""))
            if ts is None:
                continue

            if etype == "task_created":
                current_status = ev.get("data", {}).get("status", "backlog")
                current_ts = ts
            elif etype == "status_changed":
                if current_status and current_ts:
                    hours = (ts - current_ts).total_seconds() / 3600
                    status_durations[current_status].append(hours)
                current_status = ev.get("data", {}).get("to")
                current_ts = ts

        # Account for time in current status up to now
        if current_status and current_ts:
            hours = (now - current_ts).total_seconds() / 3600
            status_durations[current_status].append(hours)

    # Compute averages
    result: list[dict] = []
    for status, durations in status_durations.items():
        avg = sum(durations) / len(durations)
        result.append(
            {
                "status": status,
                "avg_hours": round(avg, 1),
                "sample_count": len(durations),
            }
        )

    result.sort(key=lambda r: r["avg_hours"], reverse=True)
    return result


def _compute_alert_episode_counts(events: list[dict], active: list[dict]) -> dict:
    """Compute alert-episode metrics (LAT-210 replacement for blocked counts).

    Walks ``alert_raised`` / ``alert_cleared`` event pairs.  Returns a
    summary block plus per-alert-name breakdowns.
    """
    # Per-alert tracking.
    open_at: dict[tuple[str, str], datetime] = {}  # (task_id, alert_name) -> raised ts
    durations_by_name: dict[str, list[float]] = {}  # alert_name -> hours per closed episode

    for ev in events:
        et = ev.get("type")
        if et not in ("alert_raised", "alert_cleared"):
            continue
        data = ev.get("data") or {}
        name = data.get("name")
        tid = ev.get("task_id", "")
        ts = parse_ts(ev.get("ts", ""))
        if not name or not ts:
            continue
        key = (tid, name)
        if et == "alert_raised":
            # Replace any existing open episode (double-raise overwrites raised_at).
            open_at[key] = ts
        else:  # alert_cleared
            raised_at = open_at.pop(key, None)
            if raised_at is not None:
                hours = (ts - raised_at).total_seconds() / 3600
                durations_by_name.setdefault(name, []).append(hours)

    # Active counts come from snapshots (the source of truth post-replay).
    currently_alerted = 0
    by_name_active: dict[str, int] = {}
    for snap in active:
        alerts = snap.get("alerts") or {}
        if alerts:
            currently_alerted += 1
        for alert_name in alerts:
            by_name_active[alert_name] = by_name_active.get(alert_name, 0) + 1

    # Per-alert summary
    by_alert: dict[str, dict] = {}
    all_names = set(durations_by_name) | set(by_name_active) | {n for (_t, n) in open_at}
    for name in sorted(all_names):
        durations = durations_by_name.get(name, [])
        still_open = sum(1 for k in open_at if k[1] == name)
        total_episodes = len(durations) + still_open
        avg_hours = round(sum(durations) / len(durations), 1) if durations else 0
        by_alert[name] = {
            "currently_alerted": by_name_active.get(name, 0),
            "total_alert_episodes": total_episodes,
            "avg_alert_hours": avg_hours,
        }

    total_episodes = sum(b["total_alert_episodes"] for b in by_alert.values())
    all_durations = [d for ds in durations_by_name.values() for d in ds]
    overall_avg = (
        round(sum(all_durations) / len(all_durations), 1) if all_durations else 0
    )

    return {
        "currently_alerted": currently_alerted,
        "total_alert_episodes": total_episodes,
        "avg_alert_hours": overall_avg,
        "by_alert": by_alert,
    }


def _compute_agent_activity(events: list[dict]) -> list[dict]:
    """Compute event counts per actor.

    Returns list of {actor, event_count} dicts, sorted by count desc. Top 10.
    """
    from lattice.core.events import get_actor_display

    actor_counts: Counter = Counter()
    for ev in events:
        actor = ev.get("actor")
        if actor:
            actor_counts[get_actor_display(actor)] += 1

    return [
        {"actor": actor, "event_count": count} for actor, count in actor_counts.most_common(10)
    ]


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
        raw_assignee = snap.get("assigned_to")
        if raw_assignee:
            from lattice.core.events import get_actor_display

            assignee_counts[get_actor_display(raw_assignee)] += 1
        else:
            assignee_counts["unassigned"] += 1
        for tag in snap.get("tags") or []:
            tag_counts[tag] += 1

    # --- Staleness (active tasks only) ---
    stale: list[dict] = []  # tasks not updated in 7+ days
    recently_active: list[dict] = []

    for snap in active:
        updated_at = snap.get("updated_at", "")
        d = days_ago(updated_at, now)
        if d is not None and d >= 7:
            stale.append(
                {
                    "id": snap.get("short_id") or snap.get("id", "?"),
                    "full_id": snap.get("id", ""),
                    "title": snap.get("title", "?"),
                    "status": snap.get("status", "?"),
                    "days_stale": round(d, 1),
                }
            )

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
        recently_active.append(
            {
                "id": snap.get("short_id") or snap.get("id", "?"),
                "full_id": snap.get("id", ""),
                "title": snap.get("title", "?"),
                "status": snap.get("status", "?"),
                "updated_ago": format_days(d) if d is not None else "?",
            }
        )

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
        busiest.append(
            {
                "id": short_id or task_id,
                "full_id": full_id,
                "title": title,
                "event_count": count,
            }
        )

    # --- Workflow config info ---
    workflow = config.get("workflow", {})
    wip_limits = workflow.get("wip_limits", {})
    wip_status: list[dict] = []
    for status_name, limit in wip_limits.items():
        current = status_counts.get(status_name, 0)
        wip_status.append(
            {
                "status": status_name,
                "current": current,
                "limit": limit,
                "over": current > limit,
            }
        )

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

    # --- Quality Metrics (event-derived) ---
    all_events = load_all_events(lattice_dir)
    velocity = _compute_velocity(all_events, now)
    time_in_status = _compute_time_in_status(all_events, now)
    alert_episodes = _compute_alert_episode_counts(all_events, active)
    agent_activity = _compute_agent_activity(all_events)

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
        "velocity": velocity,
        "time_in_status": time_in_status,
        "alert_episodes": alert_episodes,
        "agent_activity": agent_activity,
    }
