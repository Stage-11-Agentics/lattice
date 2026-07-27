"""Task-local acceptance-criterion commands."""

from __future__ import annotations

import copy
import json

import click

from lattice.cli.helpers import (
    common_options,
    load_project_config,
    output_error,
    output_result,
    require_actor,
    require_root,
    resolve_body,
    resolve_task_id,
    validate_actor_format_or_exit,
)
from lattice.cli.main import cli
from lattice.core.acceptance_criteria import (
    allocate_criterion_id,
    criterion_without_history,
    find_criterion,
    normalize_outcome,
    validate_criterion_id,
)
from lattice.core.events import create_event
from lattice.storage.operations import TaskMutationDecision, mutate_task


@cli.group("criterion")
def criterion_group() -> None:
    """Manage optional task-local acceptance criteria."""


def _mutation_context(is_json: bool, raw_task_id: str, on_behalf_of: str | None):
    lattice_dir = require_root(is_json)
    config = load_project_config(lattice_dir)
    actor = require_actor(is_json)
    if on_behalf_of is not None:
        validate_actor_format_or_exit(on_behalf_of, is_json)
    task_id = resolve_task_id(lattice_dir, raw_task_id, is_json)
    return lattice_dir, config, actor, task_id


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
    outcome = normalize_outcome(
        resolve_body(
            outcome,
            file_path,
            is_json,
            what="acceptance-criterion outcome",
            arg_label="OUTCOME",
        )
    )
    if criterion_id is not None:
        try:
            validate_criterion_id(criterion_id)
        except ValueError as exc:
            output_error(str(exc), "VALIDATION_ERROR", is_json)
    lattice_dir, config, actor, task_id = _mutation_context(
        is_json, task_id, on_behalf_of
    )

    def decide(context):  # noqa: ANN001, ANN202
        snapshot = context.snapshot
        assert snapshot is not None
        chosen_id = criterion_id or allocate_criterion_id(
            snapshot.get("acceptance_criteria", [])
        )
        existing = find_criterion(snapshot, chosen_id)
        if existing is not None:
            initial_outcome = existing["revisions"][0]["outcome"]
            if criterion_id is not None and initial_outcome == outcome:
                return TaskMutationDecision(
                    value=copy.deepcopy(existing), idempotent=True
                )
            raise ValueError(
                f"Acceptance criterion {chosen_id} already exists with different initial prose."
            )
        event = create_event(
            type="acceptance_criterion_added",
            task_id=task_id,
            actor=actor,
            data={"criterion_id": chosen_id, "outcome": outcome, "revision": 1},
            model=model,
            session=session,
            triggered_by=triggered_by,
            on_behalf_of=on_behalf_of,
            reason=provenance_reason,
        )
        return TaskMutationDecision(events=[event], value=chosen_id)

    try:
        result = mutate_task(lattice_dir, task_id, decide, config)
    except ValueError as exc:
        output_error(str(exc), "VALIDATION_ERROR", is_json)
    chosen_id = (
        result.callback_value["id"]
        if isinstance(result.callback_value, dict)
        else result.callback_value
    )
    criterion = find_criterion(result.snapshot, chosen_id)
    data = {"task_id": task_id, "criterion": criterion, "snapshot": result.snapshot}
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
@click.option("--file", "file_path", type=click.Path(exists=True), help="Read outcome from a file.")
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
    try:
        validate_criterion_id(criterion_id)
        outcome = normalize_outcome(
            resolve_body(
                outcome,
                file_path,
                is_json,
                what="acceptance-criterion outcome",
                arg_label="OUTCOME",
            )
        )
    except ValueError as exc:
        output_error(str(exc), "VALIDATION_ERROR", is_json)
    lattice_dir, config, actor, task_id = _mutation_context(
        is_json, task_id, on_behalf_of
    )

    def decide(context):  # noqa: ANN001, ANN202
        snapshot = context.snapshot
        assert snapshot is not None
        criterion = find_criterion(snapshot, criterion_id)
        if criterion is None:
            raise ValueError(f"Acceptance criterion {criterion_id} not found.")
        if criterion["retired"]:
            raise ValueError(f"Acceptance criterion {criterion_id} is retired.")
        if criterion["outcome"] == outcome:
            return TaskMutationDecision(
                value=copy.deepcopy(criterion), idempotent=True
            )
        event = create_event(
            type="acceptance_criterion_edited",
            task_id=task_id,
            actor=actor,
            data={
                "criterion_id": criterion_id,
                "from_outcome": criterion["outcome"],
                "outcome": outcome,
                "revision": criterion["revision"] + 1,
            },
            model=model,
            session=session,
            triggered_by=triggered_by,
            on_behalf_of=on_behalf_of,
            reason=provenance_reason,
        )
        return TaskMutationDecision(events=[event])

    try:
        result = mutate_task(lattice_dir, task_id, decide, config)
    except ValueError as exc:
        output_error(str(exc), "VALIDATION_ERROR", is_json)
    criterion = find_criterion(result.snapshot, criterion_id)
    output_result(
        data={"task_id": task_id, "criterion": criterion, "snapshot": result.snapshot},
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
    try:
        validate_criterion_id(criterion_id)
    except ValueError as exc:
        output_error(str(exc), "VALIDATION_ERROR", is_json)
    lattice_dir, config, actor, task_id = _mutation_context(
        is_json, task_id, on_behalf_of
    )

    def decide(context):  # noqa: ANN001, ANN202
        snapshot = context.snapshot
        assert snapshot is not None
        criterion = find_criterion(snapshot, criterion_id)
        if criterion is None:
            raise ValueError(f"Acceptance criterion {criterion_id} not found.")
        if criterion["retired"]:
            raise ValueError(f"Acceptance criterion {criterion_id} is already retired.")
        event = create_event(
            type="acceptance_criterion_retired",
            task_id=task_id,
            actor=actor,
            data={"criterion_id": criterion_id, "revision": criterion["revision"]},
            model=model,
            session=session,
            triggered_by=triggered_by,
            on_behalf_of=on_behalf_of,
            reason=provenance_reason,
        )
        return TaskMutationDecision(events=[event])

    try:
        result = mutate_task(lattice_dir, task_id, decide, config)
    except ValueError as exc:
        output_error(str(exc), "VALIDATION_ERROR", is_json)
    criterion = find_criterion(result.snapshot, criterion_id)
    output_result(
        data={"task_id": task_id, "criterion": criterion, "snapshot": result.snapshot},
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
    active_path = lattice_dir / "tasks" / f"{task_id}.json"
    archived_path = lattice_dir / "archive" / "tasks" / f"{task_id}.json"
    archived = not active_path.exists() and archived_path.exists()
    path = archived_path if archived else active_path
    if not path.exists():
        output_error(f"Task {task_id} not found.", "NOT_FOUND", is_json)
    snapshot = json.loads(path.read_text(encoding="utf-8"))
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
                {"ok": True, "data": {"task_id": task_id, "archived": archived, "criteria": criteria}},
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
