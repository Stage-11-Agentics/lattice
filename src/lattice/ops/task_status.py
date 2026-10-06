"""``task.status``: the ``lattice status`` command's rules.

The transition graph, ``--force`` with ``--reason``, the review-cycle record
and limit,
completion policies, the plan gate, auto-assignment on entering active work,
and the plan-reset heading on a backward move. The c11 bridge and auto-review
spawning are client-side effects the caller runs after this returns.
"""

from __future__ import annotations

from dataclasses import dataclass

from lattice.core.config import (
    get_review_cycle_limit,
    get_valid_transitions,
    resolve_status_input,
    validate_completion_policy,
    validate_status,
    validate_transition,
)
from lattice.core.events import count_review_rework_cycles, latest_review_auto_fired
from lattice.core.tasks import is_backward_status_transition
from lattice.ops.attestation_check import attested_review_commits
from lattice.ops.base import CommonParams, OpContext, OpError, OpResult, operation
from lattice.ops.plan_gate import check_plan_gate
from lattice.storage.fs import atomic_write
from lattice.storage.operations import TaskMutationDecision

ACTIVE_WORK_STATUSES = frozenset({"in_planning", "in_progress"})
REWORK_SOURCES = ("review", "in_validation", "pr_open")
REWORK_TARGETS = ("in_progress", "in_planning")


@dataclass(frozen=True, kw_only=True)
class StatusParams(CommonParams):
    task: str
    new_status: str
    force: bool = False
    no_auto_review: bool = False  # read by the caller's auto-review effect, not here


def _status_rank(config: dict) -> dict[str, int] | None:
    statuses = config.get("workflow", {}).get("statuses", [])
    if not isinstance(statuses, list):
        return None
    rank = {status: idx for idx, status in enumerate(statuses) if isinstance(status, str)}
    return rank or None


def append_plan_reset_section(lattice_dir, task_id: str, actor, event_ts: str | None) -> None:  # noqa: ANN001
    """Append ``## Reset <date> by <actor>`` to the task's plan, if it has one."""
    plan_path = lattice_dir / "plans" / f"{task_id}.md"
    if not plan_path.exists():
        return
    date = "unknown-date"
    if isinstance(event_ts, str) and event_ts:
        date = event_ts.split("T", 1)[0]
    content = plan_path.read_text(encoding="utf-8")
    separator = "" if content.endswith("\n") else "\n"
    atomic_write(plan_path, f"{content}{separator}\n## Reset {date} by {actor}\n")


@operation("task.status")
class Status:
    Params = StatusParams

    def run(self, ctx: OpContext, p: StatusParams) -> OpResult:
        config = ctx.config
        task_id = ctx.resolve_task(p.task)
        ctx.require_active(task_id)

        new_status = resolve_status_input(config, p.new_status)
        if not validate_status(config, new_status):
            if new_status == "needs_human":
                raise OpError(
                    "VALIDATION_ERROR",
                    "needs_human is a flag, not a status. Use: "
                    f'lattice needs-human {task_id} "<what you need>" '
                    "(the task keeps its current status).",
                )
            valid = ", ".join(config.get("workflow", {}).get("statuses", []))
            raise OpError(
                "VALIDATION_ERROR", f"Invalid status: '{new_status}'. Valid statuses: {valid}."
            )

        def decide(context):  # noqa: ANN001, ANN202
            snapshot = context.snapshot
            current = snapshot["status"]
            if current == new_status:
                return TaskMutationDecision(value=False, idempotent=True)
            backward = is_backward_status_transition(current, new_status, _status_rank(config))
            if not validate_transition(config, current, new_status):
                if not p.force:
                    targets = get_valid_transitions(config, current)
                    valid_list = ", ".join(targets) if targets else "(none)"
                    raise OpError.task_state(
                        "INVALID_TRANSITION",
                        f"Invalid transition from {current} to {new_status}. "
                        f"Valid transitions from {current}: {valid_list}. "
                        "Use --force --reason to override.",
                        snapshot,
                    )
                if not p.reason:
                    raise OpError("VALIDATION_ERROR", "--reason is required with --force.")
            # Rework from a gate is recorded as a numbered review cycle. The
            # limit is a hard stop only when Lattice auto-fired the review
            # being reworked; any other review loop is bounded by whoever
            # drives it, so passing the limit there is recorded, not refused.
            review_cycle: dict | None = None
            if current in REWORK_SOURCES and new_status in REWORK_TARGETS:
                task_events = list(context.events)
                cycles = count_review_rework_cycles(task_events)
                limit = get_review_cycle_limit(config)
                enforced = latest_review_auto_fired(task_events)
                if cycles >= limit and enforced and not p.force:
                    raise OpError.task_state(
                        "REVIEW_CYCLE_LIMIT",
                        f"Review cycle limit reached ({cycles}/{limit}). "
                        f"This task has been sent back from review {cycles} time(s), "
                        "and Lattice auto-fired the review being reworked. "
                        "Flag it for a human instead of cycling further: "
                        'lattice needs-human <task> "<what you need>". '
                        "Override with --force --reason.",
                        snapshot,
                    )
                review_cycle = {
                    "cycle": cycles + 1,
                    "limit": limit,
                    "over_limit": cycles >= limit,
                    "enforced": enforced,
                }
            # The reachable-review-commit policy judges the caller's
            # attestation, checked against the board as it is now (SPEC §3.4).
            policy = config.get("workflow", {}).get("completion_policies", {}).get(new_status, {})
            attested = attested_review_commits(ctx, snapshot, policy)
            policy_ok, failures = validate_completion_policy(
                config,
                snapshot,
                new_status,
                events=context.events,
                reachable_review_commits=attested,
            )
            if not policy_ok:
                if not p.force:
                    raise OpError.task_state(
                        "COMPLETION_BLOCKED",
                        "Completion policy not satisfied: "
                        f"{'; '.join(failures)}. Override with --force --reason.",
                        snapshot,
                    )
                if not p.reason:
                    raise OpError("VALIDATION_ERROR", "--reason is required with --force.")
            check_plan_gate(
                ctx.lattice_dir,
                task_id,
                new_status,
                config,
                force=p.force,
                reason=p.reason,
                authoritative_snapshot=snapshot,
                authoritative_location=context.location,
            )
            events: list[dict] = []
            if new_status in ACTIVE_WORK_STATUSES and snapshot.get("assigned_to") is None:
                events.append(
                    ctx.event(
                        "assignment_changed",
                        task_id,
                        {"from": None, "to": ctx.actor},
                        p,
                        reason=False,
                    )
                )
            data: dict = {"from": current, "to": new_status}
            if p.force:
                data["force"] = True
                data["reason"] = p.reason
            if attested is not None:
                data["attestations"] = {"reachable_review_commits": attested}
            if review_cycle is not None:
                data["review_cycle"] = review_cycle
            events.append(ctx.event("status_changed", task_id, data, p))
            return TaskMutationDecision(events=events, value=backward)

        result = ctx.mutate(task_id, decide)
        if not result.idempotent and result.callback_value:
            status_event = result.appended_events[-1]
            append_plan_reset_section(ctx.lattice_dir, task_id, ctx.actor, status_event.get("ts"))
        return OpResult(
            task=result.snapshot,
            events=result.appended_events,
            value=result.snapshot,
            idempotent=result.idempotent,
        )
