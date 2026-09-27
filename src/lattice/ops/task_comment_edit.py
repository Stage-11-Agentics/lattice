"""``task.comment_edit``: the ``lattice comment-edit`` command's rules."""

from __future__ import annotations

from dataclasses import dataclass

from lattice.core.comments import validate_comment_body, validate_comment_for_edit
from lattice.core.config import get_configured_roles
from lattice.ops.base import CommonParams, OpContext, OpError, OpResult, operation
from lattice.ops.value_errors import mutate_mapping_value_errors
from lattice.storage.operations import TaskMutationDecision


@dataclass(frozen=True, kw_only=True)
class CommentEditParams(CommonParams):
    task: str
    comment_id: str
    new_text: str | None = None
    file: str | None = None  # the text of --file PATH
    role: str | None = None
    clear_role: bool = False

    def check(self) -> None:
        if self.role is not None and self.clear_role:
            raise OpError("VALIDATION_ERROR", "--role and --clear-role are mutually exclusive.")
        if self.new_text is not None and self.file is not None:
            raise OpError("VALIDATION_ERROR", "Provide either NEW_TEXT or --file, not both.")
        if self.new_text is None and self.file is None:
            raise OpError(
                "VALIDATION_ERROR", "Provide the new comment text as NEW_TEXT or via --file."
            )


@operation("task.comment_edit")
class CommentEdit:
    Params = CommentEditParams

    def run(self, ctx: OpContext, p: CommentEditParams) -> OpResult:
        text = p.new_text if p.new_text is not None else p.file
        assert text is not None
        task_id = ctx.resolve_task(p.task)
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
        role_requested = p.role is not None or p.clear_role
        target_role = None if p.clear_role else p.role

        def decide(context):  # noqa: ANN001, ANN202
            previous_body, previous_role = validate_comment_for_edit(
                list(context.events), p.comment_id
            )
            data: dict = {
                "comment_id": p.comment_id,
                "body": body,
                "previous_body": previous_body,
            }
            if role_requested:
                data["role"] = target_role
                if previous_role != target_role:
                    data["previous_role"] = previous_role
            if previous_body == body and (not role_requested or previous_role == target_role):
                return TaskMutationDecision(idempotent=True)
            return TaskMutationDecision(events=[ctx.event("comment_edited", task_id, data, p)])

        # Today's mapping: every ValueError from the write, a missing or
        # archived task included, is a VALIDATION_ERROR.
        result = mutate_mapping_value_errors(ctx, task_id, decide, "VALIDATION_ERROR")
        return OpResult(
            task=result.snapshot,
            events=result.appended_events,
            value=result.snapshot,
            idempotent=result.idempotent,
        )
