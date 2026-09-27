"""Flag commands: needs-human (set/clear the orthogonal needs_human flag)."""

from __future__ import annotations

from pathlib import Path

import click

from lattice.cli.helpers import common_options, output_error, output_result
from lattice.cli.main import cli
from lattice.cli.ops_bridge import board_or_exit, caller_from_context, run_operation
from lattice.ops import OpError
from lattice.ops.task_needs_human import REASON_REQUIRED


def _notify_c11(snapshot: dict, *, flagged: bool) -> None:
    """Update the c11 sidebar when the flag changes (best-effort)."""
    from lattice.cli.c11_bridge import c11_available, on_needs_human_changed

    if c11_available():
        on_needs_human_changed(snapshot, flagged)


def _reject_before_unreadable_file(task_id: str, on_behalf_of: str | None, is_json: bool) -> None:
    """Report any rejection today's order puts ahead of reading --file.

    Runs the operation with an empty reason: an actor, task, or argument error
    is printed as always; the empty reason's own rejection (``REASON_REQUIRED``,
    reached only when everything before it passed) writes nothing and returns,
    so the caller re-raises the read error at the point it always surfaced.
    """
    board = board_or_exit(is_json)
    try:
        board.execute(
            "task.needs_human",
            {"task": task_id, "file": "", "on_behalf_of": on_behalf_of},
            caller_from_context(),
        )
    except OpError as exc:
        if exc.code == "VALIDATION_ERROR" and exc.message == REASON_REQUIRED:
            return
        output_error(exc.message, exc.code, is_json)


@cli.command("needs-human")
@click.argument("task_id")
@click.argument("reason", required=False)
@click.option(
    "--file",
    "file_path",
    default=None,
    type=click.Path(exists=True),
    help="Read the reason from a file (safe for long prose — no shell interpolation).",
)
@click.option("--clear", "clear_flag", is_flag=True, help="Clear the needs_human flag.")
@click.option("--note", default=None, help="Resolution note recorded when clearing.")
@common_options
def needs_human_cmd(
    task_id: str,
    reason: str | None,
    file_path: str | None,
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
        lattice needs-human LAT-42 --file reason.md --actor agent:claude
        lattice needs-human LAT-42 --clear --note "chose google" --actor human:atin
    """
    is_json = output_json
    # The operation checks the actor, the task, and then the argument mix
    # (--clear with a reason, --note when setting, REASON with --file), in
    # today's order. The file is read only when its text becomes the reason;
    # in every other combination the operation rejects --file before using it,
    # so it gets an empty stand-in and the path is never opened.
    file_text: str | None = None
    if file_path is not None:
        file_used = not clear_flag and note is None and reason is None
        try:
            file_text = Path(file_path).read_text(encoding="utf-8") if file_used else ""
        except (OSError, UnicodeDecodeError):
            _reject_before_unreadable_file(task_id, on_behalf_of, is_json)
            raise
    result = run_operation(
        "task.needs_human",
        {
            "task": task_id,
            "flag_reason": reason,
            "file": file_text,
            "clear": clear_flag,
            "note": note,
            "model": model,
            "session": session,
            "triggered_by": triggered_by,
            "on_behalf_of": on_behalf_of,
            "reason": provenance_reason,
        },
        is_json,
    )
    updated = result.value
    display_id = updated.get("short_id") or updated["id"]
    if clear_flag:
        _notify_c11(updated, flagged=False)
        note_msg = f"  Note: {note}" if note else ""
        human_message = f"needs_human cleared ({display_id}){note_msg}"
    else:
        _notify_c11(updated, flagged=True)
        need = result.events[-1]["data"]["reason"]
        human_message = (
            f"needs_human set ({display_id}, status stays {updated.get('status')})\n  Need: {need}"
        )
    output_result(
        data=updated,
        human_message=human_message,
        quiet_value="ok",
        is_json=is_json,
        is_quiet=quiet,
    )
