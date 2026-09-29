"""``issue.duplicate``: the ``lattice issue duplicate`` command's rules (LAT-361)."""

from __future__ import annotations

from dataclasses import dataclass

from lattice.ops import issue_common
from lattice.ops.base import OpContext, OpError, OpResult, operation
from lattice.ops.issue_common import IssueParams
from lattice.storage.issues import current_issue, read_issue_snapshot


@dataclass(frozen=True, kw_only=True)
class IssueDuplicateParams(IssueParams):
    of: str


@operation("issue.duplicate")
class IssueDuplicate:
    Params = IssueDuplicateParams

    def run(self, ctx: OpContext, p: IssueDuplicateParams) -> OpResult:
        issue_common.require_issue_log(ctx)
        issue_id = issue_common.resolve(ctx, p.issue)
        target_id = issue_common.resolve(ctx, p.of)
        if target_id == issue_id:
            raise OpError("VALIDATION_ERROR", "An issue cannot be a duplicate of itself.")
        target = current_issue(ctx.lattice_dir, target_id)
        if target is None:
            raise OpError("NOT_FOUND", f"Issue '{p.of}' not found.")
        closure = target.get("closure") or {}
        if closure.get("kind") == "duplicate":
            original = read_issue_snapshot(ctx.lattice_dir, closure["duplicate_of"]) or {}
            original_name = original.get("short_id") or closure["duplicate_of"]
            raise OpError(
                "VALIDATION_ERROR",
                f"Issue {issue_common.display(target)} is itself a duplicate of "
                f"{original_name}; mark it a duplicate of {original_name} instead.",
                {"duplicate_of": closure["duplicate_of"]},
            )

        def decide(snapshot: dict) -> tuple[str, dict]:
            issue_common.refuse_closed(snapshot, "marking it a duplicate")
            return "issue_marked_duplicate", {"duplicate_of": target_id}

        snapshot, events = issue_common.append(ctx, issue_id, decide, p)
        return issue_common.result(ctx, snapshot, events)
