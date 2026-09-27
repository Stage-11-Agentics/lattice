"""``task.unclaim``: the ``lattice unclaim`` command's rules."""

from __future__ import annotations

from dataclasses import dataclass

from lattice.ops.base import CommonParams, OpContext, OpResult, operation
from lattice.storage.operations import TaskMutationDecision


@dataclass(frozen=True, kw_only=True)
class UnclaimParams(CommonParams):
    task: str


@operation("task.unclaim")
class Unclaim:
    """Record ``surface_unbound``, bound or not, as today.

    ``value`` is today's ``--json`` data object; ``surface`` is the surface the
    task was bound to (``None`` if it was not bound).
    """

    Params = UnclaimParams

    def run(self, ctx: OpContext, p: UnclaimParams) -> OpResult:
        task_id = ctx.resolve_task(p.task)
        ctx.require_active(task_id)

        def decide(context):  # noqa: ANN001, ANN202
            old_surface = context.snapshot.get("c11_surface")
            event = ctx.event("surface_unbound", task_id, {"surface": old_surface}, p)
            return TaskMutationDecision(events=[event], value=old_surface)

        result = ctx.mutate(task_id, decide)
        return OpResult(
            task=result.snapshot,
            events=result.appended_events,
            value={
                "task_id": task_id,
                "short_id": result.snapshot.get("short_id") or task_id,
                "surface": result.callback_value,
            },
        )
