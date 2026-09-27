"""``task.assign``: the ``lattice assign`` command's rules."""

from __future__ import annotations

from dataclasses import dataclass

from lattice.core.ids import validate_actor
from lattice.ops.base import CommonParams, OpContext, OpError, OpResult, operation
from lattice.storage.operations import TaskMutationDecision

UNASSIGN_SENTINELS = frozenset({"none", "unassigned", "-"})


@dataclass(frozen=True, kw_only=True)
class AssignParams(CommonParams):
    task: str
    actor_id: str  # an actor, or 'none' / 'unassigned' / '-' to unassign


@operation("task.assign")
class Assign:
    """Set or clear ``assigned_to``. An unchanged assignee is an idempotent no-op
    whose ``value`` is today's ``{"message": ...}``."""

    Params = AssignParams

    def run(self, ctx: OpContext, p: AssignParams) -> OpResult:
        task_id = ctx.resolve_task(p.task)
        is_unassign = p.actor_id.lower() in UNASSIGN_SENTINELS
        target: str | None = None if is_unassign else p.actor_id
        if not is_unassign and not validate_actor(p.actor_id):
            raise OpError(
                "INVALID_ACTOR",
                f"Invalid actor format: '{p.actor_id}'. "
                "Expected prefix:identifier (e.g., human:atin, agent:claude). "
                "Use 'none', 'unassigned', or '-' to unassign.",
            )

        def decide(context):  # noqa: ANN001, ANN202
            current = context.snapshot.get("assigned_to")
            if current == target:
                return TaskMutationDecision(idempotent=True)
            data = {"from": current, "to": target}
            return TaskMutationDecision(events=[ctx.event("assignment_changed", task_id, data, p)])

        result = ctx.mutate(task_id, decide)
        if result.idempotent:
            label = "Already unassigned" if is_unassign else f"Already assigned to {target}"
            value: dict = {"message": label}
        else:
            value = result.snapshot
        return OpResult(
            task=result.snapshot,
            events=result.appended_events,
            value=value,
            idempotent=result.idempotent,
        )
