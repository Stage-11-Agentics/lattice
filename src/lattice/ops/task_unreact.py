"""``task.unreact``: the ``lattice unreact`` command's rules."""

from __future__ import annotations

from dataclasses import dataclass

from lattice.core.comments import validate_comment_for_react
from lattice.ops.base import CommonParams, OpContext, OpResult, operation
from lattice.ops.task_react import check_emoji, has_reaction
from lattice.ops.value_errors import mutate_mapping_value_errors
from lattice.storage.operations import TaskMutationDecision


@dataclass(frozen=True, kw_only=True)
class UnreactParams(CommonParams):
    task: str
    comment_id: str
    emoji: str


@operation("task.unreact")
class Unreact:
    Params = UnreactParams

    def run(self, ctx: OpContext, p: UnreactParams) -> OpResult:
        task_id = ctx.resolve_task(p.task)
        check_emoji(p.emoji)

        def decide(context):  # noqa: ANN001, ANN202
            events = list(context.events)
            validate_comment_for_react(events, p.comment_id)
            if not has_reaction(events, p.comment_id, p.emoji, ctx.actor):
                raise ValueError(
                    f"Reaction :{p.emoji}: by {ctx.actor} not found on comment {p.comment_id}."
                )
            data = {"comment_id": p.comment_id, "emoji": p.emoji}
            return TaskMutationDecision(events=[ctx.event("reaction_removed", task_id, data, p)])

        # Today's mapping: every ValueError from the write is NOT_FOUND.
        result = mutate_mapping_value_errors(ctx, task_id, decide, "NOT_FOUND")
        return OpResult(task=result.snapshot, events=result.appended_events, value=result.snapshot)
