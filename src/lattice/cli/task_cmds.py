"""Task write commands: create, update, status, assign, comment, react."""

from __future__ import annotations

import json
import logging
import subprocess
from pathlib import Path

import click

from lattice.cli.helpers import (
    common_options,
    load_project_config,
    output_error,
    output_result,
    require_root,
    resolve_body,
    resolve_task_id,
    require_actor,
    validate_actor_format_or_exit,
)
from lattice.storage.operations import TaskMutationDecision, mutate_task
from lattice.cli.main import cli
from lattice.cli.ops_bridge import board_or_exit, check_or_exit, params_or_exit, run_operation
from lattice.core.comments import (
    validate_comment_body,
)
from lattice.ops.task_comment_edit import check_role_flags
from lattice.core.config import (
    VALID_COMPLEXITIES,
    VALID_PRIORITIES,
    VALID_URGENCIES,
    get_configured_roles,
    get_valid_transitions,
    validate_completion_policy,
    validate_task_type,
    validate_transition,
)
from lattice.core.events import create_event, utc_now
from lattice.core.ids import validate_actor
from lattice.core.tasks import apply_event_to_snapshot

logger = logging.getLogger(__name__)


def _caller_git_worktree() -> Path | None:
    """Return the immutable git root of the invoking checkout, if any."""
    current = Path.cwd().resolve()
    for candidate in (current, *current.parents):
        if (candidate / ".git").exists():
            return candidate
    return None


def _provenance(
    model: str | None,
    session: str | None,
    triggered_by: str | None,
    on_behalf_of: str | None,
    reason: str | None,
) -> dict:
    """The ``common_options`` provenance flags as operation params."""
    return {
        "model": model,
        "session": session,
        "triggered_by": triggered_by,
        "on_behalf_of": on_behalf_of,
        "reason": reason,
    }


# ---------------------------------------------------------------------------
# lattice create
# ---------------------------------------------------------------------------


@cli.command()
@click.argument("title")
@click.option("--type", "task_type", default=None, help="Task type (task, bug, spike, chore).")
@click.option("--priority", default=None, help="Priority (critical, high, medium, low).")
@click.option("--urgency", default=None, help="Urgency (immediate, high, normal, low).")
@click.option("--complexity", default=None, help="Agentic complexity (low, medium, high).")
@click.option("--status", default=None, help="Initial status (default: backlog).")
@click.option("--description", default=None, help="Task description.")
@click.option("--tags", default=None, help="Comma-separated tags, e.g. --tags a,b.")
@click.option(
    "--tag",
    "tag_values",
    multiple=True,
    help="A single tag. Repeatable: --tag a --tag b. Combines with --tags.",
)
@click.option("--assigned-to", default=None, help="Assignee (actor format).")
@click.option("--id", "task_id", default=None, help="Caller-supplied task ID.")
@common_options
def create(
    title: str,
    task_type: str | None,
    priority: str | None,
    urgency: str | None,
    complexity: str | None,
    status: str | None,
    description: str | None,
    tags: str | None,
    tag_values: tuple[str, ...],
    assigned_to: str | None,
    task_id: str | None,
    model: str | None,
    session: str | None,
    output_json: bool,
    quiet: bool,
    triggered_by: str | None,
    on_behalf_of: str | None,
    provenance_reason: str | None,
) -> None:
    """Create a new task."""
    is_json = output_json
    result = run_operation(
        "task.create",
        {
            "title": title,
            "type": task_type,
            "priority": priority,
            "urgency": urgency,
            "complexity": complexity,
            "status": status,
            "description": description,
            "tags": tags,
            "tag": list(tag_values),
            "assigned_to": assigned_to,
            "id": task_id,
            **_provenance(model, session, triggered_by, on_behalf_of, provenance_reason),
        },
        is_json,
    )
    snapshot = result.value
    task_id = snapshot["id"]
    short_id = snapshot.get("short_id")
    status_line = (
        f"  status: {snapshot['status']}  priority: {snapshot['priority']}  "
        f"type: {snapshot['type']}"
    )

    # Output: prefer short_id when available
    display_id = short_id if short_id else task_id
    output_result(
        data=snapshot,
        human_message=(
            f"Task {display_id} already exists (idempotent)."
            if result.idempotent
            else f'Created task {display_id} ({task_id}) "{title}"\n{status_line}'
            if short_id
            else f'Created task {task_id} "{title}"\n{status_line}'
        ),
        quiet_value=display_id,
        is_json=is_json,
        is_quiet=quiet,
    )


# ---------------------------------------------------------------------------
# Updatable field names for `lattice update`
# ---------------------------------------------------------------------------

_UPDATABLE_FIELDS = frozenset(
    {"title", "description", "priority", "urgency", "complexity", "type", "tags"}
)

_REDIRECT_FIELDS = {
    "status": "Use 'lattice status' to change status.",
    "assigned_to": "Use 'lattice assign' to change assignment.",
}


# ---------------------------------------------------------------------------
# lattice update
# ---------------------------------------------------------------------------


