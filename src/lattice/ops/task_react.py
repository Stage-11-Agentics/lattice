"""``task.react``: the ``lattice react`` command's rules."""

from __future__ import annotations

from dataclasses import dataclass

from lattice.core.comments import materialize_comments, validate_comment_for_react, validate_emoji
from lattice.ops.base import CommonParams, OpContext, OpError, OpResult, operation
from lattice.ops.value_errors import mutate_mapping_value_errors
from lattice.storage.operations import TaskMutationDecision


def check_emoji(emoji: str) -> None:
    if not validate_emoji(emoji):
        raise OpError(
            "VALIDATION_ERROR",
            f"Invalid emoji: '{emoji}'. Must be 1-50 alphanumeric, underscore, or hyphen "
            "characters.",
        )


def has_reaction(events: list[dict], comment_id: str, emoji: str, actor: object) -> bool:
    """Whether *actor* already reacted *emoji* to the comment (top level or reply)."""
    for comment in materialize_comments(events):
        for candidate in (comment, *comment.get("replies", [])):
            if candidate["id"] == comment_id and actor in candidate.get("reactions", {}).get(
                emoji, []
            ):
                return True
    return False


@dataclass(frozen=True, kw_only=True)
class ReactParams(CommonParams):
    task: str
    comment_id: str
    emoji: str


@operation("task.react")
class React:
    Params = ReactParams

    def run(self, ctx: OpContext, p: ReactParams) -> OpResult:
        task_id = ctx.resolve_task(p.task)
        check_emoji(p.emoji)

        def decide(context):  # noqa: ANN001, ANN202
            events = list(context.events)
            validate_comment_for_react(events, p.comment_id)
            if has_reaction(events, p.comment_id, p.emoji, ctx.actor):
                return TaskMutationDecision(idempotent=True)
            data = {"comment_id": p.comment_id, "emoji": p.emoji}
            return TaskMutationDecision(events=[ctx.event("reaction_added", task_id, data, p)])

        result = mutate_mapping_value_errors(ctx, task_id, decide, "VALIDATION_ERROR")
        return OpResult(
            task=result.snapshot,
            events=result.appended_events,
            value=result.snapshot,
            idempotent=result.idempotent,
        )
