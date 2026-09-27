"""Task-local acceptance-criterion commands."""

from __future__ import annotations

import copy
import json
from pathlib import Path

import click

from lattice.cli.helpers import (
    common_options,
    output_error,
    output_result,
    require_root,
    resolve_task_id,
)
from lattice.cli.main import cli
from lattice.cli.ops_bridge import params_or_exit, provenance_params, run_operation
from lattice.core.acceptance_criteria import criterion_without_history
from lattice.storage.operations import read_task_authority


@cli.group("criterion")
def criterion_group() -> None:
    """Manage optional task-local acceptance criteria."""


def _file_text(file_path: str | None) -> str | None:
    return Path(file_path).read_text(encoding="utf-8") if file_path is not None else None


@criterion_group.command("add")
@click.argument("task_id")
@click.argument("outcome", required=False)
@click.option(
    "--file",
    "file_path",
    type=click.Path(exists=True),
    help="Read outcome prose from a file.",
)
@click.option("--id", "criterion_id", default=None, help="Explicit task-local criterion ID.")
@common_options
def criterion_add(
    task_id: str,
    outcome: str | None,
    file_path: str | None,
    criterion_id: str | None,
    model: str | None,
    session: str | None,
    output_json: bool,
    quiet: bool,
    triggered_by: str | None,
    on_behalf_of: str | None,
    provenance_reason: str | None,
) -> None:
    """Add an observable outcome to a task."""
    is_json = output_json
    # The outcome and the ID are argument problems, checked before the board.
    params = params_or_exit(
        "task.criterion_add",
        {
            "task": task_id,
            "outcome": outcome,
            "file": _file_text(file_path),
            "id": criterion_id,
            **provenance_params(model, session, triggered_by, on_behalf_of, provenance_reason),
        },
        is_json,
    )
    result = run_operation("task.criterion_add", params, is_json)
    data = result.value
    task_id = data["task_id"]
    chosen_id = data["criterion"]["id"]
    output_result(
        data=data,
        human_message=(
            f"Acceptance criterion {chosen_id} already exists (idempotent)."
            if result.idempotent
            else f"Added acceptance criterion {chosen_id} to {task_id} (revision 1)."
        ),
        quiet_value=chosen_id,
        is_json=is_json,
        is_quiet=quiet,
    )


@criterion_group.command("edit")
@click.argument("task_id")
@click.argument("criterion_id")
@click.argument("outcome", required=False)
@click.option(
    "--file", "file_path", type=click.Path(exists=True), help="Read outcome from a file."
)
@common_options
def criterion_edit(
    task_id: str,
    criterion_id: str,
    outcome: str | None,
    file_path: str | None,
    model: str | None,
    session: str | None,
    output_json: bool,
    quiet: bool,
    triggered_by: str | None,
    on_behalf_of: str | None,
    provenance_reason: str | None,
) -> None:
    """Revise an active criterion's outcome prose."""
    is_json = output_json
    # The ID and the outcome are argument problems, checked before the board.
    params = params_or_exit(
        "task.criterion_edit",
        {
            "task": task_id,
            "criterion_id": criterion_id,
            "outcome": outcome,
            "file": _file_text(file_path),
            **provenance_params(model, session, triggered_by, on_behalf_of, provenance_reason),
        },
        is_json,
    )
    result = run_operation("task.criterion_edit", params, is_json)
    criterion = result.value["criterion"]
    output_result(
        data=result.value,
        human_message=(
            f"Acceptance criterion {criterion_id} unchanged."
            if result.idempotent
            else f"Edited acceptance criterion {criterion_id} to revision {criterion['revision']}."
        ),
        quiet_value=criterion_id,
        is_json=is_json,
        is_quiet=quiet,
    )


@criterion_group.command("retire")
@click.argument("task_id")
@click.argument("criterion_id")
@common_options
def criterion_retire(
    task_id: str,
    criterion_id: str,
    model: str | None,
    session: str | None,
    output_json: bool,
    quiet: bool,
    triggered_by: str | None,
    on_behalf_of: str | None,
    provenance_reason: str | None,
) -> None:
    """Retire an active criterion without deleting its history."""
    is_json = output_json
    params = params_or_exit(
        "task.criterion_retire",
        {
            "task": task_id,
            "criterion_id": criterion_id,
            **provenance_params(model, session, triggered_by, on_behalf_of, provenance_reason),
        },
        is_json,
    )
    result = run_operation("task.criterion_retire", params, is_json)
    criterion = result.value["criterion"]
    output_result(
        data=result.value,
        human_message=f"Retired acceptance criterion {criterion_id} at revision {criterion['revision']}.",
        quiet_value=criterion_id,
        is_json=is_json,
        is_quiet=quiet,
    )


@criterion_group.command("list")
@click.argument("task_id")
@click.option("--include-retired", is_flag=True, help="Include retired criteria.")
@click.option("--history", is_flag=True, help="Include full revision histories.")
@click.option("--json", "output_json", is_flag=True, help="Output structured JSON.")
@click.option("--quiet", is_flag=True, help="Print criterion IDs only.")
def criterion_list(
    task_id: str,
    include_retired: bool,
    history: bool,
    output_json: bool,
    quiet: bool,
) -> None:
    """List active or archived task criteria."""
    is_json = output_json
    lattice_dir = require_root(is_json)
    task_id = resolve_task_id(lattice_dir, task_id, is_json, allow_archived=True)
    authority = read_task_authority(lattice_dir, task_id, allow_missing=True)
    if authority is None:
        output_error(f"Task {task_id} not found.", "NOT_FOUND", is_json)
    archived = authority.location == "archived"
    snapshot = authority.snapshot
    criteria = [
        copy.deepcopy(criterion)
        for criterion in snapshot.get("acceptance_criteria", [])
        if include_retired or not criterion.get("retired")
    ]
    if not history:
        criteria = [criterion_without_history(criterion) for criterion in criteria]
    if is_json:
        click.echo(
            json.dumps(
                {
                    "ok": True,
                    "data": {"task_id": task_id, "archived": archived, "criteria": criteria},
                },
                sort_keys=True,
                indent=2,
            )
            + "\n"
        )
        return
    if quiet:
        for criterion in criteria:
            click.echo(criterion["id"])
        return
    if not criteria:
        click.echo(f"No acceptance criteria on {task_id}.")
        return
    suffix = " (archived task)" if archived else ""
    click.echo(f"Acceptance criteria for {task_id}{suffix}:")
    for criterion in criteria:
        marker = " [retired]" if criterion["retired"] else ""
        click.echo(
            f"  {criterion['id']}  r{criterion['revision']}{marker}  {criterion['outcome']}"
        )
        for revision in criterion.get("revisions", []):
            click.echo(
                f"    r{revision['revision']}  {revision['outcome']}  "
                f"({revision['changed_at']} by {revision['changed_by']})"
            )
