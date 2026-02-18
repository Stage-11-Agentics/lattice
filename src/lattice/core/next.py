"""Pure selection logic for `lattice next` — pick the highest-priority ready task."""

from __future__ import annotations

from lattice.core.events import get_actor_display

# Priority and urgency sort orders (lower number = higher priority)
PRIORITY_ORDER = {"critical": 0, "high": 1, "medium": 2, "low": 3}
URGENCY_ORDER = {"immediate": 0, "high": 1, "normal": 2, "low": 3}

# Statuses that are NOT eligible for next (terminal, waiting, or active)
EXCLUDED_STATUSES = frozenset({"needs_human", "blocked", "done", "cancelled"})

# Statuses indicating work already in progress (for resume-first logic)
RESUME_STATUSES = frozenset({"in_progress", "in_planning"})

# Default statuses considered "ready to pick up"
DEFAULT_READY_STATUSES = frozenset({"backlog", "planned"})


def _actors_match(assigned: str | dict | None, actor: str | dict | None) -> bool:
    """Check if an assigned_to value matches a requesting actor.

    Handles both legacy string actors and structured dict actors.
    Comparison is by display name (``get_actor_display``), which is
    the ``name`` field for structured actors or the string itself for
    legacy actors.
    """
    if assigned is None or actor is None:
        return assigned is None and actor is None
    return get_actor_display(assigned) == get_actor_display(actor)


def select_next(
    snapshots: list[dict],
    *,
    actor: str | dict | None = None,
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
        ready_statuses = DEFAULT_READY_STATUSES

    # Filter out epics — they are containers, not actionable work items
    snapshots = [s for s in snapshots if s.get("type") != "epic"]

    # Step 1: Resume interrupted work
    if actor:
        resume_candidates = []
        for snap in snapshots:
            status = snap.get("status", "")
            assigned = snap.get("assigned_to")
            if status in RESUME_STATUSES and _actors_match(assigned, actor):
                resume_candidates.append(snap)
        if resume_candidates:
            resume_candidates.sort(key=sort_key)
            return resume_candidates[0]

    # Step 2: Pick from ready pool
    candidates = []
    for snap in snapshots:
        status = snap.get("status", "")
        if status not in ready_statuses:
            continue
        # Defensive: if caller passes custom ready_statuses that include
        # terminal/waiting states, still exclude them.
        if status in EXCLUDED_STATUSES:
            continue
        assigned = snap.get("assigned_to")
        if assigned is not None and actor is not None and not _actors_match(assigned, actor):
            continue  # assigned to someone else
        if assigned is not None and actor is None:
            continue  # assigned but no actor specified
        candidates.append(snap)

    if not candidates:
        return None

    candidates.sort(key=sort_key)
    return candidates[0]


def select_all_ready(
    snapshots: list[dict],
    *,
    ready_statuses: frozenset[str] | None = None,
) -> list[dict]:
    """Return all ready tasks sorted by priority, for display purposes.

    Unlike select_next, this returns the full sorted list (not just top-1)
    and does not filter by actor assignment. Used by weather/display code.
    """
    if ready_statuses is None:
        ready_statuses = DEFAULT_READY_STATUSES

    candidates = []
    for snap in snapshots:
        # Epics are containers, not actionable work items
        if snap.get("type") == "epic":
            continue
        status = snap.get("status", "")
        if status not in ready_statuses:
            continue
        if status in EXCLUDED_STATUSES:
            continue
        candidates.append(snap)

    candidates.sort(key=sort_key)
    return candidates


def sort_key(snap: dict) -> tuple[int, int, str]:
    """Return a sort key: (priority_rank, urgency_rank, id).

    Lower values sort first (higher priority).
    """
    pri = snap.get("priority", "medium")
    urg = snap.get("urgency", "normal")
    task_id = snap.get("id", "")
    return (
        PRIORITY_ORDER.get(pri, 99),
        URGENCY_ORDER.get(urg, 99),
        task_id,
    )


def compute_claim_transitions(
    current_status: str,
    target_status: str,
    transitions: dict[str, list[str]],
) -> list[str] | None:
    """Compute the shortest valid transition path from current to target status.

    Returns a list of intermediate statuses (excluding current, including target),
    or None if no valid path exists within 3 hops.

    This is used by --claim to emit valid intermediate status_changed events
    rather than bypassing workflow validation.
    """
    if current_status == target_status:
        return []

    # BFS for shortest path (max 3 hops to prevent runaway)
    max_depth = 3
    queue: list[tuple[str, list[str]]] = [(current_status, [])]
    visited: set[str] = {current_status}

    while queue:
        state, path = queue.pop(0)
        if len(path) >= max_depth:
            continue
        for next_state in transitions.get(state, []):
            if next_state in visited:
                continue
            new_path = path + [next_state]
            if next_state == target_status:
                return new_path
            visited.add(next_state)
            queue.append((next_state, new_path))

    return None
