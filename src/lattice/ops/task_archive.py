"""``task.archive``: one task of the ``lattice archive`` command.

The command's multi-ID and ``--stale`` forms call this once per task. The
move goes through ``mutate_task``'s placement (append, then copy-then-unlink
into ``archive/``), exactly as before operations.
"""

from __future__ import annotations

from dataclasses import dataclass

from lattice.core.errors import OpError
from lattice.ops.base import CommonParams, OpContext, OpResult, operation
from lattice.storage.operations import (
    AuthoritativeLogError,
    TaskMutationDecision,
    TaskPlacementError,
)

UNRESOLVED_TASK = "UNRESOLVED_TASK"
_NO_LOG = "no authoritative event log exists"


def resolve_placement_task(ctx: OpContext, raw_id: str) -> str:
    """``ctx.resolve_task``, marking its rejection ``details.reason: UNRESOLVED_TASK``
    so a multi-task caller can tell an unknown ID from a failed move."""
    try:
        return ctx.resolve_task(raw_id)
    except OpError as exc:
        raise OpError(exc.code, exc.message, {"reason": UNRESOLVED_TASK}) from exc


def move_task(
    ctx: OpContext,
    task_id: str,
    decide,  # noqa: ANN001
    *,
    destination: str,
    conflict_marker: str,
) -> OpResult:
    """Run a placement mutation, mapping a bare ``ValueError`` as the command always has."""
    try:
        result = ctx.mutate(
            task_id,
            decide,
            source="either",
            destination=destination,
            may_emit_lifecycle=True,
        )
    except OpError:
        raise
    except AuthoritativeLogError as exc:
        # An absent task (no log in either placement) is NOT_FOUND with today's
        # message; any other unreplayable log reaches execute as INTEGRITY_ERROR.
        if isinstance(exc, TaskPlacementError) or not str(exc).endswith(_NO_LOG):
            raise
        raise OpError("NOT_FOUND", str(exc)) from exc
    except ValueError as exc:
        code = "CONFLICT" if conflict_marker in str(exc) else "NOT_FOUND"
        raise OpError(code, str(exc)) from exc
    return OpResult(
        task=result.snapshot,
        events=result.appended_events,
        value=result.callback_value,
        idempotent=result.idempotent,
    )


@dataclass(frozen=True, kw_only=True)
class ArchiveParams(CommonParams):
    task: str


@operation("task.archive")
class Archive:
    """Archive one task. Archiving an archived task is an idempotent no-op.

    ``value`` is the ``task_archived`` event: the new one, or on a no-op the
    one that archived it.
    """

    Params = ArchiveParams

    def run(self, ctx: OpContext, p: ArchiveParams) -> OpResult:
        task_id = resolve_placement_task(ctx, p.task)

        def decide(context):  # noqa: ANN001, ANN202
            if context.location == "archived":
                event = next(e for e in reversed(context.events) if e["type"] == "task_archived")
                return TaskMutationDecision(value=event, idempotent=True)
            event = ctx.event("task_archived", task_id, {}, p)
            return TaskMutationDecision(events=[event], value=event)

        return move_task(
            ctx, task_id, decide, destination="archived", conflict_marker="already archived"
        )
