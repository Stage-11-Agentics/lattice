"""``task.criterion_add``: the ``lattice criterion add`` command's rules."""

from __future__ import annotations

import copy
from dataclasses import dataclass

from lattice.core.acceptance_criteria import (
    allocate_criterion_id,
    find_criterion,
    normalize_outcome,
    validate_criterion_id,
)
from lattice.ops.base import CommonParams, OpContext, OpError, OpResult, operation
from lattice.ops.value_errors import mutate_mapping_value_errors
from lattice.storage.operations import TaskMutationDecision


def outcome_text(outcome: str | None, file: str | None) -> str:
    """The criterion prose from ``OUTCOME`` or ``--file``: exactly one, trimmed, non-empty."""
    if outcome is not None and file is not None:
        raise OpError("VALIDATION_ERROR", "Provide either OUTCOME or --file, not both.")
    if outcome is None and file is None:
        raise OpError(
            "VALIDATION_ERROR", "Provide acceptance-criterion outcome as OUTCOME or via --file."
        )
    try:
        return normalize_outcome(outcome if outcome is not None else file)
    except ValueError as exc:
        raise OpError("VALIDATION_ERROR", str(exc)) from exc


def check_criterion_id(criterion_id: str) -> None:
    try:
        validate_criterion_id(criterion_id)
    except ValueError as exc:
        raise OpError("VALIDATION_ERROR", str(exc)) from exc


def criterion_value(task_id: str, snapshot: dict, criterion_id: str) -> dict:
    """The ``data`` every ``criterion`` write prints under ``--json``."""
    return {
        "task_id": task_id,
        "criterion": find_criterion(snapshot, criterion_id),
        "snapshot": snapshot,
    }


@dataclass(frozen=True, kw_only=True)
class CriterionAddParams(CommonParams):
    task: str
    outcome: str | None = None
    file: str | None = None  # the text of --file PATH
    id: str | None = None

    def check(self) -> None:
        outcome_text(self.outcome, self.file)
        if self.id is not None:
            check_criterion_id(self.id)


@operation("task.criterion_add")
class CriterionAdd:
    Params = CriterionAddParams

    def run(self, ctx: OpContext, p: CriterionAddParams) -> OpResult:
        outcome = outcome_text(p.outcome, p.file)
        task_id = ctx.resolve_task(p.task)

        def decide(context):  # noqa: ANN001, ANN202
            snapshot = context.snapshot
            chosen_id = p.id or allocate_criterion_id(snapshot.get("acceptance_criteria", []))
            existing = find_criterion(snapshot, chosen_id)
            if existing is not None:
                if p.id is not None and existing["revisions"][0]["outcome"] == outcome:
                    return TaskMutationDecision(value=copy.deepcopy(existing), idempotent=True)
                raise ValueError(
                    f"Acceptance criterion {chosen_id} already exists with different "
                    "initial prose."
                )
            data = {"criterion_id": chosen_id, "outcome": outcome, "revision": 1}
            return TaskMutationDecision(
                events=[ctx.event("acceptance_criterion_added", task_id, data, p)],
                value=chosen_id,
            )

        result = mutate_mapping_value_errors(ctx, task_id, decide, "VALIDATION_ERROR")
        value = result.callback_value
        chosen_id = value["id"] if isinstance(value, dict) else value
        return OpResult(
            task=result.snapshot,
            events=result.appended_events,
            value=criterion_value(task_id, result.snapshot, chosen_id),
            idempotent=result.idempotent,
        )
