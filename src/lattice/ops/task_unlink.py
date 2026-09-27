"""``task.unlink``: the ``lattice unlink`` command's rules."""

from __future__ import annotations

from dataclasses import dataclass

from lattice.ops.base import CommonParams, OpContext, OpError, OpResult, operation
from lattice.ops.task_link import check_relationship_type
from lattice.storage.operations import TaskMutationDecision


@dataclass(frozen=True, kw_only=True)
class UnlinkParams(CommonParams):
    task: str
    type: str
    target_task: str


@operation("task.unlink")
class Unlink:
    Params = UnlinkParams

    def run(self, ctx: OpContext, p: UnlinkParams) -> OpResult:
        task_id = ctx.resolve_task(p.task)
        target_task_id = ctx.resolve_task(p.target_task)
        check_relationship_type(p.type)
        ctx.require_active(task_id)
        data = {"type": p.type, "target_task_id": target_task_id}

        def decide(context):  # noqa: ANN001, ANN202
            found = any(
                relationship["type"] == p.type and relationship["target_task_id"] == target_task_id
                for relationship in context.snapshot.get("relationships_out", [])
            )
            if not found:
                raise OpError("NOT_FOUND", f"No {p.type} relationship to {target_task_id}.")
            return TaskMutationDecision(
                events=[ctx.event("relationship_removed", task_id, data, p)]
            )

        result = ctx.mutate(task_id, decide)
        return OpResult(task=result.snapshot, events=result.appended_events, value=result.snapshot)
