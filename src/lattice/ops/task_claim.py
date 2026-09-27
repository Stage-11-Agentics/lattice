"""``task.claim``: the ``lattice claim`` command's rules.

A claim binds the task to a c11 surface. It is not exclusive: claiming an
already bound task rebinds it. The caller resolves the surface and workspace
from its own environment (``--surface`` or ``C11_SURFACE_ID``) and renames the
c11 tab after this returns.
"""

from __future__ import annotations

from dataclasses import dataclass

from lattice.ops.base import CommonParams, OpContext, OpError, OpResult, operation
from lattice.storage.operations import TaskMutationDecision


@dataclass(frozen=True, kw_only=True)
class ClaimParams(CommonParams):
    task: str
    surface: str | None = None  # --surface, or the caller's C11_SURFACE_ID
    workspace: str | None = None  # the caller's c11 workspace, if any


@operation("task.claim")
class Claim:
    """Record ``surface_bound``. ``value`` is today's ``--json`` data object."""

    Params = ClaimParams

    def run(self, ctx: OpContext, p: ClaimParams) -> OpResult:
        task_id = ctx.resolve_task(p.task)
        ctx.require_active(task_id)
        if not p.surface:
            raise OpError(
                "MISSING_SURFACE",
                "No surface specified. Provide --surface or run inside c11 "
                "(C11_SURFACE_ID must be set).",
            )
        data: dict = {"surface": p.surface}
        if p.workspace:
            data["workspace"] = p.workspace

        def decide(_context):  # noqa: ANN001, ANN202
            return TaskMutationDecision(events=[ctx.event("surface_bound", task_id, data, p)])

        result = ctx.mutate(task_id, decide)
        return OpResult(
            task=result.snapshot,
            events=result.appended_events,
            value={
                "task_id": task_id,
                "short_id": result.snapshot.get("short_id") or task_id,
                "surface": p.surface,
                "workspace": p.workspace,
            },
        )
