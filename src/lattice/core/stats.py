"""Shared statistics logic for CLI and dashboard."""

from __future__ import annotations

from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path


def load_all_snapshots(lattice_dir: Path) -> tuple[list[dict], list[dict]]:
    """Load all event-authoritative active and archived task snapshots.

    Returns (active, archived) lists.
    """
    # Local import avoids a module cycle: storage replay depends on core task
    # reducers, while statistics is itself a read-only storage consumer.
    from lattice.storage.operations import discover_task_authorities

    active: list[dict] = []
    archived: list[dict] = []
    for authority in discover_task_authorities(lattice_dir, include_archived=True):
        (archived if authority.location == "archived" else active).append(authority.snapshot)
    return active, archived


def count_events(lattice_dir: Path, archived: bool = False) -> tuple[int, Counter]:
    """Count total events and events per task.

    Returns (total_events, per_task_counter).
    """
    from lattice.storage.operations import discover_task_authorities

    total = 0
    per_task: Counter = Counter()
    expected_location = "archived" if archived else "active"
    for authority in discover_task_authorities(lattice_dir, include_archived=True):
        if authority.location != expected_location:
            continue
        count = len(authority.events)
        total += count
        per_task[authority.task_id] = count

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
    """Load all events from logically active task authorities.

    Returns a flat list of event dicts, sorted by timestamp.
    """
    from lattice.storage.operations import discover_task_authorities

    events = [
        event
        for authority in discover_task_authorities(lattice_dir, include_archived=False)
        for event in authority.events
    ]
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


def _compute_blocked_counts(events: list[dict], active: list[dict]) -> dict:
    """Compute blocked-related metrics.

    Returns {currently_blocked, total_blocked_episodes, avg_blocked_hours}.
    """
    currently_blocked = sum(1 for s in active if s.get("status") == "blocked")

    # Count how many times any task entered "blocked"
    unblocked_times: list[float] = []  # hours spent blocked

    task_blocked_at: dict[str, datetime] = {}  # task_id -> when it entered blocked

    for ev in events:
        if ev.get("type") != "status_changed":
            continue
        data = ev.get("data", {})
        ts = parse_ts(ev.get("ts", ""))
        tid = ev.get("task_id", "")
        if not ts:
            continue

        if data.get("to") == "blocked":
            task_blocked_at[tid] = ts
        elif data.get("from") == "blocked" and tid in task_blocked_at:
            hours = (ts - task_blocked_at[tid]).total_seconds() / 3600
            unblocked_times.append(hours)
            del task_blocked_at[tid]

    total_episodes = len(unblocked_times) + len(task_blocked_at)  # resolved + still blocked
    avg_hours = round(sum(unblocked_times) / len(unblocked_times), 1) if unblocked_times else 0

    return {
        "currently_blocked": currently_blocked,
        "total_blocked_episodes": total_episodes,
        "avg_blocked_hours": avg_hours,
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
    blocked = _compute_blocked_counts(all_events, active)
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
        "blocked": blocked,
        "agent_activity": agent_activity,
    }
