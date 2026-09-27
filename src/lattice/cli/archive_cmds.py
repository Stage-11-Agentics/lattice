"""Archive commands: archive and unarchive."""

from __future__ import annotations

import json
import sys
from datetime import datetime, timedelta, timezone

import click

from lattice.boards import LocalBoard
from lattice.cli.helpers import (
    common_options,
    output_error,
    output_result,
    validate_actor_format_or_exit,
)
from lattice.cli.main import cli
from lattice.cli.ops_bridge import board_or_exit, caller_from_context, run_operation
from lattice.ops import OpError, check_path_component
from lattice.ops.base import resolve_actor
from lattice.ops.task_archive import UNRESOLVED_TASK


def _parse_task_ids(raw_ids: tuple[str, ...]) -> list[str]:
    """Expand comma-separated and space-separated task IDs into a flat list."""
    result: list[str] = []
    for raw in raw_ids:
        for part in raw.split(","):
            stripped = part.strip()
            if stripped:
                result.append(stripped)
    return result


def _provenance(
    model: str | None,
    session: str | None,
    triggered_by: str | None,
    on_behalf_of: str | None,
    reason: str | None,
) -> dict:
    return {
        "model": model,
        "session": session,
        "triggered_by": triggered_by,
        "on_behalf_of": on_behalf_of,
        "reason": reason,
    }


def _check_actor_first(board: LocalBoard, provenance: dict, is_json: bool) -> None:
    """Refuse a bad actor before touching any task, as these commands always have.

    Each task is its own operation, which checks the actor again; checking once
    here keeps an actor error fatal (not one failure per task) and ahead of the
    ``--stale`` scan and the no-ID check.
    """
    caller = caller_from_context()
    try:
        if caller.actor_name is not None:
            check_path_component(caller.actor_name, "session name")
        resolve_actor(board.lattice_dir, caller)
    except OpError as exc:
        output_error(exc.message, exc.code, is_json)
    if provenance["on_behalf_of"] is not None:
        validate_actor_format_or_exit(provenance["on_behalf_of"], is_json)


def _move_one(board: LocalBoard, op_name: str, raw_id: str, provenance: dict) -> dict | str:
    """Run one archive/unarchive: the event on success, the error message on failure."""
    try:
        return board.execute(op_name, {"task": raw_id, **provenance}, caller_from_context()).value
    except OpError as exc:
        if exc.details.get("reason") == UNRESOLVED_TASK:
            return f"Invalid or unresolvable task ID: {raw_id}"
        return exc.message


def _report_many(
    succeeded: list[str],
    failed: list[tuple[str, str]],
    *,
    key: str,
    human_template: str,
    empty_message: str | None,
    is_json: bool,
    is_quiet: bool,
) -> None:
    """Print a multi-task result and exit 1 if any task failed."""
    if is_json:
        envelope = {
            "ok": len(failed) == 0,
            "data": {
                key: succeeded,
                "failed": [{"id": fid, "error": msg} for fid, msg in failed],
            },
        }
        click.echo(json.dumps(envelope, sort_keys=True, indent=2))
        if failed:
            sys.exit(1)
        return

    if is_quiet:
        for tid in succeeded:
            click.echo(tid)
        if failed:
            sys.exit(1)
        return

    if succeeded:
        click.echo(human_template.format(n=len(succeeded), ids=", ".join(succeeded)))
    elif empty_message is not None:
        click.echo(empty_message)
    for fid, msg in failed:
        click.echo(f"  Failed {fid}: {msg}", err=True)
    if failed:
        sys.exit(1)


@cli.command()
@click.argument("task_ids", nargs=-1, required=False)
@click.option("--stale", is_flag=True, help="Archive all done tasks older than yesterday.")
@common_options
def archive(
    task_ids: tuple[str, ...],
    stale: bool,
    model: str | None,
    session: str | None,
    output_json: bool,
    quiet: bool,
    triggered_by: str | None,
    on_behalf_of: str | None,
    provenance_reason: str | None,
) -> None:
    """Archive one or more completed tasks.

    Accepts multiple task IDs separated by spaces or commas:

      lattice archive LAT-1 LAT-2 LAT-3 --actor human:atin

      lattice archive LAT-1,LAT-2,LAT-3 --actor human:atin

    Use --stale to auto-archive done tasks older than yesterday:

      lattice archive --stale --actor human:atin
    """
    is_json = output_json
    provenance = _provenance(model, session, triggered_by, on_behalf_of, provenance_reason)

    board = board_or_exit(is_json)
    _check_actor_first(board, provenance, is_json)

    if stale:
        _archive_stale(board, provenance, is_json=is_json, is_quiet=quiet)
        return

    if not task_ids:
        output_error(
            "No task IDs provided. Use --stale to auto-archive old done tasks.",
            "VALIDATION_ERROR",
            is_json,
        )

    parsed_ids = _parse_task_ids(task_ids)

    # Single task: preserve original behavior (errors exit immediately)
    if len(parsed_ids) == 1:
        event = run_operation(
            "task.archive", {"task": parsed_ids[0], **provenance}, is_json, board=board
        ).value
        output_result(
            data=event,
            human_message=f"Archived task {event['task_id']}",
            quiet_value=event["task_id"],
            is_json=is_json,
            is_quiet=quiet,
        )
        return

    # Multiple tasks: process all, collect results
    succeeded: list[str] = []
    failed: list[tuple[str, str]] = []
    for raw_id in parsed_ids:
        result = _move_one(board, "task.archive", raw_id, provenance)
        if isinstance(result, str):
            failed.append((raw_id, result))
        else:
            succeeded.append(raw_id)
    _report_many(
        succeeded,
        failed,
        key="archived",
        human_template="Archived {n} task(s): {ids}",
        empty_message=None,
        is_json=is_json,
        is_quiet=quiet,
    )


