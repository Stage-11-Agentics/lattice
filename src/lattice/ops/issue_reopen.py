"""``issue.reopen``: the ``lattice issue reopen`` command's rules (LAT-361)."""

from __future__ import annotations

from lattice.ops import issue_common
from lattice.ops.base import OpContext, OpError, OpResult, operation
from lattice.ops.issue_common import IssueParams


@operation("issue.reopen")
class IssueReopen:
    Params = IssueParams

    def run(self, ctx: OpContext, p: IssueParams) -> OpResult:
        issue_common.require_issue_log(ctx)
        issue_id = issue_common.resolve(ctx, p.issue)

        def decide(snapshot: dict) -> tuple[str, dict]:
            if not snapshot.get("closure"):
                raise OpError(
                    "CONFLICT",
                    f"Issue {issue_common.display(snapshot)} is not dismissed or a "
                    "duplicate; there is nothing to reopen.",
                )
            return "issue_reopened", {}

        snapshot, events = issue_common.append(ctx, issue_id, decide, p)
        return issue_common.result(ctx, snapshot, events)
