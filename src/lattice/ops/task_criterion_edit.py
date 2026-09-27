"""``task.criterion_edit``: the ``lattice criterion edit`` command's rules."""

from __future__ import annotations

from dataclasses import dataclass

from lattice.core.acceptance_criteria import find_criterion
from lattice.ops.base import CommonParams, OpContext, OpResult, operation
from lattice.ops.task_criterion_add import check_criterion_id, criterion_value, outcome_text
from lattice.ops.value_errors import mutate_mapping_value_errors
from lattice.storage.operations import TaskMutationDecision


@dataclass(frozen=True, kw_only=True)
class CriterionEditParams(CommonParams):
    task: str
    criterion_id: str
    outcome: str | None = None
    file: str | None = None  # the text of --file PATH

    def check(self) -> None:
        check_criterion_id(self.criterion_id)
        outcome_text(self.outcome, self.file)


@operation("task.criterion_edit")
class CriterionEdit:
    Params = CriterionEditParams

    def run(self, ctx: OpContext, p: CriterionEditParams) -> OpResult:
        outcome = outcome_text(p.outcome, p.file)
        task_id = ctx.resolve_task(p.task)

        def decide(context):  # noqa: ANN001, ANN202
            criterion = find_criterion(context.snapshot, p.criterion_id)
            if criterion is None:
                raise ValueError(f"Acceptance criterion {p.criterion_id} not found.")
            if criterion["retired"]:
                raise ValueError(f"Acceptance criterion {p.criterion_id} is retired.")
            if criterion["outcome"] == outcome:
                return TaskMutationDecision(idempotent=True)
            data = {
                "criterion_id": p.criterion_id,
                "from_outcome": criterion["outcome"],
                "outcome": outcome,
                "revision": criterion["revision"] + 1,
            }
            return TaskMutationDecision(
                events=[ctx.event("acceptance_criterion_edited", task_id, data, p)]
            )

        result = mutate_mapping_value_errors(ctx, task_id, decide, "VALIDATION_ERROR")
        return OpResult(
            task=result.snapshot,
            events=result.appended_events,
            value=criterion_value(task_id, result.snapshot, p.criterion_id),
            idempotent=result.idempotent,
        )