def _archive_stale(board: LocalBoard, provenance: dict, *, is_json: bool, is_quiet: bool) -> None:
    """Archive all done tasks where done_at (or updated_at) is before yesterday."""
    now = datetime.now(timezone.utc)
    # "Before yesterday" means done_at date < today - 1 day (i.e., 2+ days ago)
    cutoff = (now - timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)

    tasks_dir = board.lattice_dir / "tasks"
    candidates: list[str] = []
    if tasks_dir.is_dir():
        for task_file in sorted(tasks_dir.glob("*.json")):
            try:
                snap = json.loads(task_file.read_text())
            except (json.JSONDecodeError, OSError):
                continue
            if snap.get("status") != "done":
                continue
            # Use done_at if available, fall back to updated_at
            ts_str = snap.get("done_at") or snap.get("updated_at")
            if not ts_str:
                continue
            try:
                # Parse ISO timestamp (handles both Z suffix and +00:00)
                done_dt = datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
            except (ValueError, TypeError):
                continue
            if done_dt < cutoff:
                candidates.append(snap["id"])

    if not candidates:
        if is_json:
            click.echo(
                json.dumps(
                    {"ok": True, "data": {"archived": [], "failed": []}}, sort_keys=True, indent=2
                )
            )
        elif not is_quiet:
            click.echo("No stale done tasks found.")
        return

    succeeded: list[str] = []
    failed: list[tuple[str, str]] = []
    for task_id in candidates:
        result = _move_one(board, "task.archive", task_id, provenance)
        if isinstance(result, str):
            failed.append((task_id, result))
        else:
            succeeded.append(task_id)
    _report_many(
        succeeded,
        failed,
        key="archived",
        human_template="Archived {n} stale done task(s): {ids}",
        empty_message="No stale done tasks found.",
        is_json=is_json,
        is_quiet=is_quiet,
    )


@cli.command()
@click.argument("task_ids", nargs=-1, required=True)
@common_options
def unarchive(
    task_ids: tuple[str, ...],
    model: str | None,
    session: str | None,
    output_json: bool,
    quiet: bool,
    triggered_by: str | None,
    on_behalf_of: str | None,
    provenance_reason: str | None,
) -> None:
    """Restore one or more archived tasks to active status.

    Accepts multiple task IDs separated by spaces or commas:

      lattice unarchive LAT-1 LAT-2 LAT-3 --actor human:atin

      lattice unarchive LAT-1,LAT-2,LAT-3 --actor human:atin
    """
    is_json = output_json
    provenance = _provenance(model, session, triggered_by, on_behalf_of, provenance_reason)

    board = board_or_exit(is_json)
    _check_actor_first(board, provenance, is_json)

    parsed_ids = _parse_task_ids(task_ids)

    # Single task: preserve original behavior
    if len(parsed_ids) == 1:
        event = run_operation(
            "task.unarchive", {"task": parsed_ids[0], **provenance}, is_json, board=board
        ).value
        output_result(
            data=event,
            human_message=f"Unarchived task {event['task_id']}",
            quiet_value=event["task_id"],
            is_json=is_json,
            is_quiet=quiet,
        )
        return

    # Multiple tasks: process all, collect results
    succeeded: list[str] = []
    failed: list[tuple[str, str]] = []
    for raw_id in parsed_ids:
        result = _move_one(board, "task.unarchive", raw_id, provenance)
        if isinstance(result, str):
            failed.append((raw_id, result))
        else:
            succeeded.append(raw_id)
    _report_many(
        succeeded,
        failed,
        key="unarchived",
        human_template="Unarchived {n} task(s): {ids}",
        empty_message=None,
        is_json=is_json,
        is_quiet=quiet,
    )
