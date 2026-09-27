"""``task.criterion_retire``: the ``lattice criterion retire`` command's rules."""

from __future__ import annotations

from dataclasses import dataclass

from lattice.core.acceptance_criteria import find_criterion
from lattice.ops.base import CommonParams, OpContext, OpResult, operation
from lattice.ops.task_criterion_add import check_criterion_id, criterion_value
from lattice.ops.value_errors import mutate_mapping_value_errors
from lattice.storage.operations import TaskMutationDecision


@dataclass(frozen=True, kw_only=True)
class CriterionRetireParams(CommonParams):
    task: str
    criterion_id: str

    def check(self) -> None:
        check_criterion_id(self.criterion_id)


@operation("task.criterion_retire")
class CriterionRetire:
    Params = CriterionRetireParams

    def run(self, ctx: OpContext, p: CriterionRetireParams) -> OpResult:
        task_id = ctx.resolve_task(p.task)

        def decide(context):  # noqa: ANN001, ANN202
            criterion = find_criterion(context.snapshot, p.criterion_id)
            if criterion is None:
                raise ValueError(f"Acceptance criterion {p.criterion_id} not found.")
            if criterion["retired"]:
                raise ValueError(f"Acceptance criterion {p.criterion_id} is already retired.")
            data = {"criterion_id": p.criterion_id, "revision": criterion["revision"]}
            return TaskMutationDecision(
                events=[ctx.event("acceptance_criterion_retired", task_id, data, p)]
            )

        result = mutate_mapping_value_errors(ctx, task_id, decide, "VALIDATION_ERROR")
        return OpResult(
            task=result.snapshot,
            events=result.appended_events,
            value=criterion_value(task_id, result.snapshot, p.criterion_id),
        )
