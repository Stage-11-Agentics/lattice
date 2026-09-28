"""Task write commands: create, update, status, assign, comment, react."""

from __future__ import annotations

import json
import logging
import subprocess
from pathlib import Path

import click

from lattice.cli.helpers import (
    common_options,
    output_result,
    resolve_body,
)
from lattice.cli.main import cli
from lattice.cli.ops_bridge import (
    board_or_exit,
    check_or_exit,
    params_or_exit,
    run_attested_operation,
    run_operation,
)
from lattice.ops.task_comment_edit import check_role_flags

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
# lattice update
# ---------------------------------------------------------------------------


def _echo_no_change(message: str, is_json: bool, quiet: bool) -> None:
    """Print an idempotent no-op the way update, edit-description and assign always have."""
    if is_json:
        click.echo(
            json.dumps({"ok": True, "data": {"message": message}}, sort_keys=True, indent=2) + "\n"
        )
    elif quiet:
        click.echo("ok")
    else:
        click.echo(message)


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
    result = run_operation(
        "task.update",
        {
            "task": task_id,
            "pairs": list(pairs),
            **_provenance(model, session, triggered_by, on_behalf_of, provenance_reason),
        },
        is_json,
    )
    if result.idempotent:
        _echo_no_change("No changes", is_json, quiet)
        return
    field_names = [event["data"]["field"] for event in result.events]
    output_result(
        data=result.value,
        human_message=f"Updated task {result.value['id']}: {', '.join(field_names)}",
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
    result = run_operation(
        "task.edit_description",
        {
            "task": task_id,
            "description": description,
            **_provenance(model, session, triggered_by, on_behalf_of, provenance_reason),
        },
        is_json,
    )
    if result.idempotent:
        _echo_no_change("No changes", is_json, quiet)
        return
    output_result(
        data=result.value,
        human_message=f"Updated description on {result.value['id']}",
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
        if (lattice_dir / "cache" / "state.json").exists():
            # A hosted cache is read-only: plans go through the server (SPEC §3.9).
            hint = (
                f"Next: write the plan with 'lattice plan write {label} --file <path>', "
                "then move to planned."
            )
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


def _declines_auto_reviews(board: object) -> bool:
    """A hosted board whose remote sets ``run_auto_reviews: false`` (SPEC §3.4)."""
    remote = getattr(board, "remote", None)
    return remote is not None and not remote.run_auto_reviews


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
    from lattice.cli.attestations import completion_attestations
    from lattice.core.config import resolve_status_input

    target_status = resolve_status_input(config, new_status)
    result = run_attested_operation(
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
        attest=lambda: completion_attestations(board, config, task_id, target_status),
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
                    declined_by_machine=_declines_auto_reviews(board),
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
    result = run_operation(
        "task.assign",
        {
            "task": task_id,
            "actor_id": actor_id,
            **_provenance(model, session, triggered_by, on_behalf_of, provenance_reason),
        },
        is_json,
    )
    if result.idempotent:
        _echo_no_change(result.value["message"], is_json, quiet)
        return
    change = result.events[-1]["data"]
    from_label = change["from"] or "unassigned"
    to_label = change["to"] or "unassigned"
    output_result(
        data=result.value,
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
    from lattice.cli.attestations import caller_worktree, completion_attestations
    from lattice.ops.task_complete import prior_status

    is_json = output_json
    # The body is resolved first, before the board, exactly as always.
    review_body = resolve_body(
        review_text,
        review_file,
        is_json,
        what="review findings",
        arg_label="--review",
        file_label="--review-file",
    )
    board = board_or_exit(is_json)
    config = board.load_config()

    # The reachable-review-commit gate is Lattice-owned and reads the invoking
    # checkout, not the board root, which can be a different worktree. Its
    # HEAD becomes the review payload's marker (the operation refuses the
    # completion when there is none).
    review_head: str | None = None
    policy = config.get("workflow", {}).get("completion_policies", {}).get("done", {})
    if policy.get("require_reachable_review_commit"):
        worktree = caller_worktree()
        if worktree is not None:
            board.end_read_phase()  # before git (SPEC §9.4)
            review_head = subprocess.check_output(
                ["git", "-C", str(worktree), "rev-parse", "HEAD"], text=True
            ).strip()

    def attest() -> dict:
        if review_head is None:
            return {}
        return {
            "review_head": review_head,
            **completion_attestations(
                board,
                config,
                task_id,
                "done",
                prospective=[f"Lattice-Reviewed-Commit: {review_head}\n"],
            ),
        }

    result = run_attested_operation(
        "task.complete",
        {
            "task": task_id,
            "review": review_body if review_file is None else None,
            "review_file": review_body if review_file is not None else None,
            **_provenance(model, session, triggered_by, on_behalf_of, provenance_reason),
        },
        is_json,
        board=board,
        config=config,
        attest=attest,
    )
    snapshot = result.value
    display_id = snapshot.get("short_id") or snapshot["id"]
    output_result(
        data=snapshot,
        human_message=(
            f"Completed {display_id}: {len(result.events)} events "
            f"({prior_status(result)} -> review -> done)"
        ),
        quiet_value="ok",
        is_json=is_json,
        is_quiet=quiet,
    )
