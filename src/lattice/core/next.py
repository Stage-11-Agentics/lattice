"""Pure selection logic for `lattice next` — pick the highest-priority ready task."""

from __future__ import annotations


# Priority and urgency sort orders (lower number = higher priority)
_PRIORITY_ORDER = {"critical": 0, "high": 1, "medium": 2, "low": 3}
_URGENCY_ORDER = {"immediate": 0, "high": 1, "normal": 2, "low": 3}

# Statuses that are NOT eligible for next (terminal, waiting, or active)
_EXCLUDED_STATUSES = frozenset({"needs_human", "blocked", "done", "cancelled"})

# Statuses indicating work already in progress (for resume-first logic)
_RESUME_STATUSES = frozenset({"in_progress", "in_planning"})


def select_next(
    snapshots: list[dict],
    *,
    actor: str | None = None,
    ready_statuses: frozenset[str] | None = None,
) -> dict | None:
    """Select the highest-priority task an agent should work on.

    Algorithm:
    1. **Resume first:** If *actor* is specified, check for in_progress/in_planning
       tasks assigned to that actor. Return the highest-priority one.
    2. **Pick from ready pool:** Tasks in *ready_statuses* (default: backlog, planned)
       that are unassigned OR assigned to the requesting actor. Excludes needs_human,
       blocked, done, cancelled.
    3. **Sort by:** priority (critical > high > medium > low) → urgency
       (immediate > high > normal > low) → ULID / id (oldest first).
    4. **Return** top result or None.

    This is pure logic — no filesystem I/O.
    """
    if ready_statuses is None:
        ready_statuses = frozenset({"backlog", "planned"})

    # Step 1: Resume interrupted work
    if actor:
        resume_candidates = []
        for snap in snapshots:
            status = snap.get("status", "")
            assigned = snap.get("assigned_to")
            if status in _RESUME_STATUSES and assigned == actor:
                resume_candidates.append(snap)
        if resume_candidates:
            resume_candidates.sort(key=_sort_key)
            return resume_candidates[0]

    # Step 2: Pick from ready pool
    candidates = []
    for snap in snapshots:
        status = snap.get("status", "")
        if status not in ready_statuses:
            continue
        if status in _EXCLUDED_STATUSES:
            continue
        assigned = snap.get("assigned_to")
        if assigned is not None and actor is not None and assigned != actor:
            continue  # assigned to someone else
        if assigned is not None and actor is None:
            continue  # assigned but no actor specified
        candidates.append(snap)

    if not candidates:
        return None

    candidates.sort(key=_sort_key)
    return candidates[0]


def _sort_key(snap: dict) -> tuple[int, int, str]:
    """Return a sort key: (priority_rank, urgency_rank, id).

    Lower values sort first (higher priority).
    """
    pri = snap.get("priority", "medium")
    urg = snap.get("urgency", "normal")
    task_id = snap.get("id", "")
    return (
        _PRIORITY_ORDER.get(pri, 99),
        _URGENCY_ORDER.get(urg, 99),
        task_id,
    )
