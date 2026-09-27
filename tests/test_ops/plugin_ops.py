"""A plugin-style operation module, loaded only through a ``lattice.operations``
entry point in tests (never by ``lattice.ops`` discovery)."""

from __future__ import annotations

from dataclasses import dataclass

from lattice.ops.base import CommonParams, OpContext, OpResult, operation
from lattice.storage.operations import TaskMutationDecision


@dataclass(frozen=True, kw_only=True)
class PingParams(CommonParams):
    task: str
    note: str = "ping"


@operation("xplugin.ping")
class Ping:
    Params = PingParams

    def run(self, ctx: OpContext, p: PingParams) -> OpResult:
        task_id = ctx.resolve_task(p.task)
        ctx.require_active(task_id)

        def decide(_context):  # noqa: ANN001, ANN202
            return TaskMutationDecision(
                events=[ctx.event("x_plugin_ping", task_id, {"note": p.note}, p)]
            )

        result = ctx.mutate(task_id, decide)
        return OpResult(task=result.snapshot, events=result.appended_events, value=p.note)
