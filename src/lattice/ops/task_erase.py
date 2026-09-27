"""``task.erase``: hide a task from every default view with a tombstone (SPEC §7).

Erasing appends ``task_tombstoned`` and removes nothing: the task's log,
snapshot, plan, notes, and artifacts stay where they are, and
``task.unerase`` reverses it.
"""

from __future__ import annotations

from dataclasses import dataclass

from lattice.ops.base import CommonParams, OpContext, OpError, OpResult, operation
from lattice.storage.operations import TaskMutationDecision


def check_reason(reason: str | None) -> None:
    if reason is None or not reason.strip():
        raise OpError("VALIDATION_ERROR", "--reason is required.")


@dataclass(frozen=True, kw_only=True)
class EraseParams(CommonParams):
    task: str

    def check(self) -> None:
        check_reason(self.reason)


@operation("task.erase")
class Erase:
    Params = EraseParams

    def run(self, ctx: OpContext, p: EraseParams) -> OpResult:
        task_id = ctx.resolve_task(p.task)
        ctx.require_active(task_id)

        def decide(_context):  # noqa: ANN001, ANN202
            event = ctx.event("task_tombstoned", task_id, {"reason": p.reason}, p)
            return TaskMutationDecision(events=[event])

        result = ctx.mutate(task_id, decide)
        return OpResult(task=result.snapshot, events=result.appended_events, value=result.snapshot)
