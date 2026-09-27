"""``task.comment``: the ``lattice comment`` command's rules."""

from __future__ import annotations

from dataclasses import dataclass

from lattice.core.acceptance_criteria import normalize_criterion_ids
from lattice.core.comments import validate_comment_body, validate_comment_for_reply
from lattice.core.config import get_configured_roles
from lattice.ops.base import CommonParams, OpContext, OpError, OpResult, operation
from lattice.storage.operations import TaskMutationDecision
from lattice.storage.readers import read_task_events


@dataclass(frozen=True, kw_only=True)
class CommentParams(CommonParams):
    task: str
    text: str | None = None
    file: str | None = None  # the text of --file PATH
    reply_to: str | None = None
    role: str | None = None
    criterion: tuple[str, ...] = ()

    def check(self) -> None:
        if self.text is not None and self.file is not None:
            raise OpError("VALIDATION_ERROR", "Provide either TEXT or --file, not both.")
        if self.text is None and self.file is None:
            raise OpError("VALIDATION_ERROR", "Provide comment text as TEXT or via --file.")


@operation("task.comment")
class Comment:
    Params = CommentParams

    def run(self, ctx: OpContext, p: CommentParams) -> OpResult:
        text = p.text if p.text is not None else p.file
        assert text is not None
        task_id = ctx.resolve_task(p.task)
        ctx.require_active(task_id)

        if p.reply_to is not None:
            try:
                validate_comment_for_reply(read_task_events(ctx.lattice_dir, task_id), p.reply_to)
            except ValueError as exc:
                raise OpError("VALIDATION_ERROR", str(exc)) from exc
        try:
            body = validate_comment_body(text)
        except ValueError as exc:
            raise OpError("VALIDATION_ERROR", str(exc)) from exc
        if p.role is not None:
            configured_roles = get_configured_roles(ctx.config)
            if configured_roles and p.role not in configured_roles:
                raise OpError(
                    "INVALID_ROLE",
                    f"Unknown role: '{p.role}'. "
                    f"Valid roles: {', '.join(sorted(configured_roles))}.",
                )

        def decide(context):  # noqa: ANN001, ANN202
            try:
                criterion_ids = normalize_criterion_ids(p.criterion, snapshot=context.snapshot)
                data: dict = {"body": body}
                if p.reply_to is not None:
                    validate_comment_for_reply(list(context.events), p.reply_to)
                    data["parent_id"] = p.reply_to
            except ValueError as exc:
                raise OpError("VALIDATION_ERROR", str(exc)) from exc
            if p.role is not None:
                data["role"] = p.role
            if criterion_ids:
                data["criterion_ids"] = criterion_ids
            return TaskMutationDecision(events=[ctx.event("comment_added", task_id, data, p)])

        result = ctx.mutate(task_id, decide)
        return OpResult(task=result.snapshot, events=result.appended_events, value=result.snapshot)
