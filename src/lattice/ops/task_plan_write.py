"""``task.plan_write``: ``lattice plan write`` (SPEC §3.9)."""

from __future__ import annotations

from dataclasses import dataclass

from lattice.ops.base import CommonParams, OpContext, OpError, OpResult, operation
from lattice.ops.prose_common import ContentParams, write_task_prose


@dataclass(frozen=True, kw_only=True)
class PlanWriteParams(CommonParams, ContentParams):
    """``if_absent``: create the plan only when the task has none, checked under
    the task lock (``CONFLICT``, ``details.reason`` ``ALREADY_EXISTS``, when one
    exists). No CLI option sets it; the dashboard scaffolds a missing plan with
    it, so a plan written meanwhile is never replaced. Default ``False``, so a
    client omits it on the wire (SPEC §15)."""

    task: str
    expect_sha256: str | None = None
    if_absent: bool = False

    what = "the plan"

    def check(self) -> None:
        super().check()
        if self.if_absent and self.expect_sha256 is not None:
            raise OpError("VALIDATION_ERROR", "Give either if_absent or expect_sha256, not both.")


@operation("task.plan_write")
class PlanWrite:
    Params = PlanWriteParams

    def run(self, ctx: OpContext, p: PlanWriteParams) -> OpResult:
        return write_task_prose(ctx, p, "plan")
