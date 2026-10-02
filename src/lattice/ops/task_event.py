"""``task.event``: the ``lattice event`` command's rules (custom ``x_`` events)."""

from __future__ import annotations

import json
from dataclasses import dataclass

from lattice.core.events import BUILTIN_EVENT_TYPES, create_event, validate_custom_event_type
from lattice.core.ids import validate_id
from lattice.ops.base import CommonParams, OpContext, OpError, OpResult, operation
from lattice.storage.operations import TaskMutationDecision


@dataclass(frozen=True, kw_only=True)
class EventParams(CommonParams):
    task: str
    event_type: str
    data: str | None = None  # --data: a JSON object, as text
    id: str | None = None  # --id: a caller-supplied event ID


@operation("task.event")
class Event:
    """Append one custom event. Re-sending an ``id`` with the same type and data
    is an idempotent no-op; ``value`` is the event either way."""

    Params = EventParams

    def run(self, ctx: OpContext, p: EventParams) -> OpResult:
        task_id = ctx.resolve_task(p.task)
        if p.event_type in BUILTIN_EVENT_TYPES:
            raise OpError(
                "VALIDATION_ERROR",
                f"Event type '{p.event_type}' is reserved. Custom types must start with 'x_'.",
            )
        if not validate_custom_event_type(p.event_type):
            raise OpError(
                "VALIDATION_ERROR",
                f"Invalid custom event type: '{p.event_type}'. Custom types must start with 'x_'.",
            )
        event_data: dict = {}
        if p.data is not None:
            try:
                event_data = json.loads(p.data)
            except json.JSONDecodeError as exc:
                raise OpError("VALIDATION_ERROR", f"Invalid JSON in --data: {exc}") from exc
            if not isinstance(event_data, dict):
                raise OpError("VALIDATION_ERROR", "--data must be a JSON object.")
        ctx.require_active(task_id)
        if p.id is not None and not validate_id(p.id, "ev"):
            raise OpError("INVALID_ID", f"Invalid event ID format: '{p.id}'.")

        def decide(context):  # noqa: ANN001, ANN202
            if p.id is not None:
                for existing in context.events:
                    if existing.get("id") != p.id:
                        continue
                    if existing.get("type") == p.event_type and existing.get("data") == event_data:
                        return TaskMutationDecision(value=existing, idempotent=True)
                    raise OpError(
                        "CONFLICT", f"Conflict: event {p.id} exists with different data."
                    )
            event = create_event(
                p.event_type, task_id, ctx.actor, event_data, event_id=p.id, **p.provenance()
            )
            return TaskMutationDecision(events=[event], value=event)

        result = ctx.mutate(
            task_id,
            decide,
            may_emit_short_id="short_id" in event_data,
        )
        return OpResult(
            task=result.snapshot,
            events=result.appended_events,
            value=result.callback_value,
            idempotent=result.idempotent,
        )
