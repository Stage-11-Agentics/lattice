"""``issue.link``: the ``lattice issue link`` command's rules (LAT-361)."""

from __future__ import annotations

from dataclasses import dataclass

from lattice.ops import issue_common
from lattice.ops.base import OpContext, OpResult, operation
from lattice.ops.issue_common import IssueParams


@dataclass(frozen=True, kw_only=True)
class IssueLinkParams(IssueParams):
    task: str


@operation("issue.link")
class IssueLink:
    Params = IssueLinkParams

    def run(self, ctx: OpContext, p: IssueLinkParams) -> OpResult:
        issue_common.require_issue_log(ctx)
        issue_id = issue_common.resolve(ctx, p.issue)
        task = issue_common.linkable_task(ctx, p.task)
        snapshot, events = issue_common.link_one(ctx, issue_id, task["id"], p)
        return issue_common.result(ctx, snapshot, events)