@cli.command()
@click.argument("task_id")
@click.argument("pairs", nargs=-1)
@common_options
def update(
    task_id: str,
    pairs: tuple[str, ...],
    model: str | None,
    session: str | None,
    output_json: bool,
    quiet: bool,
    triggered_by: str | None,
    on_behalf_of: str | None,
    provenance_reason: str | None,
) -> None:
    """Update task fields.

    Pass field=value pairs (e.g., title=, description=, priority=, urgency=,
    complexity=, type=, tags=). Use 'lattice edit-description' for
    description-only edits.
    """
    is_json = output_json

    lattice_dir = require_root(is_json)
    config = load_project_config(lattice_dir)
    actor = require_actor(is_json)
    if on_behalf_of is not None:
        validate_actor_format_or_exit(on_behalf_of, is_json)

    task_id = resolve_task_id(lattice_dir, task_id, is_json)

    if not pairs:
        output_error("No field=value pairs provided.", "VALIDATION_ERROR", is_json)

    # Parse field=value pairs — split on first '=' only
    parsed: list[tuple[str, str]] = []
    for pair in pairs:
        if "=" not in pair:
            output_error(
                f"Invalid field=value pair: '{pair}'. Expected format: field=value.",
                "VALIDATION_ERROR",
                is_json,
            )
        field, value = pair.split("=", 1)
        parsed.append((field, value))

    # Validate fields and normalize caller values before entering storage.
    shared_ts = utc_now()
    normalized: list[tuple[str, object]] = []
    for field, value in parsed:
        # Reject status and assigned_to with helpful messages
        if field in _REDIRECT_FIELDS:
            output_error(_REDIRECT_FIELDS[field], "VALIDATION_ERROR", is_json)

        # Handle custom_fields.* dot notation
        if field.startswith("custom_fields."):
            key = field[len("custom_fields.") :]
            if not key:
                output_error(
                    "Invalid custom field: 'custom_fields.' requires a key name.",
                    "VALIDATION_ERROR",
                    is_json,
                )
            normalized.append((field, value))
            continue

        if field not in _UPDATABLE_FIELDS:
            valid = ", ".join(sorted(_UPDATABLE_FIELDS))
            output_error(
                f"Unknown or non-updatable field: '{field}'. "
                f"Updatable fields: {valid}. Use custom_fields.<key> for custom data.",
                "VALIDATION_ERROR",
                is_json,
            )

        # Validate enum fields
        if field == "priority" and value not in VALID_PRIORITIES:
            valid = ", ".join(VALID_PRIORITIES)
            output_error(
                f"Invalid priority: '{value}'. Valid priorities: {valid}.",
                "VALIDATION_ERROR",
                is_json,
            )
        if field == "urgency" and value not in VALID_URGENCIES:
            valid = ", ".join(VALID_URGENCIES)
            output_error(
                f"Invalid urgency: '{value}'. Valid urgencies: {valid}.",
                "VALIDATION_ERROR",
                is_json,
            )
        if field == "complexity" and value not in VALID_COMPLEXITIES:
            valid = ", ".join(VALID_COMPLEXITIES)
            output_error(
                f"Invalid complexity: '{value}'. Valid complexities: {valid}.",
                "VALIDATION_ERROR",
                is_json,
            )
        if field == "type" and not validate_task_type(config, value):
            valid = ", ".join(config.get("task_types", []))
            output_error(
                f"Invalid task type: '{value}'. Valid types: {valid}.", "VALIDATION_ERROR", is_json
            )

        if field == "tags":
            new_value = [t.strip() for t in value.split(",") if t.strip()]
        else:
            new_value = value
        normalized.append((field, new_value))

    def decide(context):  # noqa: ANN001, ANN202
        snapshot = context.snapshot
        assert snapshot is not None
        events: list[dict] = []
        for field, new_value in normalized:
            if field.startswith("custom_fields."):
                key = field[len("custom_fields.") :]
                old_value = (snapshot.get("custom_fields") or {}).get(key)
            elif field == "tags":
                old_value = snapshot.get("tags")
            else:
                old_value = snapshot.get(field)
            if (old_value or []) == new_value if field == "tags" else old_value == new_value:
                continue
            events.append(
                create_event(
                    type="field_updated",
                    task_id=task_id,
                    actor=actor,
                    data={"field": field, "from": old_value, "to": new_value},
                    ts=shared_ts,
                    model=model,
                    session=session,
                    triggered_by=triggered_by,
                    on_behalf_of=on_behalf_of,
                    reason=provenance_reason,
                )
            )
            snapshot = apply_event_to_snapshot(snapshot, events[-1])
        return TaskMutationDecision(
            events=events,
            value=[event["data"]["field"] for event in events],
            idempotent=not events,
        )

    result = mutate_task(lattice_dir, task_id, decide, config, run_hooks=True)
    field_names = result.callback_value
    if not field_names:
        if is_json:
            click.echo(
                json.dumps(
                    {"ok": True, "data": {"message": "No changes"}}, sort_keys=True, indent=2
                )
                + "\n"
            )
        elif quiet:
            click.echo("ok")
        else:
            click.echo("No changes")
        return

    output_result(
        data=result.snapshot,
        human_message=f"Updated task {task_id}: {', '.join(field_names)}",
        quiet_value="ok",
        is_json=is_json,
        is_quiet=quiet,
    )


