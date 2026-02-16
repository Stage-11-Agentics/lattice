"""Relationship and branch-link commands: link, unlink, branch-link, branch-unlink."""

from __future__ import annotations

import click

from lattice.cli.helpers import (
    common_options,
    load_project_config,
    output_error,
    output_result,
    read_snapshot_or_exit,
    require_root,
    resolve_task_id,
    validate_actor_or_exit,
    write_task_event,
)
from lattice.cli.main import cli
from lattice.core.events import create_event, serialize_event
from lattice.core.relationships import RELATIONSHIP_TYPES, validate_relationship_type
from lattice.core.tasks import apply_event_to_snapshot, serialize_snapshot
from lattice.storage.fs import atomic_write, jsonl_append
from lattice.storage.hooks import execute_hooks
from lattice.storage.locks import multi_lock


def _validate_branch_name(branch: str, is_json: bool) -> None:
    """Validate a branch name for safety.

    Rejects empty/whitespace-only names, names starting with ``-``
    (git flag injection), and names containing ASCII control characters.
    """
    if not branch or not branch.strip():
        output_error(
            "Branch name must not be empty or whitespace-only.",
            "VALIDATION_ERROR",
            is_json,
        )
    if branch.startswith("-"):
        output_error(
            f"Branch name must not start with '-': '{branch}'.",
            "VALIDATION_ERROR",
            is_json,
        )
    if any(0 <= ord(c) <= 31 for c in branch):
        output_error(
            f"Branch name must not contain control characters: '{branch!r}'.",
            "VALIDATION_ERROR",
            is_json,
        )


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
    actor: str,
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

    lattice_dir = require_root(is_json)
    config = load_project_config(lattice_dir)
    validate_actor_or_exit(actor, is_json)
    if on_behalf_of is not None:
        validate_actor_or_exit(on_behalf_of, is_json)

    task_id = resolve_task_id(lattice_dir, task_id, is_json)
    target_task_id = resolve_task_id(lattice_dir, target_task_id, is_json)

    # Validate relationship type
    if not validate_relationship_type(rel_type):
        sorted_types = ", ".join(sorted(RELATIONSHIP_TYPES))
        output_error(
            f"Invalid relationship type: '{rel_type}'. Valid types: {sorted_types}.",
            "VALIDATION_ERROR",
            is_json,
        )

    # Reject self-links
    if task_id == target_task_id:
        output_error(
            "Cannot create a relationship from a task to itself.",
            "VALIDATION_ERROR",
            is_json,
        )

    # Validate both tasks exist
    snapshot = read_snapshot_or_exit(lattice_dir, task_id, is_json)
    # Check target exists (we don't need the snapshot, just existence)
    target_path = lattice_dir / "tasks" / f"{target_task_id}.json"
    if not target_path.exists():
        output_error(
            f"Target task {target_task_id} not found.",
            "NOT_FOUND",
            is_json,
        )

    # Reject duplicates: same type + same target already in relationships_out
    for rel in snapshot.get("relationships_out", []):
        if rel["type"] == rel_type and rel["target_task_id"] == target_task_id:
            output_error(
                f"Duplicate: {rel_type} relationship to {target_task_id} already exists.",
                "CONFLICT",
                is_json,
            )

    # Build event
    event_data: dict = {
        "type": rel_type,
        "target_task_id": target_task_id,
    }
    if note is not None:
        event_data["note"] = note

    event = create_event(
        type="relationship_added",
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

    # Write (event-first, then snapshot, under lock)
    write_task_event(lattice_dir, task_id, [event], updated_snapshot, config)

    # Output
    output_result(
        data=updated_snapshot,
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
    actor: str,
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

    lattice_dir = require_root(is_json)
    config = load_project_config(lattice_dir)
    validate_actor_or_exit(actor, is_json)
    if on_behalf_of is not None:
        validate_actor_or_exit(on_behalf_of, is_json)

    task_id = resolve_task_id(lattice_dir, task_id, is_json)
    target_task_id = resolve_task_id(lattice_dir, target_task_id, is_json)

    # Validate relationship type
    if not validate_relationship_type(rel_type):
        sorted_types = ", ".join(sorted(RELATIONSHIP_TYPES))
        output_error(
            f"Invalid relationship type: '{rel_type}'. Valid types: {sorted_types}.",
            "VALIDATION_ERROR",
            is_json,
        )

    # Validate source task exists and load snapshot
    snapshot = read_snapshot_or_exit(lattice_dir, task_id, is_json)

    # Validate the relationship exists in snapshot's relationships_out
    found = False
    for rel in snapshot.get("relationships_out", []):
        if rel["type"] == rel_type and rel["target_task_id"] == target_task_id:
            found = True
            break

    if not found:
        output_error(
            f"No {rel_type} relationship to {target_task_id}.",
            "NOT_FOUND",
            is_json,
        )

    # Build event
    event_data: dict = {
        "type": rel_type,
        "target_task_id": target_task_id,
    }

    event = create_event(
        type="relationship_removed",
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

    # Write (event-first, then snapshot, under lock)
    write_task_event(lattice_dir, task_id, [event], updated_snapshot, config)

    # Output
    output_result(
        data=updated_snapshot,
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
    actor: str,
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

    # Input validation
    _validate_branch_name(branch, is_json)
    # Normalize empty repo to None
    if repo is not None and not repo.strip():
        repo = None

    lattice_dir = require_root(is_json)
    config = load_project_config(lattice_dir)
    validate_actor_or_exit(actor, is_json)
    if on_behalf_of is not None:
        validate_actor_or_exit(on_behalf_of, is_json)

    task_id = resolve_task_id(lattice_dir, task_id, is_json)

    # Build event (branch/repo are validated; event created before lock for timestamp)
    event_data: dict = {"branch": branch}
    if repo is not None:
        event_data["repo"] = repo

    event = create_event(
        type="branch_linked",
        task_id=task_id,
        actor=actor,
        data=event_data,
        model=model,
        session=session,
        triggered_by=triggered_by,
        on_behalf_of=on_behalf_of,
        reason=provenance_reason,
    )

    # Acquire lock, then read snapshot + check + write atomically
    locks_dir = lattice_dir / "locks"
    lock_keys = sorted([f"events_{task_id}", f"tasks_{task_id}"])

    with multi_lock(locks_dir, lock_keys):
        # Read snapshot inside lock
        snapshot = read_snapshot_or_exit(lattice_dir, task_id, is_json)

        # Reject duplicates: same (branch, repo) pair
        for bl in snapshot.get("branch_links", []):
            if bl["branch"] == branch and bl.get("repo") == repo:
                repo_display = f" (repo: {repo})" if repo else ""
                output_error(
                    f"Duplicate: branch '{branch}'{repo_display} already linked to {task_id}.",
                    "CONFLICT",
                    is_json,
                )

        updated_snapshot = apply_event_to_snapshot(snapshot, event)

        # Event-first write
        event_path = lattice_dir / "events" / f"{task_id}.jsonl"
        jsonl_append(event_path, serialize_event(event))

        snapshot_path = lattice_dir / "tasks" / f"{task_id}.json"
        atomic_write(snapshot_path, serialize_snapshot(updated_snapshot))

    # Fire hooks after locks released
    if config:
        execute_hooks(config, lattice_dir, task_id, event)

    # Output
    repo_display = f" (repo: {repo})" if repo else ""
    output_result(
        data=updated_snapshot,
        human_message=f"Linked branch '{branch}'{repo_display} to {task_id}",
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
    actor: str,
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

    # Input validation
    _validate_branch_name(branch, is_json)
    # Normalize empty repo to None
    if repo is not None and not repo.strip():
        repo = None

    lattice_dir = require_root(is_json)
    config = load_project_config(lattice_dir)
    validate_actor_or_exit(actor, is_json)
    if on_behalf_of is not None:
        validate_actor_or_exit(on_behalf_of, is_json)

    task_id = resolve_task_id(lattice_dir, task_id, is_json)

    # Build event (branch/repo are validated; event created before lock for timestamp)
    event_data: dict = {"branch": branch}
    if repo is not None:
        event_data["repo"] = repo

    event = create_event(
        type="branch_unlinked",
        task_id=task_id,
        actor=actor,
        data=event_data,
        model=model,
        session=session,
        triggered_by=triggered_by,
        on_behalf_of=on_behalf_of,
        reason=provenance_reason,
    )

    # Acquire lock, then read snapshot + check + write atomically
    locks_dir = lattice_dir / "locks"
    lock_keys = sorted([f"events_{task_id}", f"tasks_{task_id}"])

    with multi_lock(locks_dir, lock_keys):
        # Read snapshot inside lock
        snapshot = read_snapshot_or_exit(lattice_dir, task_id, is_json)

        # Validate the branch link exists
        found = False
        for bl in snapshot.get("branch_links", []):
            if bl["branch"] == branch and bl.get("repo") == repo:
                found = True
                break

        if not found:
            repo_display = f" (repo: {repo})" if repo else ""
            output_error(
                f"No branch link '{branch}'{repo_display} on {task_id}.",
                "NOT_FOUND",
                is_json,
            )

        updated_snapshot = apply_event_to_snapshot(snapshot, event)

        # Event-first write
        event_path = lattice_dir / "events" / f"{task_id}.jsonl"
        jsonl_append(event_path, serialize_event(event))

        snapshot_path = lattice_dir / "tasks" / f"{task_id}.json"
        atomic_write(snapshot_path, serialize_snapshot(updated_snapshot))

    # Fire hooks after locks released
    if config:
        execute_hooks(config, lattice_dir, task_id, event)

    # Output
    repo_display = f" (repo: {repo})" if repo else ""
    output_result(
        data=updated_snapshot,
        human_message=f"Unlinked branch '{branch}'{repo_display} from {task_id}",
        quiet_value=task_id,
        is_json=is_json,
        is_quiet=quiet,
    )
