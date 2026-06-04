"""Flag commands: needs-human (set/clear the orthogonal needs_human flag)."""

from __future__ import annotations

import click

from lattice.cli.helpers import (
    common_options,
    load_project_config,
    output_error,
    output_result,
    read_snapshot_or_exit,
    require_actor,
    require_root,
    resolve_task_id,
    validate_actor_format_or_exit,
    write_task_event,
)
from lattice.cli.main import cli
from lattice.core.events import create_event, get_actor_display
from lattice.core.tasks import apply_event_to_snapshot


def _notify_c11(snapshot: dict, *, flagged: bool) -> None:
    """Update the c11 sidebar when the flag changes (best-effort)."""
    from lattice.cli.c11_bridge import c11_available, on_needs_human_changed

    if c11_available():
        on_needs_human_changed(snapshot, flagged)


@cli.command("needs-human")
@click.argument("task_id")
@click.argument("reason", required=False)
@click.option("--clear", "clear_flag", is_flag=True, help="Clear the needs_human flag.")
@click.option("--note", default=None, help="Resolution note recorded when clearing.")
@common_options
def needs_human_cmd(
    task_id: str,
    reason: str | None,
    clear_flag: bool,
    note: str | None,
    model: str | None,
    session: str | None,
    output_json: bool,
    quiet: bool,
    triggered_by: str | None,
    on_behalf_of: str | None,
    provenance_reason: str | None,
) -> None:
    """Set or clear the needs_human flag on a task.

    The flag is orthogonal to status: the task stays in its current
    status/swimlane while signalling that a human decision, approval,
    or input is required. Set it with a REASON (required — say exactly
    what you need); clear it with --clear once the human has responded.

    \b
        lattice needs-human LAT-42 "Need: which OAuth provider?" --actor agent:claude
        lattice needs-human LAT-42 --clear --note "chose google" --actor human:atin
    """
    is_json = output_json

    lattice_dir = require_root(is_json)
    config = load_project_config(lattice_dir)
    actor = require_actor(is_json)
    if on_behalf_of is not None:
        validate_actor_format_or_exit(on_behalf_of, is_json)

    task_id = resolve_task_id(lattice_dir, task_id, is_json)
    snapshot = read_snapshot_or_exit(lattice_dir, task_id, is_json)
    current = snapshot.get("needs_human")
    display_id = snapshot.get("short_id") or task_id

    if clear_flag:
        if reason is not None:
            output_error(
                "REASON is only for setting the flag. To clear, use "
                "--clear (optionally with --note).",
                "VALIDATION_ERROR",
                is_json,
            )
        if not current:
            output_error(
                f"Task {display_id} does not have the needs_human flag set.",
                "FLAG_NOT_SET",
                is_json,
            )
        event = create_event(
            type="needs_human_cleared",
            task_id=task_id,
            actor=actor,
            data={"note": note},
            model=model,
            session=session,
            triggered_by=triggered_by,
            on_behalf_of=on_behalf_of,
            reason=provenance_reason,
        )
        updated = apply_event_to_snapshot(snapshot, event)
        write_task_event(lattice_dir, task_id, [event], updated, config)
        _notify_c11(updated, flagged=False)
        note_msg = f"  Note: {note}" if note else ""
        output_result(
            data=updated,
            human_message=f"needs_human cleared ({display_id}){note_msg}",
            quiet_value="ok",
            is_json=is_json,
            is_quiet=quiet,
        )
        return

    if note is not None:
        output_error(
            "--note is only for clearing the flag. To set, pass a REASON.",
            "VALIDATION_ERROR",
            is_json,
        )
    if not reason or not reason.strip():
        output_error(
            "REASON is required when setting the needs_human flag. "
            "Say exactly what you need from the human, in one line.",
            "VALIDATION_ERROR",
            is_json,
        )
    if current:
        flagged_by = get_actor_display(current.get("flagged_by"))
        output_error(
            f"Task {display_id} already has the needs_human flag set "
            f"(by {flagged_by} since {current.get('since')}: "
            f"{current.get('reason')}). Clear it first with --clear.",
            "FLAG_ALREADY_SET",
            is_json,
        )

    event = create_event(
        type="needs_human_flagged",
        task_id=task_id,
        actor=actor,
        data={"reason": reason},
        model=model,
        session=session,
        triggered_by=triggered_by,
        on_behalf_of=on_behalf_of,
        reason=provenance_reason,
    )
    updated = apply_event_to_snapshot(snapshot, event)
    write_task_event(lattice_dir, task_id, [event], updated, config)
    _notify_c11(updated, flagged=True)
    output_result(
        data=updated,
        human_message=(
            f"needs_human set ({display_id}, status stays {snapshot.get('status')})\n"
            f"  Need: {reason}"
        ),
        quiet_value="ok",
        is_json=is_json,
        is_quiet=quiet,
    )
