"""``task.needs_human``: the ``lattice needs-human`` command's rules (set and clear).

The c11 sidebar update is a client-side effect the caller runs after this
returns.
"""

from __future__ import annotations

from dataclasses import dataclass

from lattice.core.events import get_actor_display
from lattice.ops.base import CommonParams, OpContext, OpError, OpResult, operation
from lattice.storage.operations import TaskMutationDecision

REASON_REQUIRED = (
    "REASON is required when setting the needs_human flag. "
    "Say exactly what you need from the human, in one line "
    "(as an argument, or via --file)."
)


@dataclass(frozen=True, kw_only=True)
class NeedsHumanParams(CommonParams):
    task: str
    # The REASON argument. Named apart from the provenance ``reason`` that
    # every write command inherits from ``CommonParams`` (``--reason``).
    flag_reason: str | None = None
    file: str | None = None  # the text of --file PATH
    clear: bool = False
    note: str | None = None


@operation("task.needs_human")
class NeedsHuman:
    """Set the flag with a reason, or clear it (``clear``) with an optional note."""

    Params = NeedsHumanParams

    def run(self, ctx: OpContext, p: NeedsHumanParams) -> OpResult:
        task_id = ctx.resolve_task(p.task)
        ctx.require_active(task_id)
        if p.clear:
            if p.flag_reason is not None or p.file is not None:
                raise OpError(
                    "VALIDATION_ERROR",
                    "REASON / --file is only for setting the flag. To clear, use "
                    "--clear (optionally with --note).",
                )
            return self._clear(ctx, p, task_id)
        if p.note is not None:
            raise OpError(
                "VALIDATION_ERROR",
                "--note is only for clearing the flag. To set, pass a REASON.",
            )
        if p.flag_reason is not None and p.file is not None:
            raise OpError("VALIDATION_ERROR", "Provide either REASON or --file, not both.")
        reason = (p.flag_reason if p.flag_reason is not None else p.file or "").strip()
        if not reason:
            raise OpError("VALIDATION_ERROR", REASON_REQUIRED)
        return self._set(ctx, p, task_id, reason)

    def _set(self, ctx: OpContext, p: NeedsHumanParams, task_id: str, reason: str) -> OpResult:
        def decide(context):  # noqa: ANN001, ANN202
            snapshot = context.snapshot
            current = snapshot.get("needs_human")
            if current:
                display_id = snapshot.get("short_id") or task_id
                flagged_by = get_actor_display(current.get("flagged_by"))
                raise OpError.task_state(
                    "FLAG_ALREADY_SET",
                    f"Task {display_id} already has the needs_human flag set "
                    f"(by {flagged_by} since {current.get('since')}: "
                    f"{current.get('reason')}). Clear it first with --clear.",
                    snapshot,
                )
            data = {"reason": reason}
            return TaskMutationDecision(
                events=[ctx.event("needs_human_flagged", task_id, data, p)]
            )

        result = ctx.mutate(task_id, decide)
        return OpResult(task=result.snapshot, events=result.appended_events, value=result.snapshot)

    def _clear(self, ctx: OpContext, p: NeedsHumanParams, task_id: str) -> OpResult:
        def decide(context):  # noqa: ANN001, ANN202
            snapshot = context.snapshot
            if not snapshot.get("needs_human"):
                display_id = snapshot.get("short_id") or task_id
                raise OpError.task_state(
                    "FLAG_NOT_SET",
                    f"Task {display_id} does not have the needs_human flag set.",
                    snapshot,
                )
            data = {"note": p.note}
            return TaskMutationDecision(
                events=[ctx.event("needs_human_cleared", task_id, data, p)]
            )

        result = ctx.mutate(task_id, decide)
        return OpResult(task=result.snapshot, events=result.appended_events, value=result.snapshot)
