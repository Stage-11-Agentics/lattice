"""``task.link``: the ``lattice link`` command's rules."""

from __future__ import annotations

from dataclasses import dataclass

from lattice.core.relationships import RELATIONSHIP_TYPES, validate_relationship_type
from lattice.ops.base import CommonParams, OpContext, OpError, OpResult, operation
from lattice.storage.operations import TaskMutationDecision, read_task_authority


def check_relationship_type(rel_type: str) -> None:
    if not validate_relationship_type(rel_type):
        sorted_types = ", ".join(sorted(RELATIONSHIP_TYPES))
        raise OpError(
            "VALIDATION_ERROR",
            f"Invalid relationship type: '{rel_type}'. Valid types: {sorted_types}.",
        )


@dataclass(frozen=True, kw_only=True)
class LinkParams(CommonParams):
    task: str
    type: str
    target_task: str
    note: str | None = None


@operation("task.link")
class Link:
    Params = LinkParams

    def run(self, ctx: OpContext, p: LinkParams) -> OpResult:
        task_id = ctx.resolve_task(p.task)
        target_task_id = ctx.resolve_task(p.target_task)
        check_relationship_type(p.type)
        if task_id == target_task_id:
            raise OpError(
                "VALIDATION_ERROR", "Cannot create a relationship from a task to itself."
            )
        ctx.require_active(task_id)
        target = read_task_authority(ctx.lattice_dir, target_task_id, allow_missing=True)
        if target is None or target.location != "active":
            raise OpError("NOT_FOUND", f"Target task {target_task_id} not found.")

        data: dict = {"type": p.type, "target_task_id": target_task_id}
        if p.note is not None:
            data["note"] = p.note

        def decide(context):  # noqa: ANN001, ANN202
            for relationship in context.snapshot.get("relationships_out", []):
                if (
                    relationship["type"] == p.type
                    and relationship["target_task_id"] == target_task_id
                ):
                    raise OpError.task_state(
                        "CONFLICT",
                        f"Duplicate: {p.type} relationship to {target_task_id} already exists.",
                        context.snapshot,
                    )
            return TaskMutationDecision(events=[ctx.event("relationship_added", task_id, data, p)])

        result = ctx.mutate(task_id, decide)
        return OpResult(task=result.snapshot, events=result.appended_events, value=result.snapshot)