# ---------------------------------------------------------------------------
# lattice edit-description
# ---------------------------------------------------------------------------


@cli.command("edit-description")
@click.argument("task_id")
@click.argument("description")
@common_options
def edit_description(
    task_id: str,
    description: str,
    model: str | None,
    session: str | None,
    output_json: bool,
    quiet: bool,
    triggered_by: str | None,
    on_behalf_of: str | None,
    provenance_reason: str | None,
) -> None:
    """Edit a task's description.

    Sugar over `lattice update <task> description=<text>` — same event-sourced
    semantics, but takes the description as a positional argument so it doesn't
    need field=value escaping.
    """
    is_json = output_json

    lattice_dir = require_root(is_json)
    config = load_project_config(lattice_dir)
    actor = require_actor(is_json)
    if on_behalf_of is not None:
        validate_actor_format_or_exit(on_behalf_of, is_json)

    task_id = resolve_task_id(lattice_dir, task_id, is_json)

    new_value = description

    def decide(context):  # noqa: ANN001, ANN202
        snapshot = context.snapshot
        assert snapshot is not None
        old_value = snapshot.get("description")
        if old_value == new_value:
            return TaskMutationDecision(idempotent=True)
        event = create_event(
            type="field_updated",
            task_id=task_id,
            actor=actor,
            data={"field": "description", "from": old_value, "to": new_value},
            ts=utc_now(),
            model=model,
            session=session,
            triggered_by=triggered_by,
            on_behalf_of=on_behalf_of,
            reason=provenance_reason,
        )
        return TaskMutationDecision(events=[event])

    result = mutate_task(lattice_dir, task_id, decide, config, run_hooks=True)
    if result.idempotent:
        if is_json:
            click.echo(
                json.dumps(
                    {"ok": True, "data": {"message": "No changes"}}, sort_keys=True, indent=2
                )
                + "\n"
            )
        elif quiet:
            click.echo("ok")
        else:
            click.echo("No changes")
        return

    output_result(
        data=result.snapshot,
        human_message=f"Updated description on {task_id}",
        quiet_value="ok",
        is_json=is_json,
        is_quiet=quiet,
    )


# ---------------------------------------------------------------------------
# Next-step hints after status transitions (LAT-197)
# ---------------------------------------------------------------------------


def compute_next_steps(
    new_status: str,
    config: dict,
    task_id: str,
    lattice_dir: object,  # Path
    *,
    display_id: str | None = None,
    auto_review_result: dict | None = None,
) -> tuple[str | None, dict | None]:
    """Return (human_hint, structured_dict) for the given status transition.

    Both may be ``None`` when no hint applies.  *display_id* is the short ID
    (e.g. ``LAT-42``) used in human-readable hints; falls back to *task_id*.
    *auto_review_result*, when present, is the dict returned by
    :func:`lattice.cli.auto_review.auto_fire_review`; the hint shifts to
    "auto-firing" when ``fired=True`` and appends a skip-reason note when
    ``fired=False``.
    """
    from pathlib import Path

    from lattice.core.auto_review import format_skip_reason

    lattice_dir = Path(lattice_dir)
    label = display_id or task_id

    if new_status == "in_planning":
        hint = f"Next: write the plan in plans/{task_id}.md, then move to planned."
        return hint, {
            "action": "write_plan",
            "plan_path": f"plans/{task_id}.md",
            "then": "planned",
        }

    if new_status == "planned":
        plan_review_mode = config.get("plan_review_mode", "single")
        if auto_review_result and auto_review_result.get("fired"):
            hint = (
                f"Auto-firing plan-review (pid {auto_review_result['pid']}, "
                f"plan_review_mode: {auto_review_result['mode']}). "
                f"Tail: lattice review-status {label}."
            )
            return hint, {
                "action": "plan_review_auto_fired",
                "pid": auto_review_result["pid"],
                "mode": auto_review_result["mode"],
                "log_path": auto_review_result["log_path"],
                "then": "in_progress",
            }
        if plan_review_mode != "inline":
            hint = (
                f"Next: run 'lattice plan-review {label}' "
                f"(plan_review_mode: {plan_review_mode}) before moving to in_progress."
            )
            if auto_review_result and not auto_review_result.get("fired"):
                hint += (
                    " ("
                    + format_skip_reason(
                        auto_review_result.get("reason", "unknown"),
                        holder_pid=auto_review_result.get("holder_pid"),
                    )
                    + ")"
                )
            return hint, {
                "action": "plan_review",
                "command": f"lattice plan-review {label}",
                "plan_review_mode": plan_review_mode,
                "then": "in_progress",
            }
        return None, None

    if new_status == "in_progress":
        hint = "Next: implement the plan, then move to review."
        return hint, {"action": "implement", "then": "review"}

    if new_status == "review":
        review_mode = config.get("review_mode", "single")
        if auto_review_result and auto_review_result.get("fired"):
            hint = (
                f"Auto-firing code-review (pid {auto_review_result['pid']}, "
                f"review_mode: {auto_review_result['mode']}). "
                f"Tail: lattice review-status {label}."
            )
            return hint, {
                "action": "code_review_auto_fired",
                "pid": auto_review_result["pid"],
                "mode": auto_review_result["mode"],
                "log_path": auto_review_result["log_path"],
                "then": "in_validation",
            }
        hint = (
            f"Next: run 'lattice code-review {label}' "
            f"(review_mode: {review_mode}) before moving to in_validation."
        )
        if auto_review_result and not auto_review_result.get("fired"):
            hint += (
                " ("
                + format_skip_reason(
                    auto_review_result.get("reason", "unknown"),
                    holder_pid=auto_review_result.get("holder_pid"),
                )
                + ")"
            )
        return hint, {
            "action": "code_review",
            "command": f"lattice code-review {label}",
            "review_mode": review_mode,
            "then": "in_validation",
        }

    if new_status == "in_validation":
        hint = (
            "Next: validate end-to-end against a running system — browser "
            "automation for web, simulator MCP for mobile, curl for APIs. "
            "Exercise the actual flow this task touched, then record evidence: "
            f"lattice attach {label} --role validation (or lattice comment "
            f"{label} --role validation). On pass move to pr_open; on fail "
            "route back to in_progress (impl-level) or in_planning (plan-level). "
            "The bar: 'I saw it work,' not 'I think it should work.'"
        )
        return hint, {
            "action": "validate_e2e",
            "evidence": f"lattice attach {label} --role validation",
            "then": "pr_open",
        }

    if new_status == "pr_open":
        hint = (
            "Next: open the PR (or confirm it is open). "
            "Move to done after merge, or back to in_progress if PR feedback "
            "requires more changes."
        )
        return hint, {
            "action": "await_pr_merge",
            "then": "done",
        }

    return None, None


