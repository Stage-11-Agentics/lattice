"""Claim/unclaim commands: bind a task to a c11 surface."""

from __future__ import annotations

import click

from lattice.cli.c11_bridge import c11_available, get_surface, get_workspace, rename_tab
from lattice.cli.helpers import (
    common_options,
    load_project_config,
    output_error,
    output_result,
    read_snapshot_or_exit,
    require_actor,
    require_root,
    resolve_task_id,
    write_task_event,
)
from lattice.cli.main import cli
from lattice.core.events import create_event
from lattice.core.tasks import apply_event_to_snapshot


@cli.command("claim")
@click.argument("task_id")
@click.option(
    "--surface",
    "surface_id",
    default=None,
    help="c11 surface ref (e.g. surface:153). Defaults to C11_SURFACE_ID env var.",
)
@common_options
def claim_cmd(
    task_id: str,
    surface_id: str | None,
    model: str | None,
    session: str | None,
    output_json: bool,
    quiet: bool,
    triggered_by: str | None,
    on_behalf_of: str | None,
    provenance_reason: str | None,
) -> None:
    """Bind a task to a c11 surface.

    Records a surface_bound event and stores the binding in the snapshot.
    If inside c11, renames the tab to the task's short code and title.

    The surface defaults to C11_SURFACE_ID from the environment, or can be
    specified explicitly with --surface.

    Examples:

        lattice claim LAT-55 --actor agent:chef

        lattice claim LAT-55 --surface surface:153 --actor agent:chef
    """
    is_json = output_json
    lattice_dir = require_root(is_json)
    config = load_project_config(lattice_dir)
    actor = require_actor(is_json)

    task_id = resolve_task_id(lattice_dir, task_id, is_json)
    snapshot = read_snapshot_or_exit(lattice_dir, task_id, is_json)

    # Resolve surface
    resolved_surface = surface_id or get_surface()
    if not resolved_surface:
        output_error(
            "No surface specified. Provide --surface or run inside c11 "
            "(C11_SURFACE_ID must be set).",
            "MISSING_SURFACE",
            is_json,
        )

    workspace = get_workspace()

    event_data: dict = {"surface": resolved_surface}
    if workspace:
        event_data["workspace"] = workspace

    event = create_event(
        type="surface_bound",
        task_id=task_id,
        actor=actor,
        data=event_data,
        model=model,
        session=session,
        triggered_by=triggered_by,
        on_behalf_of=on_behalf_of,
        reason=provenance_reason,
    )
    updated_snapshot = apply_event_to_snapshot(snapshot, event)
    write_task_event(lattice_dir, task_id, [event], updated_snapshot, config)

    # Rename tab if inside c11
    if c11_available():
        short_id = updated_snapshot.get("short_id") or task_id
        title = updated_snapshot.get("title") or ""
        rename_tab(resolved_surface, f"{short_id}: {title}")

    display_id = updated_snapshot.get("short_id") or task_id
    output_result(
        data={
            "task_id": task_id,
            "short_id": display_id,
            "surface": resolved_surface,
            "workspace": workspace,
        },
        human_message=f"Bound {display_id} to {resolved_surface}",
        quiet_value=display_id,
        is_json=is_json,
        is_quiet=quiet,
    )


@cli.command("unclaim")
@click.argument("task_id")
@common_options
def unclaim_cmd(
    task_id: str,
    model: str | None,
    session: str | None,
    output_json: bool,
    quiet: bool,
    triggered_by: str | None,
    on_behalf_of: str | None,
    provenance_reason: str | None,
) -> None:
    """Remove a task's c11 surface binding.

    Records a surface_unbound event and clears c11_surface / c11_workspace
    from the snapshot.

    Examples:

        lattice unclaim LAT-55 --actor agent:chef
    """
    is_json = output_json
    lattice_dir = require_root(is_json)
    config = load_project_config(lattice_dir)
    actor = require_actor(is_json)

    task_id = resolve_task_id(lattice_dir, task_id, is_json)
    snapshot = read_snapshot_or_exit(lattice_dir, task_id, is_json)

    old_surface = snapshot.get("c11_surface")

    event = create_event(
        type="surface_unbound",
        task_id=task_id,
        actor=actor,
        data={"surface": old_surface},
        model=model,
        session=session,
        triggered_by=triggered_by,
        on_behalf_of=on_behalf_of,
        reason=provenance_reason,
    )
    updated_snapshot = apply_event_to_snapshot(snapshot, event)
    write_task_event(lattice_dir, task_id, [event], updated_snapshot, config)

    display_id = updated_snapshot.get("short_id") or task_id
    output_result(
        data={
            "task_id": task_id,
            "short_id": display_id,
            "surface": old_surface,
        },
        human_message=f"Unbound {display_id} from {old_surface or '(no surface)'}",
        quiet_value=display_id,
        is_json=is_json,
        is_quiet=quiet,
    )
