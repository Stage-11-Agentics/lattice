"""Claim/unclaim commands: bind a task to a c11 surface."""

from __future__ import annotations

import click

from lattice.cli.c11_bridge import c11_available, get_surface, get_workspace, rename_tab
from lattice.cli.helpers import common_options, output_result
from lattice.cli.main import cli
from lattice.cli.ops_bridge import run_operation


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
    # The surface and workspace are this machine's c11 environment.
    result = run_operation(
        "task.claim",
        {
            "task": task_id,
            "surface": surface_id or get_surface(),
            "workspace": get_workspace(),
            "model": model,
            "session": session,
            "triggered_by": triggered_by,
            "on_behalf_of": on_behalf_of,
            "reason": provenance_reason,
        },
        is_json,
    )
    data = result.value
    display_id = data["short_id"]

    # Rename tab if inside c11
    if c11_available():
        title = result.task.get("title") or ""
        rename_tab(data["surface"], f"{display_id}: {title}")

    output_result(
        data=data,
        human_message=f"Bound {display_id} to {data['surface']}",
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
    result = run_operation(
        "task.unclaim",
        {
            "task": task_id,
            "model": model,
            "session": session,
            "triggered_by": triggered_by,
            "on_behalf_of": on_behalf_of,
            "reason": provenance_reason,
        },
        is_json,
    )
    data = result.value
    display_id = data["short_id"]
    output_result(
        data=data,
        human_message=f"Unbound {display_id} from {data['surface'] or '(no surface)'}",
        quiet_value=display_id,
        is_json=is_json,
        is_quiet=quiet,
    )
