"""``task.record_auto_review``: the ``auto_review_spawned`` audit event.

The client that made a transition spawns the review, then records the spawn
with this operation as ``agent:lattice-auto-review`` (SPEC §3.1, §3.4).
"""

from __future__ import annotations

from dataclasses import dataclass

from lattice.ops.base import OpContext, OpResult, operation
from lattice.storage.operations import TaskMutationDecision


@dataclass(frozen=True, kw_only=True)
class RecordAutoReviewParams:
    task: str
    review_type: str
    mode: str
    log_path: str
    spawned_at: str
    pid: int
    trigger_status_event_id: str
    reviewed_worktree: str | None = None


@operation("task.record_auto_review")
class RecordAutoReview:
    Params = RecordAutoReviewParams

    def run(self, ctx: OpContext, p: RecordAutoReviewParams) -> OpResult:
        from lattice.core.events import create_event

        task_id = ctx.resolve_task(p.task)
        data: dict = {
            "review_type": p.review_type,
            "mode": p.mode,
            "log_path": p.log_path,
            "spawned_at": p.spawned_at,
            # PIDs are short-lived debug aids; the durable signal is
            # ``log_path`` plus the eventual review artifact.
            "pid": p.pid,
            "trigger_status_event_id": p.trigger_status_event_id,
        }
        if p.reviewed_worktree is not None:
            data["reviewed_worktree"] = p.reviewed_worktree

        def decide(_context):  # noqa: ANN001, ANN202
            return TaskMutationDecision(
                events=[create_event("auto_review_spawned", task_id, ctx.actor, data)]
            )

        result = ctx.mutate(task_id, decide)
        return OpResult(task=result.snapshot, events=result.appended_events, value=result.snapshot)