# ---------------------------------------------------------------------------
# lattice status
# ---------------------------------------------------------------------------


@cli.command("status")
@click.argument("task_id")
@click.argument("new_status")
@click.option("--force", is_flag=True, help="Force an invalid transition.")
@click.option(
    "--no-auto-review",
    is_flag=True,
    help=(
        "Skip auto-firing code-review/plan-review on transitions to "
        "review/planned (per-invocation opt-out)."
    ),
)
@common_options
def status_cmd(
    task_id: str,
    new_status: str,
    force: bool,
    no_auto_review: bool,
    model: str | None,
    session: str | None,
    output_json: bool,
    quiet: bool,
    triggered_by: str | None,
    on_behalf_of: str | None,
    provenance_reason: str | None,
) -> None:
    """Change a task's status."""
    is_json = output_json
    # One configuration, read before the write, governs the transition rules,
    # the hooks, auto-review, and the hints, even if a hook edits config.json.
    board = board_or_exit(is_json)
    config = board.load_config()
    lattice_dir = board.lattice_dir
    result = run_operation(
        "task.status",
        {
            "task": task_id,
            "new_status": new_status,
            "force": force,
            "no_auto_review": no_auto_review,
            **_provenance(model, session, triggered_by, on_behalf_of, provenance_reason),
        },
        is_json,
        board=board,
        config=config,
    )
    updated_snapshot = result.value
    task_id = updated_snapshot["id"]
    if result.idempotent:
        output_result(
            data=updated_snapshot,
            human_message=f"Already at status {updated_snapshot['status']}",
            quiet_value="ok",
            is_json=is_json,
            is_quiet=quiet,
        )
        return
    event = result.events[-1]
    current_status = event["data"]["from"]
    new_status = event["data"]["to"]
    auto_assigned_to = next(
        (e["data"]["to"] for e in result.events if e["type"] == "assignment_changed"), None
    )

    # c11 integration: update tab title / sidebar / flash when task is surface-bound
    from lattice.cli.c11_bridge import c11_available, on_status_changed

    if c11_available():
        on_status_changed(updated_snapshot, current_status, new_status)

    # Auto-fire review/plan-review on transitions to review/planned (LAT-211).
    # Wrapped defensively: a spawn failure must NEVER block the status
    # transition. The status_changed event is already durable above.
    auto_review_result: dict | None = None
    if new_status in ("review", "planned"):
        try:
            from lattice.cli.auto_review import auto_fire_review

            reviewed_worktree = _caller_git_worktree() if new_status == "review" else None
            # A review must inspect the checkout that requested the transition.
            # Do not let auto_review's board-root fallback turn an unknown
            # caller identity into a review of a different checkout.
            if new_status == "review" and reviewed_worktree is None:
                auto_review_result = {
                    "fired": False,
                    "reason": "reviewed_worktree_unavailable",
                }
            else:
                auto_review_result = auto_fire_review(
                    lattice_dir,
                    task_id,
                    new_status,
                    status_event_id=event["id"],
                    config=config,
                    no_auto_review_flag=no_auto_review,
                    reviewed_worktree=reviewed_worktree,
                )
        except Exception as exc:  # noqa: BLE001 — never fail the transition
            logger.warning(
                "auto-review spawn raised: %s",
                exc,
                exc_info=True,
            )

        # Record the auto_review_spawned audit event when (and only when)
        # we actually spawned, as its own operation. Skip reasons surface in
        # CLI output instead.
        if auto_review_result and auto_review_result.get("fired"):
            try:
                from lattice.core.auto_review import AUTO_REVIEW_ACTOR
                from lattice.ops import Caller

                params = {
                    "task": task_id,
                    "review_type": auto_review_result["review_type"],
                    "mode": auto_review_result["mode"],
                    "log_path": auto_review_result["log_path"],
                    "spawned_at": auto_review_result["spawned_at"],
                    "pid": auto_review_result["pid"],
                    "trigger_status_event_id": event["id"],
                }
                if "reviewed_worktree" in auto_review_result:
                    params["reviewed_worktree"] = auto_review_result["reviewed_worktree"]
                updated_snapshot = board.execute(
                    "task.record_auto_review",
                    params,
                    Caller(actor=AUTO_REVIEW_ACTOR),
                    config=config,
                ).value
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "auto_review_spawned event write failed: %s",
                    exc,
                    exc_info=True,
                )

    display_id = updated_snapshot.get("short_id") or task_id

    # Compute next-step hints (LAT-197)
    hint, next_steps_data = compute_next_steps(
        new_status,
        config,
        task_id,
        lattice_dir,
        display_id=display_id,
        auto_review_result=auto_review_result,
    )

    # Build JSON data with optional next_steps
    json_data = dict(updated_snapshot)
    if next_steps_data is not None:
        json_data["next_steps"] = next_steps_data
    if auto_review_result is not None:
        json_data["auto_review"] = auto_review_result

    assign_msg = f"  (auto-assigned to {auto_assigned_to})" if auto_assigned_to else ""
    human_msg = f"Status: {current_status} -> {new_status} ({display_id}){assign_msg}"
    if hint and not quiet:
        human_msg += f"\n  {hint}"

    output_result(
        data=json_data,
        human_message=human_msg,
        quiet_value="ok",
        is_json=is_json,
        is_quiet=quiet,
    )


