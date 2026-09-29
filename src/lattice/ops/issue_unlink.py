"""``issue.unlink``: the ``lattice issue unlink`` command's rules (LAT-361)."""

from __future__ import annotations

from dataclasses import dataclass

from lattice.ops import issue_common
from lattice.ops.base import OpContext, OpResult, operation
from lattice.ops.issue_common import IssueParams


@dataclass(frozen=True, kw_only=True)
class IssueUnlinkParams(IssueParams):
    task: str


@operation("issue.unlink")
class IssueUnlink:
    Params = IssueUnlinkParams

    def run(self, ctx: OpContext, p: IssueUnlinkParams) -> OpResult:
        issue_common.require_issue_log(ctx)
        issue_id = issue_common.resolve(ctx, p.issue)
        # Any task, even an erased or missing one: unlinking is how a mistake is undone.
        task_id = ctx.resolve_task(p.task)

        def decide(snapshot: dict) -> tuple[str, dict] | None:
            if not any(link["task_id"] == task_id for link in snapshot["links"]):
                return None
            return "issue_unlinked", {"task_id": task_id}

        snapshot, events = issue_common.append(ctx, issue_id, decide, p)
        return issue_common.result(ctx, snapshot, events)
