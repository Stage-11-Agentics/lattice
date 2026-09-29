"""``issue.dismiss``: the ``lattice issue dismiss`` command's rules (LAT-361)."""

from __future__ import annotations

from dataclasses import dataclass

from lattice.ops import issue_common
from lattice.ops.base import OpContext, OpError, OpResult, operation
from lattice.ops.issue_common import IssueParams


@dataclass(frozen=True, kw_only=True)
class IssueDismissParams(IssueParams):
    def check(self) -> None:
        if self.reason is None or not self.reason.strip():
            raise OpError("VALIDATION_ERROR", "--reason is required.")


@operation("issue.dismiss")
class IssueDismiss:
    Params = IssueDismissParams

    def run(self, ctx: OpContext, p: IssueDismissParams) -> OpResult:
        issue_common.require_issue_log(ctx)
        issue_id = issue_common.resolve(ctx, p.issue)

        def decide(snapshot: dict) -> tuple[str, dict]:
            issue_common.refuse_closed(snapshot, "dismissing it")
            return "issue_dismissed", {"reason": p.reason}

        snapshot, events = issue_common.append(ctx, issue_id, decide, p)
        return issue_common.result(ctx, snapshot, events)