# ---------------------------------------------------------------------------
# lattice assign
# ---------------------------------------------------------------------------


_UNASSIGN_SENTINELS = frozenset({"none", "unassigned", "-"})


@cli.command()
@click.argument("task_id")
@click.argument("actor_id")
@common_options
def assign(
    task_id: str,
    actor_id: str,
    model: str | None,
    session: str | None,
    output_json: bool,
    quiet: bool,
    triggered_by: str | None,
    on_behalf_of: str | None,
    provenance_reason: str | None,
) -> None:
    """Assign a task to an actor. Use 'none', 'unassigned', or '-' to unassign."""
    is_json = output_json

    lattice_dir = require_root(is_json)
    config = load_project_config(lattice_dir)
    actor = require_actor(is_json)
    if on_behalf_of is not None:
        validate_actor_format_or_exit(on_behalf_of, is_json)

    task_id = resolve_task_id(lattice_dir, task_id, is_json)

    # Check for unassignment sentinel values
    is_unassign = actor_id.lower() in _UNASSIGN_SENTINELS
    target_actor: str | None = None if is_unassign else actor_id

    # Validate assignee actor format (skip for unassignment)
    if not is_unassign and not validate_actor(actor_id):
        output_error(
            f"Invalid actor format: '{actor_id}'. "
            "Expected prefix:identifier (e.g., human:atin, agent:claude). "
            "Use 'none', 'unassigned', or '-' to unassign.",
            "INVALID_ACTOR",
            is_json,
        )

    def decide(context):  # noqa: ANN001, ANN202
        snapshot = context.snapshot
        assert snapshot is not None
        current_assigned = snapshot.get("assigned_to")
        if current_assigned == target_actor:
            return TaskMutationDecision(value=current_assigned, idempotent=True)
        event = create_event(
            type="assignment_changed",
            task_id=task_id,
            actor=actor,
            data={"from": current_assigned, "to": target_actor},
            model=model,
            session=session,
            triggered_by=triggered_by,
            on_behalf_of=on_behalf_of,
            reason=provenance_reason,
        )
        return TaskMutationDecision(events=[event], value=current_assigned)

    result = mutate_task(lattice_dir, task_id, decide, config, run_hooks=True)
    current_assigned = result.callback_value
    if result.idempotent:
        if is_unassign:
            label = "Already unassigned"
        else:
            label = f"Already assigned to {target_actor}"
        if is_json:
            click.echo(
                json.dumps(
                    {"ok": True, "data": {"message": label}},
                    sort_keys=True,
                    indent=2,
                )
                + "\n"
            )
        elif quiet:
            click.echo("ok")
        else:
            click.echo(label)
        return

    from_label = current_assigned or "unassigned"
    to_label = target_actor or "unassigned"
    output_result(
        data=result.snapshot,
        human_message=f"Assigned: {from_label} -> {to_label}",
        quiet_value="ok",
        is_json=is_json,
        is_quiet=quiet,
    )


