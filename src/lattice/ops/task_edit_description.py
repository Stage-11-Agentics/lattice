"""``task.edit_description``: the ``lattice edit-description`` command's rules."""

from __future__ import annotations

from dataclasses import dataclass

from lattice.ops.base import CommonParams, OpContext, OpResult, operation
from lattice.storage.operations import TaskMutationDecision


@dataclass(frozen=True, kw_only=True)
class EditDescriptionParams(CommonParams):
    task: str
    description: str


@operation("task.edit_description")
class EditDescription:
    """Replace the description (sugar over ``task.update description=...``)."""

    Params = EditDescriptionParams

    def run(self, ctx: OpContext, p: EditDescriptionParams) -> OpResult:
        task_id = ctx.resolve_task(p.task)

        def decide(context):  # noqa: ANN001, ANN202
            old_value = context.snapshot.get("description")
            if old_value == p.description:
                return TaskMutationDecision(idempotent=True)
            data = {"field": "description", "from": old_value, "to": p.description}
            return TaskMutationDecision(events=[ctx.event("field_updated", task_id, data, p)])

        result = ctx.mutate(task_id, decide)
        return OpResult(
            task=result.snapshot,
            events=result.appended_events,
            value={"message": "No changes"} if result.idempotent else result.snapshot,
            idempotent=result.idempotent,
        )
