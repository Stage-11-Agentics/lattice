"""Tombstone commands: erase and unerase (SPEC §7)."""

from __future__ import annotations

import click

from lattice.cli.helpers import common_options, output_result
from lattice.cli.main import cli
from lattice.cli.ops_bridge import run_operation


def _params(task_id: str, model, session, triggered_by, on_behalf_of, reason) -> dict:  # noqa: ANN001
    return {
        "task": task_id,
        "model": model,
        "session": session,
        "triggered_by": triggered_by,
        "on_behalf_of": on_behalf_of,
        "reason": reason,
    }


@cli.command()
@click.argument("task_id")
@common_options
def erase(
    task_id: str,
    model: str | None,
    session: str | None,
    output_json: bool,
    quiet: bool,
    triggered_by: str | None,
    on_behalf_of: str | None,
    provenance_reason: str | None,
) -> None:
    """Erase a task: hide it from every view. Requires --reason.

    Nothing is deleted. The task keeps its files and history, and
    'lattice unerase' restores it.
    """
    is_json = output_json
    result = run_operation(
        "task.erase",
        _params(task_id, model, session, triggered_by, on_behalf_of, provenance_reason),
        is_json,
    )
    snap = result.value
    display = snap.get("short_id") or snap["id"]
    output_result(
        data=snap,
        human_message=f"Erased {display}: {snap.get('tombstone_reason')}",
        quiet_value="ok",
        is_json=is_json,
        is_quiet=quiet,
    )


@cli.command()
@click.argument("task_id")
@common_options
def unerase(
    task_id: str,
    model: str | None,
    session: str | None,
    output_json: bool,
    quiet: bool,
    triggered_by: str | None,
    on_behalf_of: str | None,
    provenance_reason: str | None,
) -> None:
    """Restore an erased task to every view, in the status it had. Requires --reason."""
    is_json = output_json
    result = run_operation(
        "task.unerase",
        _params(task_id, model, session, triggered_by, on_behalf_of, provenance_reason),
        is_json,
    )
    snap = result.value
    display = snap.get("short_id") or snap["id"]
    output_result(
        data=snap,
        human_message=f"Restored {display} ({snap.get('status')})",
        quiet_value="ok",
        is_json=is_json,
        is_quiet=quiet,
    )
