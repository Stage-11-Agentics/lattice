"""``issue.promote``: the ``lattice issue promote`` command's rules (LAT-361).

Validate every issue, create one task through ``task.create``'s own rules,
then link each issue to it. Locally the three steps are not one transaction:
if a link fails after the task exists, the error names the task and the
issues still to link, and ``lattice issue link`` finishes the job.
"""

from __future__ import annotations

from dataclasses import dataclass

from lattice.core.issues import default_task_title, promote_description
from lattice.ops import issue_common
from lattice.ops.base import CommonParams, OpContext, OpError, OpResult, operation
from lattice.ops.task_create import Create, CreateParams
from lattice.storage.issues import current_issue


@dataclass(frozen=True, kw_only=True)
class IssuePromoteParams(CommonParams):
    issues: tuple[str, ...]
    title: str | None = None
    priority: str | None = None
    type: str | None = None

    def check(self) -> None:
        if not self.issues:
            raise OpError("VALIDATION_ERROR", "Name at least one issue to promote.")
        if self.title is not None and not self.title.strip():
            raise OpError("VALIDATION_ERROR", "--title must not be empty.")


@operation("issue.promote")
class IssuePromote:
    Params = IssuePromoteParams

    def run(self, ctx: OpContext, p: IssuePromoteParams) -> OpResult:
        issue_common.require_issue_log(ctx)
        issue_ids = list(dict.fromkeys(issue_common.resolve(ctx, raw) for raw in p.issues))
        snapshots = []
        for issue_id in issue_ids:
            snapshot = current_issue(ctx.lattice_dir, issue_id)
            if snapshot is None:
                raise OpError("NOT_FOUND", f"Issue {issue_id} not found.")
            issue_common.refuse_closed(snapshot, "promoting it")
            snapshots.append(snapshot)

        created = Create().run(
            ctx,
            CreateParams(
                title=p.title or default_task_title(snapshots[0]),
                priority=p.priority,
                type=p.type,
                description=promote_description(snapshots),
                **p.provenance(),
            ),
        )
        task = created.task
        events = list(created.events)
        linked: list[str] = []
        final: list[dict] = []
        for issue_id in issue_ids:
            try:
                snapshot, link_events = issue_common.link_one(ctx, issue_id, task["id"], p)
            except OpError as exc:
                raise _partial(exc, task, linked, snapshots) from exc
            linked.append(issue_id)
            events += link_events
            final.append(snapshot)

        views = [issue_common.view(ctx, s) for s in final]
        return OpResult(task=task, events=events, value={"task": task, "issues": views})


def _partial(exc: OpError, task: dict, linked: list[str], snapshots: list[dict]) -> OpError:
    name = task.get("short_id") or task["id"]
    missing = [s for s in snapshots if s["id"] not in linked]
    not_linked = [s["id"] for s in missing]
    names = ", ".join(issue_common.display(s) for s in missing)
    return OpError(
        exc.code,
        f"Created task {name}, but could not link {names}: {exc.message} "
        f"Run 'lattice issue link <issue> {name}' for each to finish.",
        {
            "created_task": {"id": task["id"], "short_id": task.get("short_id")},
            "linked": linked,
            "not_linked": not_linked,
        },
    )
