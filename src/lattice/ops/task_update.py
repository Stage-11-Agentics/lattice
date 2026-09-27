"""``task.update``: the ``lattice update`` command's rules."""

from __future__ import annotations

from dataclasses import dataclass

from lattice.core.config import (
    VALID_COMPLEXITIES,
    VALID_PRIORITIES,
    VALID_URGENCIES,
    validate_task_type,
)
from lattice.core.events import create_event, utc_now
from lattice.core.tasks import apply_event_to_snapshot
from lattice.ops.base import CommonParams, OpContext, OpError, OpResult, operation
from lattice.storage.operations import TaskMutationDecision

UPDATABLE_FIELDS = frozenset(
    {"title", "description", "priority", "urgency", "complexity", "type", "tags"}
)

REDIRECT_FIELDS = {
    "status": "Use 'lattice status' to change status.",
    "assigned_to": "Use 'lattice assign' to change assignment.",
}


def _normalize_pairs(pairs: tuple[str, ...], config: dict) -> list[tuple[str, object]]:
    """Parse and validate ``field=value`` pairs in today's order."""
    if not pairs:
        raise OpError("VALIDATION_ERROR", "No field=value pairs provided.")

    parsed: list[tuple[str, str]] = []
    for pair in pairs:
        if "=" not in pair:
            raise OpError(
                "VALIDATION_ERROR",
                f"Invalid field=value pair: '{pair}'. Expected format: field=value.",
            )
        field, value = pair.split("=", 1)
        parsed.append((field, value))

    normalized: list[tuple[str, object]] = []
    for field, value in parsed:
        if field in REDIRECT_FIELDS:
            raise OpError("VALIDATION_ERROR", REDIRECT_FIELDS[field])

        if field.startswith("custom_fields."):
            if not field[len("custom_fields.") :]:
                raise OpError(
                    "VALIDATION_ERROR",
                    "Invalid custom field: 'custom_fields.' requires a key name.",
                )
            normalized.append((field, value))
            continue

        if field not in UPDATABLE_FIELDS:
            valid = ", ".join(sorted(UPDATABLE_FIELDS))
            raise OpError(
                "VALIDATION_ERROR",
                f"Unknown or non-updatable field: '{field}'. "
                f"Updatable fields: {valid}. Use custom_fields.<key> for custom data.",
            )

        if field == "priority" and value not in VALID_PRIORITIES:
            valid = ", ".join(VALID_PRIORITIES)
            raise OpError(
                "VALIDATION_ERROR", f"Invalid priority: '{value}'. Valid priorities: {valid}."
            )
        if field == "urgency" and value not in VALID_URGENCIES:
            valid = ", ".join(VALID_URGENCIES)
            raise OpError(
                "VALIDATION_ERROR", f"Invalid urgency: '{value}'. Valid urgencies: {valid}."
            )
        if field == "complexity" and value not in VALID_COMPLEXITIES:
            valid = ", ".join(VALID_COMPLEXITIES)
            raise OpError(
                "VALIDATION_ERROR",
                f"Invalid complexity: '{value}'. Valid complexities: {valid}.",
            )
        if field == "type" and not validate_task_type(config, value):
            valid = ", ".join(config.get("task_types", []))
            raise OpError(
                "VALIDATION_ERROR", f"Invalid task type: '{value}'. Valid types: {valid}."
            )

        if field == "tags":
            new_value: object = [t.strip() for t in value.split(",") if t.strip()]
        else:
            new_value = value
        normalized.append((field, new_value))
    return normalized


@dataclass(frozen=True, kw_only=True)
class UpdateParams(CommonParams):
    task: str
    pairs: tuple[str, ...] = ()


@operation("task.update")
class Update:
    """Set fields from ``field=value`` pairs; one ``field_updated`` per changed field.

    ``value`` is the updated snapshot, or ``{"message": "No changes"}`` when
    nothing changed (``idempotent``). ``events`` name the fields that changed.
    """

    Params = UpdateParams

    def run(self, ctx: OpContext, p: UpdateParams) -> OpResult:
        task_id = ctx.resolve_task(p.task)
        normalized = _normalize_pairs(p.pairs, ctx.config)
        shared_ts = utc_now()

        def decide(context):  # noqa: ANN001, ANN202
            snapshot = context.snapshot
            events: list[dict] = []
            for field, new_value in normalized:
                if field.startswith("custom_fields."):
                    key = field[len("custom_fields.") :]
                    old_value = (snapshot.get("custom_fields") or {}).get(key)
                else:
                    old_value = snapshot.get(field)
                if (old_value or []) == new_value if field == "tags" else old_value == new_value:
                    continue
                event = create_event(
                    "field_updated",
                    task_id,
                    ctx.actor,
                    {"field": field, "from": old_value, "to": new_value},
                    ts=shared_ts,
                    **p.provenance(),
                )
                events.append(event)
                snapshot = apply_event_to_snapshot(snapshot, event)
            return TaskMutationDecision(events=events, idempotent=not events)

        result = ctx.mutate(task_id, decide)
        return OpResult(
            task=result.snapshot,
            events=result.appended_events,
            value={"message": "No changes"} if result.idempotent else result.snapshot,
            idempotent=result.idempotent,
        )
