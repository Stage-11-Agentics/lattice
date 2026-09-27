"""Relationship and branch-link commands: link, unlink, branch-link, branch-unlink."""

from __future__ import annotations

import click

from lattice.cli.helpers import common_options, output_result
from lattice.cli.main import cli
from lattice.cli.ops_bridge import params_or_exit, provenance_params, run_operation
from lattice.ops.task_branch_link import repo_display


# ---------------------------------------------------------------------------
# lattice link
# ---------------------------------------------------------------------------


@cli.command()
@click.argument("task_id")
@click.argument("rel_type", metavar="TYPE")
@click.argument("target_task_id")
@click.option("--note", default=None, help="Optional note for the relationship.")
@common_options
def link(
    task_id: str,
    rel_type: str,
    target_task_id: str,
    note: str | None,
    model: str | None,
    session: str | None,
    output_json: bool,
    quiet: bool,
    triggered_by: str | None,
    on_behalf_of: str | None,
    provenance_reason: str | None,
) -> None:
    """Create a relationship between two tasks."""
    is_json = output_json
    result = run_operation(
        "task.link",
        {
            "task": task_id,
            "type": rel_type,
            "target_task": target_task_id,
            "note": note,
            **provenance_params(model, session, triggered_by, on_behalf_of, provenance_reason),
        },
        is_json,
    )
    task_id = result.value["id"]
    target_task_id = result.events[-1]["data"]["target_task_id"]
    output_result(
        data=result.value,
        human_message=(f"Linked {task_id} --[{rel_type}]--> {target_task_id}"),
        quiet_value=task_id,
        is_json=is_json,
        is_quiet=quiet,
    )


# ---------------------------------------------------------------------------
# lattice unlink
# ---------------------------------------------------------------------------


@cli.command()
@click.argument("task_id")
@click.argument("rel_type", metavar="TYPE")
@click.argument("target_task_id")
@common_options
def unlink(
    task_id: str,
    rel_type: str,
    target_task_id: str,
    model: str | None,
    session: str | None,
    output_json: bool,
    quiet: bool,
    triggered_by: str | None,
    on_behalf_of: str | None,
    provenance_reason: str | None,
) -> None:
    """Remove a relationship between two tasks."""
    is_json = output_json
    result = run_operation(
        "task.unlink",
        {
            "task": task_id,
            "type": rel_type,
            "target_task": target_task_id,
            **provenance_params(model, session, triggered_by, on_behalf_of, provenance_reason),
        },
        is_json,
    )
    task_id = result.value["id"]
    target_task_id = result.events[-1]["data"]["target_task_id"]
    output_result(
        data=result.value,
        human_message=(f"Unlinked {task_id} --[{rel_type}]--> {target_task_id}"),
        quiet_value=task_id,
        is_json=is_json,
        is_quiet=quiet,
    )


# ---------------------------------------------------------------------------
# lattice branch-link
# ---------------------------------------------------------------------------


@cli.command("branch-link")
@click.argument("task_id")
@click.argument("branch")
@click.option("--repo", default=None, help="Optional repository identifier.")
@common_options
def branch_link(
    task_id: str,
    branch: str,
    repo: str | None,
    model: str | None,
    session: str | None,
    output_json: bool,
    quiet: bool,
    triggered_by: str | None,
    on_behalf_of: str | None,
    provenance_reason: str | None,
) -> None:
    """Link a git branch to a task."""
    is_json = output_json
    # The branch name is an argument problem, checked before the board.
    params = params_or_exit(
        "task.branch_link",
        {
            "task": task_id,
            "branch": branch,
            "repo": repo,
            **provenance_params(model, session, triggered_by, on_behalf_of, provenance_reason),
        },
        is_json,
    )
    result = run_operation("task.branch_link", params, is_json)
    task_id = result.value["id"]
    repo = result.events[-1]["data"].get("repo")
    output_result(
        data=result.value,
        human_message=f"Linked branch '{branch}'{repo_display(repo)} to {task_id}",
        quiet_value=task_id,
        is_json=is_json,
        is_quiet=quiet,
    )


# ---------------------------------------------------------------------------
# lattice branch-unlink
# ---------------------------------------------------------------------------


@cli.command("branch-unlink")
@click.argument("task_id")
@click.argument("branch")
@click.option("--repo", default=None, help="Optional repository identifier.")
@common_options
def branch_unlink(
    task_id: str,
    branch: str,
    repo: str | None,
    model: str | None,
    session: str | None,
    output_json: bool,
    quiet: bool,
    triggered_by: str | None,
    on_behalf_of: str | None,
    provenance_reason: str | None,
) -> None:
    """Unlink a git branch from a task."""
    is_json = output_json
    # The branch name is an argument problem, checked before the board.
    params = params_or_exit(
        "task.branch_unlink",
        {
            "task": task_id,
            "branch": branch,
            "repo": repo,
            **provenance_params(model, session, triggered_by, on_behalf_of, provenance_reason),
        },
        is_json,
    )
    result = run_operation("task.branch_unlink", params, is_json)
    task_id = result.value["id"]
    repo = result.events[-1]["data"].get("repo")
    output_result(
        data=result.value,
        human_message=f"Unlinked branch '{branch}'{repo_display(repo)} from {task_id}",
        quiet_value=task_id,
        is_json=is_json,
        is_quiet=quiet,
    )
