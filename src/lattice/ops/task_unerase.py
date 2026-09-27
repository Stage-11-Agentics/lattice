"""``task.unerase``: lift a task's tombstone (SPEC §7).

Appends ``task_untombstoned``; the tombstone fields leave the snapshot and the
task is back in every view, in the status it had. Both events stay in history.
"""

from __future__ import annotations

from dataclasses import dataclass

from lattice.core.visibility import is_tombstoned
from lattice.ops.base import CommonParams, OpContext, OpError, OpResult, operation
from lattice.ops.task_erase import check_reason
from lattice.storage.operations import TaskMutationDecision


@dataclass(frozen=True, kw_only=True)
class UneraseParams(CommonParams):
    task: str

    def check(self) -> None:
        check_reason(self.reason)


@operation("task.unerase")
class Unerase:
    Params = UneraseParams

    def run(self, ctx: OpContext, p: UneraseParams) -> OpResult:
        task_id = ctx.resolve_task(p.task)
        ctx.require_active(task_id, allow_tombstoned=True)

        def decide(context):  # noqa: ANN001, ANN202
            snapshot = context.snapshot
            if not is_tombstoned(snapshot):
                display = snapshot.get("short_id") or task_id
                raise OpError.task_state("CONFLICT", f"Task {display} is not erased.", snapshot)
            event = ctx.event("task_untombstoned", task_id, {"reason": p.reason}, p)
            return TaskMutationDecision(events=[event])

        result = ctx.mutate(task_id, decide, allow_tombstoned=True)
        return OpResult(task=result.snapshot, events=result.appended_events, value=result.snapshot)