# ---------------------------------------------------------------------------
# lattice comment
# ---------------------------------------------------------------------------


@cli.command()
@click.argument("task_id")
@click.argument("text", required=False, default=None)
@click.option(
    "--file",
    "file_path",
    default=None,
    type=click.Path(exists=True),
    help="Read comment body from a file (safe for long prose — no shell interpolation).",
)
@click.option("--reply-to", default=None, help="Event ID of the comment to reply to.")
@click.option(
    "--role",
    default=None,
    help="Role of this comment (e.g., 'review'). Satisfies completion policies.",
)
@click.option(
    "--criterion",
    "criterion_ids",
    multiple=True,
    help="Link this evidence comment to a task-local acceptance criterion (repeatable).",
)
@common_options
def comment(
    task_id: str,
    text: str | None,
    file_path: str | None,
    reply_to: str | None,
    role: str | None,
    criterion_ids: tuple[str, ...],
    model: str | None,
    session: str | None,
    output_json: bool,
    quiet: bool,
    triggered_by: str | None,
    on_behalf_of: str | None,
    provenance_reason: str | None,
) -> None:
    """Add a comment to a task."""
    is_json = output_json
    # The body is resolved first, before the board, exactly as always: both,
    # neither, and the file's text are argument problems, not board state.
    body = resolve_body(text, file_path, is_json, what="comment text", arg_label="TEXT")
    result = run_operation(
        "task.comment",
        {
            "task": task_id,
            "text": body if file_path is None else None,
            "file": body if file_path is not None else None,
            "reply_to": reply_to,
            "role": role,
            "criterion": list(criterion_ids),
            **_provenance(model, session, triggered_by, on_behalf_of, provenance_reason),
        },
        is_json,
    )
    task_id = result.value["id"]
    msg = f"Reply added to {task_id}" if reply_to else f"Comment added to {task_id}"
    output_result(
        data=result.value,
        human_message=msg,
        quiet_value="ok",
        is_json=is_json,
        is_quiet=quiet,
    )


# ---------------------------------------------------------------------------
# lattice comment-edit
# ---------------------------------------------------------------------------


@cli.command("comment-edit")
@click.argument("task_id")
@click.argument("comment_id")
@click.argument("new_text", required=False, default=None)
@click.option(
    "--file",
    "file_path",
    default=None,
    type=click.Path(exists=True),
    help="Read the new comment body from a file (safe for long prose — no shell interpolation).",
)
@click.option("--role", default=None, help="Set or change the comment's role (e.g. review).")
@click.option(
    "--clear-role",
    is_flag=True,
    help="Remove the comment's role while preserving any linked acceptance criteria.",
)
@common_options
def comment_edit(
    task_id: str,
    comment_id: str,
    new_text: str | None,
    file_path: str | None,
    role: str | None,
    clear_role: bool,
    model: str | None,
    session: str | None,
    output_json: bool,
    quiet: bool,
    triggered_by: str | None,
    on_behalf_of: str | None,
    provenance_reason: str | None,
) -> None:
    """Edit an existing comment on a task."""
    is_json = output_json
    # Today's argument order: --role with --clear-role, then NEW_TEXT or --file
    # (exactly one, and the file is read only then); all before the board.
    check_or_exit(is_json, check_role_flags, role, clear_role)
    body = resolve_body(
        new_text, file_path, is_json, what="the new comment text", arg_label="NEW_TEXT"
    )
    params = params_or_exit(
        "task.comment_edit",
        {
            "task": task_id,
            "comment_id": comment_id,
            "new_text": body if file_path is None else None,
            "file": body if file_path is not None else None,
            "role": role,
            "clear_role": clear_role,
            **_provenance(model, session, triggered_by, on_behalf_of, provenance_reason),
        },
        is_json,
    )
    result = run_operation("task.comment_edit", params, is_json)
    output_result(
        data=result.value,
        human_message=f"Comment {comment_id} edited on {result.value['id']}",
        quiet_value="ok",
        is_json=is_json,
        is_quiet=quiet,
    )


# ---------------------------------------------------------------------------
# lattice comment-delete
# ---------------------------------------------------------------------------


@cli.command("comment-delete")
@click.argument("task_id")
@click.argument("comment_id")
@common_options
def comment_delete(
    task_id: str,
    comment_id: str,
    model: str | None,
    session: str | None,
    output_json: bool,
    quiet: bool,
    triggered_by: str | None,
    on_behalf_of: str | None,
    provenance_reason: str | None,
) -> None:
    """Delete a comment from a task."""
    is_json = output_json
    result = run_operation(
        "task.comment_delete",
        {
            "task": task_id,
            "comment_id": comment_id,
            **_provenance(model, session, triggered_by, on_behalf_of, provenance_reason),
        },
        is_json,
    )
    output_result(
        data=result.value,
        human_message=f"Comment {comment_id} deleted from {result.value['id']}",
        quiet_value="ok",
        is_json=is_json,
        is_quiet=quiet,
    )


# ---------------------------------------------------------------------------
# lattice react
# ---------------------------------------------------------------------------


