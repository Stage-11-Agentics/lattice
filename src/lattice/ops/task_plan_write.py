"""``task.plan_write``: ``lattice plan write`` (SPEC §3.9)."""

from __future__ import annotations

from dataclasses import dataclass

from lattice.ops.base import CommonParams, OpContext, OpResult, operation
from lattice.ops.prose_common import ContentParams, write_task_prose


@dataclass(frozen=True, kw_only=True)
class PlanWriteParams(CommonParams, ContentParams):
    task: str
    expect_sha256: str | None = None

    what = "the plan"


@operation("task.plan_write")
class PlanWrite:
    Params = PlanWriteParams

    def run(self, ctx: OpContext, p: PlanWriteParams) -> OpResult:
        return write_task_prose(ctx, p, "plan")
