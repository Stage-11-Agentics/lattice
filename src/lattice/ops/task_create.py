"""``task.create``: the ``lattice create`` command's rules."""

from __future__ import annotations

from dataclasses import dataclass

from lattice.core.config import (
    VALID_COMPLEXITIES,
    VALID_PRIORITIES,
    VALID_URGENCIES,
    configured_event_prefix,
    validate_status,
    validate_task_type,
)
from lattice.core.ids import generate_task_id, validate_actor, validate_id
from lattice.ops.base import CommonParams, OpContext, OpError, OpResult, operation
from lattice.storage.operations import TaskMutationDecision, scaffold_plan

# A repeated create with the same --id is idempotent when these match.
_CREATE_COMPARE_FIELDS = (
    "title",
    "type",
    "priority",
    "urgency",
    "complexity",
    "status",
    "description",
    "tags",
    "assigned_to",
)


@dataclass(frozen=True, kw_only=True)
class CreateParams(CommonParams):
    title: str
    type: str | None = None
    priority: str | None = None
    urgency: str | None = None
    complexity: str | None = None
    status: str | None = None
    description: str | None = None
    tags: str | None = None
    tag: tuple[str, ...] = ()
    assigned_to: str | None = None
    id: str | None = None


@operation("task.create")
class Create:
    Params = CreateParams

    def run(self, ctx: OpContext, p: CreateParams) -> OpResult:
        config = ctx.config
        status = p.status if p.status is not None else config.get("default_status", "backlog")
        priority = (
            p.priority if p.priority is not None else config.get("default_priority", "medium")
        )
        task_type = p.type if p.type is not None else "task"

        if not validate_status(config, status):
            valid = ", ".join(config.get("workflow", {}).get("statuses", []))
            raise OpError(
                "VALIDATION_ERROR", f"Invalid status: '{status}'. Valid statuses: {valid}."
            )
        if not validate_task_type(config, task_type):
            valid = ", ".join(config.get("task_types", []))
            raise OpError(
                "VALIDATION_ERROR", f"Invalid task type: '{task_type}'. Valid types: {valid}."
            )
        if priority not in VALID_PRIORITIES:
            valid = ", ".join(VALID_PRIORITIES)
            raise OpError(
                "VALIDATION_ERROR", f"Invalid priority: '{priority}'. Valid priorities: {valid}."
            )
        if p.urgency is not None and p.urgency not in VALID_URGENCIES:
            valid = ", ".join(VALID_URGENCIES)
            raise OpError(
                "VALIDATION_ERROR", f"Invalid urgency: '{p.urgency}'. Valid urgencies: {valid}."
            )
        if p.complexity is not None and p.complexity not in VALID_COMPLEXITIES:
            valid = ", ".join(VALID_COMPLEXITIES)
            raise OpError(
                "VALIDATION_ERROR",
                f"Invalid complexity: '{p.complexity}'. Valid complexities: {valid}.",
            )
        if p.assigned_to is not None and not validate_actor(p.assigned_to):
            raise OpError("INVALID_ACTOR", f"Invalid assigned-to format: '{p.assigned_to}'.")

        # --tag is sugar: its values follow --tags in argv order, no dedupe.
        tag_list = [t.strip() for t in p.tags.split(",") if t.strip()] if p.tags else []
        tag_list += [t.strip() for t in p.tag if t.strip()]

        if p.id is not None:
            if not validate_id(p.id, "task"):
                raise OpError("INVALID_ID", f"Invalid task ID format: '{p.id}'.")
            task_id = p.id
        else:
            task_id = generate_task_id()

        requested: dict = {
            "title": p.title,
            "status": status,
            "type": task_type,
            "priority": priority,
        }
        if p.urgency is not None:
            requested["urgency"] = p.urgency
        if p.complexity is not None:
            requested["complexity"] = p.complexity
        if p.description is not None:
            requested["description"] = p.description
        if tag_list:
            requested["tags"] = tag_list
        if p.assigned_to is not None:
            requested["assigned_to"] = p.assigned_to

        def decide(context):  # noqa: ANN001, ANN202
            if context.snapshot is not None:
                created = context.events[0]["data"]
                existing = {f: created.get(f) for f in _CREATE_COMPARE_FIELDS}
                new = {f: requested.get(f) for f in _CREATE_COMPARE_FIELDS}
                existing["tags"] = existing.get("tags") or []
                new["tags"] = new.get("tags") or []
                if existing != new:
                    raise OpError(
                        "CONFLICT", f"Conflict: task {task_id} exists with different data."
                    )
                return TaskMutationDecision(idempotent=True)
            data = dict(requested)
            if context.reserved_short_id is not None:
                data["short_id"] = context.reserved_short_id
            return TaskMutationDecision(events=[ctx.event("task_created", task_id, data, p)])

        result = ctx.mutate(
            task_id,
            decide,
            source="absent",
            may_emit_lifecycle=True,
            project_prefix=configured_event_prefix(config),
        )
        snapshot = result.snapshot
        scaffold_plan(ctx.lattice_dir, task_id, p.title, snapshot.get("short_id"), p.description)
        return OpResult(
            task=snapshot,
            events=result.appended_events,
            value=snapshot,
            idempotent=result.idempotent,
        )