@cli.command()
@click.argument("task_id")
@click.argument("comment_id")
@click.argument("emoji")
@common_options
def react(
    task_id: str,
    comment_id: str,
    emoji: str,
    model: str | None,
    session: str | None,
    output_json: bool,
    quiet: bool,
    triggered_by: str | None,
    on_behalf_of: str | None,
    provenance_reason: str | None,
) -> None:
    """Add a reaction to a comment."""
    is_json = output_json
    result = run_operation(
        "task.react",
        {
            "task": task_id,
            "comment_id": comment_id,
            "emoji": emoji,
            **_provenance(model, session, triggered_by, on_behalf_of, provenance_reason),
        },
        is_json,
    )
    output_result(
        data=result.value,
        human_message=(
            f"Reaction :{emoji}: already exists on {comment_id} (idempotent)."
            if result.idempotent
            else f"Reaction :{emoji}: added to {comment_id}"
        ),
        quiet_value="ok",
        is_json=is_json,
        is_quiet=quiet,
    )


# ---------------------------------------------------------------------------
# lattice unreact
# ---------------------------------------------------------------------------


@cli.command()
@click.argument("task_id")
@click.argument("comment_id")
@click.argument("emoji")
@common_options
def unreact(
    task_id: str,
    comment_id: str,
    emoji: str,
    model: str | None,
    session: str | None,
    output_json: bool,
    quiet: bool,
    triggered_by: str | None,
    on_behalf_of: str | None,
    provenance_reason: str | None,
) -> None:
    """Remove a reaction from a comment."""
    is_json = output_json
    result = run_operation(
        "task.unreact",
        {
            "task": task_id,
            "comment_id": comment_id,
            "emoji": emoji,
            **_provenance(model, session, triggered_by, on_behalf_of, provenance_reason),
        },
        is_json,
    )
    output_result(
        data=result.value,
        human_message=f"Reaction :{emoji}: removed from {comment_id}",
        quiet_value="ok",
        is_json=is_json,
        is_quiet=quiet,
    )


# ---------------------------------------------------------------------------
# lattice complete
# ---------------------------------------------------------------------------


