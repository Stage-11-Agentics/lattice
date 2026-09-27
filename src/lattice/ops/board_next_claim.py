"""``board.next_claim``: the ``lattice next --claim`` command's rules.

Selection and the claim run in one call under the board's ``next_claim``
lock, so two callers racing for the next task never receive the same one.
The in-lock guard against a task claimed by another writer (``assign``,
``status``) stays, as before operations. Hooks run after the lock is
released, as every write's hooks do.
"""

from __future__ import annotations

import dataclasses
from dataclasses import dataclass

from lattice.core.events import create_event, get_actor_display
from lattice.core.next import _actors_match, compute_claim_transitions, select_next
from lattice.core.tasks import apply_event_to_snapshot
from lattice.core.visibility import visible
from lattice.ops.base import OpContext, OpError, OpResult, operation
from lattice.ops.plan_gate import check_plan_gate
from lattice.storage.hooks import execute_hooks
from lattice.storage.locks import lattice_lock
from lattice.storage.operations import TaskMutationDecision, discover_task_authorities

NEXT_CLAIM_LOCK = "next_claim"
CLAIMED_STATUSES = frozenset(
    {"in_progress", "review", "in_validation", "pr_open", "done", "cancelled"}
)


@dataclass(frozen=True, kw_only=True)
class NextClaimParams:
    # --status: comma-separated statuses to pick from (default: backlog,planned).
    status: str | None = None


@operation("board.next_claim")
class NextClaim:
    """Pick the caller's next task and claim it: assign it to the caller and walk
    it to ``in_progress``.

    ``value`` is the claimed task's snapshot, or ``None`` when no task is
    available. A task the caller already has in progress is returned with no
    new events (``idempotent``).
    """

    Params = NextClaimParams

    def run(self, ctx: OpContext, p: NextClaimParams) -> OpResult:
        ready: frozenset[str] | None = None
        if p.status is not None:
            ready = frozenset(s.strip() for s in p.status.split(",") if s.strip())
        # Hooks wait until the board lock is released (below).
        inner = dataclasses.replace(ctx, run_hooks=False)
        with lattice_lock(ctx.lattice_dir / "locks", NEXT_CLAIM_LOCK):
            # Erased tasks are never offered, exactly as plain `next` skips them.
            active = visible(
                authority.snapshot
                for authority in discover_task_authorities(ctx.lattice_dir, include_archived=False)
            )
            selected = select_next(active, actor=ctx.actor, ready_statuses=ready)
            if selected is None:
                return OpResult(value=None, idempotent=True)
            task_id = selected["id"]
            check_plan_gate(ctx.lattice_dir, task_id, "in_progress", ctx.config)
            result = inner.mutate(task_id, lambda context: self._decide(ctx, context, task_id))
        if ctx.run_hooks and ctx.config:
            for event in result.appended_events:
                execute_hooks(ctx.config, ctx.lattice_dir, task_id, event)
        return OpResult(
            task=result.snapshot,
            events=result.appended_events,
            value=result.snapshot,
            idempotent=not result.appended_events,
        )

    @staticmethod
    def _decide(ctx: OpContext, context, task_id: str) -> TaskMutationDecision:  # noqa: ANN001
        actor = ctx.actor
        current = snapshot = context.snapshot
        assigned = snapshot.get("assigned_to")
        status = snapshot.get("status", "")
        if assigned is not None and not _actors_match(assigned, actor):
            raise OpError.task_state(
                "ALREADY_CLAIMED",
                f"Task already claimed by {get_actor_display(assigned)}.",
                snapshot,
            )
        if status in CLAIMED_STATUSES and not _actors_match(assigned, actor):
            raise OpError.task_state("ALREADY_CLAIMED", f"Task already in {status}.", snapshot)

        events: list[dict] = []
        if not _actors_match(assigned, actor):
            event = create_event(
                "assignment_changed", task_id, actor, {"from": assigned, "to": actor}
            )
            events.append(event)
            snapshot = apply_event_to_snapshot(snapshot, event)

        status = snapshot.get("status")
        if status != "in_progress":
            transitions = ctx.config.get("workflow", {}).get("transitions", {})
            path = compute_claim_transitions(status, "in_progress", transitions)
            if path is None:
                raise OpError.task_state(
                    "INVALID_TRANSITION",
                    f"No valid transition path from {status} to in_progress.",
                    current,
                )
            for next_status in path:
                event = create_event(
                    "status_changed", task_id, actor, {"from": status, "to": next_status}
                )
                events.append(event)
                snapshot = apply_event_to_snapshot(snapshot, event)
                status = next_status
        return TaskMutationDecision(events=events)
