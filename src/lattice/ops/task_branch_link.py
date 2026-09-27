"""``task.branch_link``: the ``lattice branch-link`` command's rules."""

from __future__ import annotations

from dataclasses import dataclass

from lattice.ops.base import CommonParams, OpContext, OpError, OpResult, operation
from lattice.storage.operations import TaskMutationDecision


def check_branch_name(branch: str) -> None:
    """Refuse an empty name, a leading ``-`` (git flag injection), or a control character."""
    if not branch or not branch.strip():
        raise OpError("VALIDATION_ERROR", "Branch name must not be empty or whitespace-only.")
    if branch.startswith("-"):
        raise OpError("VALIDATION_ERROR", f"Branch name must not start with '-': '{branch}'.")
    if any(0 <= ord(c) <= 31 for c in branch):
        raise OpError(
            "VALIDATION_ERROR",
            f"Branch name must not contain control characters: '{branch!r}'.",
        )


def normalized_repo(repo: str | None) -> str | None:
    """An empty or whitespace-only ``--repo`` means no repo."""
    return repo if repo is not None and repo.strip() else None


def repo_display(repo: str | None) -> str:
    return f" (repo: {repo})" if repo else ""


@dataclass(frozen=True, kw_only=True)
class BranchLinkParams(CommonParams):
    task: str
    branch: str
    repo: str | None = None

    def check(self) -> None:
        check_branch_name(self.branch)


@operation("task.branch_link")
class BranchLink:
    Params = BranchLinkParams

    def run(self, ctx: OpContext, p: BranchLinkParams) -> OpResult:
        repo = normalized_repo(p.repo)
        task_id = ctx.resolve_task(p.task)
        ctx.require_active(task_id)
        data: dict = {"branch": p.branch}
        if repo is not None:
            data["repo"] = repo

        def decide(context):  # noqa: ANN001, ANN202
            for link in context.snapshot.get("branch_links", []):
                if link["branch"] == p.branch and link.get("repo") == repo:
                    raise OpError.task_state(
                        "CONFLICT",
                        f"Duplicate: branch '{p.branch}'{repo_display(repo)} "
                        f"already linked to {task_id}.",
                        context.snapshot,
                    )
            return TaskMutationDecision(events=[ctx.event("branch_linked", task_id, data, p)])

        result = ctx.mutate(task_id, decide)
        return OpResult(task=result.snapshot, events=result.appended_events, value=result.snapshot)