@cli.command("complete")
@click.argument("task_id")
@click.option("--review", "review_text", default=None, help="Review findings text.")
@click.option(
    "--review-file",
    "review_file",
    default=None,
    type=click.Path(exists=True),
    help="Read review findings from a file (safe for long prose — no shell interpolation).",
)
@common_options
def complete_cmd(
    task_id: str,
    review_text: str | None,
    review_file: str | None,
    model: str | None,
    session: str | None,
    output_json: bool,
    quiet: bool,
    triggered_by: str | None,
    on_behalf_of: str | None,
    provenance_reason: str | None,
) -> None:
    """Complete a task with review-to-done ceremony in one command.

    Give the review findings with --review, or --review-file for long prose
    (a file body is read byte-for-byte, never shell-interpolated).

    Emits 4 discrete events (3 if already in review):
    comment_added (role=review), status_changed -> review,
    artifact_attached (role=review), status_changed -> done.
    """
    import tempfile
    from pathlib import Path

    from lattice.core.artifacts import create_artifact_metadata, serialize_artifact
    from lattice.core.ids import generate_artifact_id
    from lattice.storage.fs import atomic_write, ensure_artifact_dirs, unlink_path

    is_json = output_json

    review_text = resolve_body(
        review_text,
        review_file,
        is_json,
        what="review findings",
        arg_label="--review",
        file_label="--review-file",
    )

    lattice_dir = require_root(is_json)
    config = load_project_config(lattice_dir)
    actor = require_actor(is_json)
    if on_behalf_of is not None:
        validate_actor_format_or_exit(on_behalf_of, is_json)

    task_id = resolve_task_id(lattice_dir, task_id, is_json)
    # Validate review -> done transition exists
    if not validate_transition(config, "review", "done"):
        output_error(
            "Cannot complete: no transition from review to done in workflow.",
            "INVALID_TRANSITION",
            is_json,
        )

    # Validate role is accepted
    configured_roles = get_configured_roles(config)
    if configured_roles and "review" not in configured_roles:
        output_error(
            f"Unknown role: 'review'. Valid roles: {', '.join(sorted(configured_roles))}.",
            "INVALID_ROLE",
            is_json,
        )

    # Validate review text
    try:
        review_text = validate_comment_body(review_text)
    except ValueError as exc:
        output_error(str(exc), "VALIDATION_ERROR", is_json)

    shared_ts = utc_now()
    art_id = generate_artifact_id()

    review_payload = review_text
    # The opt-in gate is Lattice-owned. Capture the invoking checkout rather
    # than the board root, which can legitimately be a different worktree.
    policy = config.get("workflow", {}).get("completion_policies", {}).get("done", {})
    if policy.get("require_reachable_review_commit"):
        worktree = _caller_git_worktree()
        if worktree is None:
            output_error("Not inside a git worktree.", "COMPLETION_BLOCKED", is_json)
        sha = subprocess.check_output(
            ["git", "-C", str(worktree), "rev-parse", "HEAD"], text=True
        ).strip()
        review_payload = f"Lattice-Reviewed-Commit: {sha}\n\n{review_text}"

    # --- Write artifact metadata ---
    # Write inline review text as artifact payload
    tmp = tempfile.NamedTemporaryFile(
        mode="w",
        suffix=".md",
        delete=False,
        prefix="lattice-review-",
    )
    tmp.write(review_payload)
    tmp.close()
    tmp_path = Path(tmp.name)

    # meta/ and payload/ are scaffolded at init but empty dirs aren't
    # git-tracked, so cloned installs may lack them (LAT-239).
    ensure_artifact_dirs(lattice_dir)
    try:
        payload_file = f"{art_id}.md"
        dest_path = lattice_dir / "artifacts" / "payload" / payload_file
        atomic_write(dest_path, tmp_path.read_bytes())
    finally:
        tmp_path.unlink(missing_ok=True)

    actor_str = actor if isinstance(actor, str) else actor.get("name", "unknown")
    metadata = create_artifact_metadata(
        art_id,
        "note",
        "Review findings",
        created_by=actor_str,
        created_at=shared_ts,
        summary=review_text[:200] if len(review_text) > 200 else review_text,
        model=model,
        payload_file=payload_file,
        content_type="text/markdown",
        size_bytes=len(review_payload.encode("utf-8")),
    )

    meta_path = lattice_dir / "artifacts" / "meta" / f"{art_id}.json"
    atomic_write(meta_path, serialize_artifact(metadata))

    def decide(context):  # noqa: ANN001, ANN202
        snapshot = context.snapshot
        assert snapshot is not None
        current_status = snapshot["status"]
        already_in_review = current_status == "review"
        if not already_in_review and not validate_transition(config, current_status, "review"):
            valid_targets = get_valid_transitions(config, current_status)
            valid_list = ", ".join(valid_targets) if valid_targets else "(none)"
            output_error(
                f"Cannot complete: task is in '{current_status}' which cannot "
                f"transition to review. Valid transitions: {valid_list}.",
                "INVALID_TRANSITION",
                is_json,
            )
        events: list[dict] = []
        comment_event = create_event(
            type="comment_added",
            task_id=task_id,
            actor=actor,
            data={"body": review_text, "role": "review"},
            ts=shared_ts,
            model=model,
            session=session,
            triggered_by=triggered_by,
            on_behalf_of=on_behalf_of,
            reason=provenance_reason,
        )
        events.append(comment_event)
        working = apply_event_to_snapshot(snapshot, comment_event)
        if not already_in_review:
            review_status_event = create_event(
                type="status_changed",
                task_id=task_id,
                actor=actor,
                data={"from": current_status, "to": "review"},
                ts=shared_ts,
                model=model,
                session=session,
                triggered_by=triggered_by,
                on_behalf_of=on_behalf_of,
                reason=provenance_reason,
            )
            events.append(review_status_event)
            working = apply_event_to_snapshot(working, review_status_event)
        artifact_event = create_event(
            type="artifact_attached",
            task_id=task_id,
            actor=actor,
            data={"artifact_id": art_id, "role": "review"},
            ts=shared_ts,
            model=model,
            session=session,
            triggered_by=triggered_by,
            on_behalf_of=on_behalf_of,
            reason=provenance_reason,
        )
        events.append(artifact_event)
        working = apply_event_to_snapshot(working, artifact_event)
        # Validate the completion policy against the prospective post-transition
        # snapshot. The reachable-review-commit gate is bound to the invoking
        # checkout, and the payload it inspects is the one written above.
        policy_ok, policy_failures = validate_completion_policy(
            config,
            working,
            "done",
            lattice_dir=lattice_dir,
            repo_root=_caller_git_worktree(),
            prospective_review_payloads=[review_payload],
        )
        if not policy_ok:
            output_error(
                f"Completion policy not satisfied: {'; '.join(policy_failures)}.",
                "COMPLETION_BLOCKED",
                is_json,
            )
        done_status_event = create_event(
            type="status_changed",
            task_id=task_id,
            actor=actor,
            data={"from": "review", "to": "done"},
            ts=shared_ts,
            model=model,
            session=session,
            triggered_by=triggered_by,
            on_behalf_of=on_behalf_of,
            reason=provenance_reason,
        )
        events.append(done_status_event)
        return TaskMutationDecision(events=events, value=current_status)

    # The artifact files above are written before the events that reference
    # them, so a refused completion must not leave them behind. Nothing was
    # appended when the mutation raises, so the artifact is unreferenced.
    try:
        result = mutate_task(lattice_dir, task_id, decide, config, run_hooks=True)
    except BaseException:
        unlink_path(dest_path, missing_ok=True)
        unlink_path(meta_path, missing_ok=True)
        raise
    snapshot = result.snapshot
    current_status = result.callback_value

    display_id = snapshot.get("short_id") or task_id
    event_count = len(result.appended_events)
    output_result(
        data=snapshot,
        human_message=(
            f"Completed {display_id}: {event_count} events ({current_status} -> review -> done)"
        ),
        quiet_value="ok",
        is_json=is_json,
        is_quiet=quiet,
    )
