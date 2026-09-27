"""``task.comment_delete``: the ``lattice comment-delete`` command's rules."""

from __future__ import annotations

from dataclasses import dataclass

from lattice.core.comments import validate_comment_for_delete
from lattice.ops.base import CommonParams, OpContext, OpResult, operation
from lattice.ops.value_errors import mutate_mapping_value_errors
from lattice.storage.operations import TaskMutationDecision


@dataclass(frozen=True, kw_only=True)
class CommentDeleteParams(CommonParams):
    task: str
    comment_id: str


@operation("task.comment_delete")
class CommentDelete:
    Params = CommentDeleteParams

    def run(self, ctx: OpContext, p: CommentDeleteParams) -> OpResult:
        task_id = ctx.resolve_task(p.task)

        def decide(context):  # noqa: ANN001, ANN202
            validate_comment_for_delete(list(context.events), p.comment_id)
            data = {"comment_id": p.comment_id}
            return TaskMutationDecision(events=[ctx.event("comment_deleted", task_id, data, p)])

        result = mutate_mapping_value_errors(ctx, task_id, decide, "VALIDATION_ERROR")
        return OpResult(task=result.snapshot, events=result.appended_events, value=result.snapshot)
