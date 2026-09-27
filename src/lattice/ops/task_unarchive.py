"""``task.unarchive``: one task of the ``lattice unarchive`` command."""

from __future__ import annotations

from dataclasses import dataclass

from lattice.ops.base import CommonParams, OpContext, OpResult, operation
from lattice.ops.task_archive import move_task, resolve_placement_task
from lattice.storage.operations import TaskMutationDecision


@dataclass(frozen=True, kw_only=True)
class UnarchiveParams(CommonParams):
    task: str


@operation("task.unarchive")
class Unarchive:
    """Restore one archived task. Unarchiving an active task is an idempotent no-op.

    ``value`` is the ``task_unarchived`` event: the new one, or on a no-op the
    latest one (the ``task_created`` event if it was never archived).
    """

    Params = UnarchiveParams

    def run(self, ctx: OpContext, p: UnarchiveParams) -> OpResult:
        task_id = resolve_placement_task(ctx, p.task)

        def decide(context):  # noqa: ANN001, ANN202
            if context.location == "active":
                event = next(
                    (e for e in reversed(context.events) if e["type"] == "task_unarchived"),
                    context.events[0],
                )
                return TaskMutationDecision(value=event, idempotent=True)
            event = ctx.event("task_unarchived", task_id, {}, p)
            return TaskMutationDecision(events=[event], value=event)

        return move_task(
            ctx, task_id, decide, destination="active", conflict_marker="already active"
        )
