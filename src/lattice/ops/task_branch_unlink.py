"""``task.branch_unlink``: the ``lattice branch-unlink`` command's rules."""

from __future__ import annotations

from dataclasses import dataclass

from lattice.ops.base import CommonParams, OpContext, OpError, OpResult, operation
from lattice.ops.task_branch_link import check_branch_name, normalized_repo, repo_display
from lattice.storage.operations import TaskMutationDecision


@dataclass(frozen=True, kw_only=True)
class BranchUnlinkParams(CommonParams):
    task: str
    branch: str
    repo: str | None = None

    def check(self) -> None:
        check_branch_name(self.branch)


@operation("task.branch_unlink")
class BranchUnlink:
    Params = BranchUnlinkParams

    def run(self, ctx: OpContext, p: BranchUnlinkParams) -> OpResult:
        repo = normalized_repo(p.repo)
        task_id = ctx.resolve_task(p.task)
        ctx.require_active(task_id)
        data: dict = {"branch": p.branch}
        if repo is not None:
            data["repo"] = repo

        def decide(context):  # noqa: ANN001, ANN202
            found = any(
                link["branch"] == p.branch and link.get("repo") == repo
                for link in context.snapshot.get("branch_links", [])
            )
            if not found:
                raise OpError(
                    "NOT_FOUND", f"No branch link '{p.branch}'{repo_display(repo)} on {task_id}."
                )
            return TaskMutationDecision(events=[ctx.event("branch_unlinked", task_id, data, p)])

        result = ctx.mutate(task_id, decide)
        return OpResult(task=result.snapshot, events=result.appended_events, value=result.snapshot)
