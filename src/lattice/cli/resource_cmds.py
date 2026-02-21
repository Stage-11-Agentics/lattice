"""CLI commands for resource coordination (create, acquire, release, heartbeat, status)."""

from __future__ import annotations

import copy
import time
from pathlib import Path

import click

from lattice.cli.helpers import (
    common_options,
    list_all_resources,
    load_project_config,
    output_error,
    output_result,
    read_resource_snapshot,
    require_actor,
    require_root,
    resolve_resource,
)
from lattice.cli.main import cli
from lattice.completion import complete_resource_name
from lattice.core.events import create_resource_event
from lattice.core.ids import generate_resource_id, validate_id
from lattice.core.resources import (
    apply_resource_event_to_snapshot,
    compute_expires_at,
    evict_stale_holders,
    find_holder,
    format_duration_ago,
    format_duration_remaining,
    is_holder_stale,
    is_resource_available,
)
from lattice.storage.operations import resource_write_context, write_resource_event


# ---------------------------------------------------------------------------
# Resource command group
# ---------------------------------------------------------------------------


@cli.group()
def resource() -> None:
    """Manage shared resources (locks, coordination)."""


# ---------------------------------------------------------------------------
# lattice resource create
# ---------------------------------------------------------------------------


@resource.command("create")
@click.argument("name", shell_complete=complete_resource_name)
@click.option("--description", default=None, help="Human-readable description.")
@click.option("--max-holders", type=int, default=1, help="Max concurrent holders (default 1).")
@click.option("--ttl", type=int, default=300, help="Lock TTL in seconds (default 300).")
@click.option("--id", "resource_id", default=None, help="Caller-supplied resource ID.")
@common_options
def resource_create(
    name: str,
    description: str | None,
    max_holders: int,
    ttl: int,
    resource_id: str | None,
    output_json: bool,
    quiet: bool,
    session: str | None,
    model: str | None,
    triggered_by: str | None,
    on_behalf_of: str | None,
    provenance_reason: str | None,
) -> None:
    """Create a new resource."""
    is_json = output_json
    lattice_dir = require_root(is_json)
    actor = require_actor(is_json)

    # Validate ID format before locking
    if resource_id:
        if not validate_id(resource_id, "res"):
            output_error(
                f"Invalid resource ID format: '{resource_id}'.",
                "INVALID_ID",
                is_json,
            )
    # Validate max_holders
    if max_holders < 1:
        output_error("--max-holders must be at least 1.", "VALIDATION_ERROR", is_json)
    # Validate TTL
    if ttl < 1:
        output_error("--ttl must be at least 1 second.", "VALIDATION_ERROR", is_json)

    # Lock: read-check-write atomically
    with resource_write_context(lattice_dir, name):
        existing = read_resource_snapshot(lattice_dir, name)
        if existing is not None:
            if resource_id and existing.get("id") == resource_id:
                # Idempotent: same ID, same resource
                output_result(
                    data=existing,
                    human_message=f"Resource '{name}' already exists ({existing['id']})",
                    quiet_value=existing["id"],
                    is_json=is_json,
                    is_quiet=quiet,
                )
                return
            output_error(
                f"Resource '{name}' already exists ({existing['id']}).",
                "CONFLICT",
                is_json,
            )

        # Check caller-supplied ID uniqueness across all resources
        if resource_id:
            _check_id_uniqueness(lattice_dir, resource_id, is_json)
        else:
            resource_id = generate_resource_id()

        # Build event
        data = {
            "name": name,
            "max_holders": max_holders,
            "ttl_seconds": ttl,
        }
        if description:
            data["description"] = description

        event = create_resource_event(
            "resource_created",
            resource_id,
            actor,
            data,
            model=model,
            session=session,
            triggered_by=triggered_by,
            on_behalf_of=on_behalf_of,
            reason=provenance_reason,
        )

        snapshot = apply_resource_event_to_snapshot(None, event)
        config = load_project_config(lattice_dir)
        write_resource_event(
            lattice_dir,
            resource_id,
            name,
            [event],
            snapshot,
            config,
            _caller_holds_lock=True,
        )

    output_result(
        data=snapshot,
        human_message=f"Created resource '{name}' ({resource_id})",
        quiet_value=resource_id,
        is_json=is_json,
        is_quiet=quiet,
    )


# ---------------------------------------------------------------------------
# lattice resource acquire
# ---------------------------------------------------------------------------


@resource.command("acquire")
@click.argument("name", shell_complete=complete_resource_name)
@click.option("--task", "task_id", default=None, help="Link to a task (e.g., LAT-88).")
@click.option("--force", is_flag=True, help="Evict current holder.")
@click.option("--wait", "do_wait", is_flag=True, help="Poll until available.")
@click.option("--timeout", type=int, default=60, help="Max wait time in seconds (default 60).")
@common_options
def resource_acquire(
    name: str,
    task_id: str | None,
    force: bool,
    do_wait: bool,
    timeout: int,
    output_json: bool,
    quiet: bool,
    session: str | None,
    model: str | None,
    triggered_by: str | None,
    on_behalf_of: str | None,
    provenance_reason: str | None,
) -> None:
    """Acquire exclusive access to a resource."""
    is_json = output_json
    lattice_dir = require_root(is_json)
    actor = require_actor(is_json)
    config = load_project_config(lattice_dir)

    # Resolve task short ID if provided
    if task_id:
        from lattice.cli.helpers import resolve_task_id

        task_id = resolve_task_id(lattice_dir, task_id, is_json)

    # Common event kwargs
    event_kwargs = dict(
        model=model,
        session=session,
        triggered_by=triggered_by,
        on_behalf_of=on_behalf_of,
        reason=provenance_reason,
    )

    # Try to acquire (with optional wait loop)
    start_time = time.monotonic()
    poll_interval = 0.1  # start at 100ms

    while True:
        # Lock per-iteration: read, check, write atomically.
        # Lock is released between polls so other operations (release) can proceed.
        with resource_write_context(lattice_dir, name):
            # Resolve resource under lock (handles auto-create from config)
            resource_id, resource_name, snapshot = resolve_resource(lattice_dir, name, is_json)

            # Auto-create from config if needed (under same lock)
            if not resource_id:
                resource_id, resource_name, snapshot = _auto_create_resource(
                    lattice_dir,
                    resource_name,
                    actor,
                    config,
                    is_json,
                    **event_kwargs,
                )

            assert snapshot is not None

            from lattice.core.events import utc_now

            now = utc_now()
            events_to_write: list[dict] = []

            # Evict stale holders
            stale = evict_stale_holders(snapshot, now)
            for stale_holder in stale:
                exp_event = create_resource_event(
                    "resource_expired",
                    resource_id,
                    actor,
                    {
                        "holder": stale_holder["actor"],
                        "expired_at": stale_holder.get("expires_at", now),
                        "reclaimed_by": actor,
                    },
                    ts=now,
                    **event_kwargs,
                )
                snapshot = apply_resource_event_to_snapshot(snapshot, exp_event)
                events_to_write.append(exp_event)

            # Check if actor already holds it (idempotent)
            existing_holder = find_holder(snapshot, actor)
            if existing_holder is not None:
                # Extend TTL
                new_expires = compute_expires_at(snapshot["ttl_seconds"], now)
                hb_event = create_resource_event(
                    "resource_heartbeat",
                    resource_id,
                    actor,
                    {"holder": actor, "expires_at": new_expires},
                    ts=now,
                    **event_kwargs,
                )
                snapshot = apply_resource_event_to_snapshot(snapshot, hb_event)
                events_to_write.append(hb_event)

                if events_to_write:
                    write_resource_event(
                        lattice_dir,
                        resource_id,
                        resource_name,
                        events_to_write,
                        snapshot,
                        config,
                        _caller_holds_lock=True,
                    )

                output_result(
                    data=snapshot,
                    human_message=f"Already holding '{resource_name}' (TTL extended)",
                    quiet_value=resource_id,
                    is_json=is_json,
                    is_quiet=quiet,
                )
                return

            # Force eviction
            if force and snapshot.get("holders"):
                for h in list(snapshot.get("holders", [])):
                    exp_event = create_resource_event(
                        "resource_expired",
                        resource_id,
                        actor,
                        {
                            "holder": h["actor"],
                            "expired_at": now,
                            "reclaimed_by": actor,
                        },
                        ts=now,
                        **event_kwargs,
                    )
                    snapshot = apply_resource_event_to_snapshot(snapshot, exp_event)
                    events_to_write.append(exp_event)

            # Check availability
            if is_resource_available(snapshot, now):
                expires_at = compute_expires_at(snapshot["ttl_seconds"], now)
                acq_data: dict = {
                    "holder": actor,
                    "expires_at": expires_at,
                }
                if task_id:
                    acq_data["task_id"] = task_id
                if provenance_reason:
                    acq_data["reason"] = provenance_reason

                acq_event = create_resource_event(
                    "resource_acquired",
                    resource_id,
                    actor,
                    acq_data,
                    ts=now,
                    **event_kwargs,
                )
                snapshot = apply_resource_event_to_snapshot(snapshot, acq_event)
                events_to_write.append(acq_event)

                write_resource_event(
                    lattice_dir,
                    resource_id,
                    resource_name,
                    events_to_write,
                    snapshot,
                    config,
                    _caller_holds_lock=True,
                )

                output_result(
                    data=snapshot,
                    human_message=f"Acquired '{resource_name}' (expires {format_duration_remaining(expires_at, now)})",
                    quiet_value=resource_id,
                    is_json=is_json,
                    is_quiet=quiet,
                )
                return

            # Write any stale eviction events even if we can't acquire yet
            if events_to_write:
                write_resource_event(
                    lattice_dir,
                    resource_id,
                    resource_name,
                    events_to_write,
                    snapshot,
                    config,
                    _caller_holds_lock=True,
                )

            # Capture holder info for error message (while still under lock)
            holders = snapshot.get("holders", [])
            holder_info = ""
            if holders:
                h = holders[0]
                holder_info = f" Held by {h['actor']}"
                if h.get("task_id"):
                    holder_info += f" ({h['task_id']})"
                holder_info += f" since {format_duration_ago(h['acquired_at'], now)}"
                holder_info += f", expires {format_duration_remaining(h['expires_at'], now)}"

        # --- Lock released ---

        # Not available and not waiting
        if not do_wait:
            output_error(
                f"Resource '{name}' is not available.{holder_info}",
                "RESOURCE_HELD",
                is_json,
            )

        # Wait mode: check timeout
        elapsed = time.monotonic() - start_time
        if elapsed >= timeout:
            output_error(
                f"Timed out waiting for resource '{name}' after {timeout}s.",
                "TIMEOUT",
                is_json,
            )

        time.sleep(min(poll_interval, timeout - elapsed))
        poll_interval = min(poll_interval * 2, 1.0)  # backoff to 1s max


# ---------------------------------------------------------------------------
# lattice resource release
# ---------------------------------------------------------------------------


@resource.command("release")
@click.argument("name", shell_complete=complete_resource_name)
@common_options
def resource_release(
    name: str,
    output_json: bool,
    quiet: bool,
    session: str | None,
    model: str | None,
    triggered_by: str | None,
    on_behalf_of: str | None,
    provenance_reason: str | None,
) -> None:
    """Release a held resource."""
    is_json = output_json
    lattice_dir = require_root(is_json)
    actor = require_actor(is_json)
    config = load_project_config(lattice_dir)

    # Lock: read-check-write atomically
    with resource_write_context(lattice_dir, name):
        resource_id, resource_name, snapshot = resolve_resource(lattice_dir, name, is_json)
        if snapshot is None:
            output_error(f"Resource '{name}' does not exist.", "NOT_FOUND", is_json)

        # Verify actor holds it
        holder = find_holder(snapshot, actor)
        if holder is None:
            output_error(
                f"You ({actor}) do not hold resource '{resource_name}'.",
                "NOT_HELD",
                is_json,
            )

        from lattice.core.events import utc_now

        now = utc_now()

        rel_data: dict = {"holder": actor}
        if holder.get("task_id"):
            rel_data["task_id"] = holder["task_id"]
        if provenance_reason:
            rel_data["reason"] = provenance_reason

        event = create_resource_event(
            "resource_released",
            resource_id,
            actor,
            rel_data,
            ts=now,
            model=model,
            session=session,
            triggered_by=triggered_by,
            on_behalf_of=on_behalf_of,
            reason=provenance_reason,
        )

        snapshot = apply_resource_event_to_snapshot(snapshot, event)
        write_resource_event(
            lattice_dir,
            resource_id,
            resource_name,
            [event],
            snapshot,
            config,
            _caller_holds_lock=True,
        )

    output_result(
        data=snapshot,
        human_message=f"Released '{resource_name}'",
        quiet_value=resource_id,
        is_json=is_json,
        is_quiet=quiet,
    )


# ---------------------------------------------------------------------------
# lattice resource heartbeat
# ---------------------------------------------------------------------------


@resource.command("heartbeat")
@click.argument("name", shell_complete=complete_resource_name)
@common_options
def resource_heartbeat(
    name: str,
    output_json: bool,
    quiet: bool,
    session: str | None,
    model: str | None,
    triggered_by: str | None,
    on_behalf_of: str | None,
    provenance_reason: str | None,
) -> None:
    """Extend TTL on a held resource."""
    is_json = output_json
    lattice_dir = require_root(is_json)
    actor = require_actor(is_json)
    config = load_project_config(lattice_dir)

    # Lock: read-check-write atomically
    with resource_write_context(lattice_dir, name):
        resource_id, resource_name, snapshot = resolve_resource(lattice_dir, name, is_json)
        if snapshot is None:
            output_error(f"Resource '{name}' does not exist.", "NOT_FOUND", is_json)

        # Verify actor holds it
        holder = find_holder(snapshot, actor)
        if holder is None:
            output_error(
                f"You ({actor}) do not hold resource '{resource_name}'.",
                "NOT_HELD",
                is_json,
            )

        from lattice.core.events import utc_now

        now = utc_now()

        # Reject heartbeat on expired holders — must re-acquire
        if is_holder_stale(holder, now):
            output_error(
                f"Your hold on '{resource_name}' has expired. Use 'lattice resource acquire' to re-acquire.",
                "EXPIRED",
                is_json,
            )

        new_expires = compute_expires_at(snapshot["ttl_seconds"], now)

        event = create_resource_event(
            "resource_heartbeat",
            resource_id,
            actor,
            {"holder": actor, "expires_at": new_expires},
            ts=now,
            model=model,
            session=session,
            triggered_by=triggered_by,
            on_behalf_of=on_behalf_of,
            reason=provenance_reason,
        )

        snapshot = apply_resource_event_to_snapshot(snapshot, event)
        write_resource_event(
            lattice_dir,
            resource_id,
            resource_name,
            [event],
            snapshot,
            config,
            _caller_holds_lock=True,
        )

    output_result(
        data=snapshot,
        human_message=f"Heartbeat: '{resource_name}' TTL extended to {format_duration_remaining(new_expires, now)}",
        quiet_value=resource_id,
        is_json=is_json,
        is_quiet=quiet,
    )


# ---------------------------------------------------------------------------
# lattice resource status / list (read-only, no locking needed)
# ---------------------------------------------------------------------------


@resource.command("status")
@click.argument("name", required=False, default=None, shell_complete=complete_resource_name)
@click.option("--json", "output_json", is_flag=True, help="Output structured JSON.")
def resource_status(name: str | None, output_json: bool) -> None:
    """Show resource status. No args = all resources."""
    is_json = output_json
    lattice_dir = require_root(is_json)

    if name:
        _show_single_resource(lattice_dir, name, is_json)
    else:
        _show_all_resources(lattice_dir, is_json)


@resource.command("list")
@click.option("--json", "output_json", is_flag=True, help="Output structured JSON.")
def resource_list(output_json: bool) -> None:
    """List all resources and their status."""
    is_json = output_json
    lattice_dir = require_root(is_json)
    _show_all_resources(lattice_dir, is_json)


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _check_id_uniqueness(lattice_dir: Path, resource_id: str, is_json: bool) -> None:
    """Verify no existing resource uses this ID (different name, same ID = corruption)."""
    all_resources = list_all_resources(lattice_dir)
    for r in all_resources:
        if r.get("id") == resource_id:
            output_error(
                f"Resource ID '{resource_id}' is already used by resource '{r.get('name')}'.",
                "CONFLICT",
                is_json,
            )


def _filter_active_holders(snapshot: dict, now: str) -> list[dict]:
    """Return holders that are not expired at *now*."""
    return [h for h in snapshot.get("holders", []) if not is_holder_stale(h, now)]


def _auto_create_resource(
    lattice_dir: Path,
    resource_name: str,
    actor: str,
    config: dict,
    is_json: bool,
    *,
    model: str | None = None,
    session: str | None = None,
    triggered_by: str | None = None,
    on_behalf_of: str | None = None,
    reason: str | None = None,
) -> tuple[str, str, dict]:
    """Auto-create a resource from config definition.

    Called under resource_write_context — re-checks existence to prevent
    races where two concurrent first-acquires both try to auto-create.

    Returns (resource_id, name, snapshot).
    """
    # Re-check existence under lock
    existing = read_resource_snapshot(lattice_dir, resource_name)
    if existing is not None:
        return existing["id"], resource_name, existing

    config_resources = config.get("resources", {})
    res_def = config_resources.get(resource_name, {})

    resource_id = generate_resource_id()
    data = {
        "name": resource_name,
        "max_holders": res_def.get("max_holders", 1),
        "ttl_seconds": res_def.get("ttl_seconds", 300),
    }
    desc = res_def.get("description")
    if desc:
        data["description"] = desc

    event = create_resource_event(
        "resource_created",
        resource_id,
        actor,
        data,
        model=model,
        session=session,
        triggered_by=triggered_by,
        on_behalf_of=on_behalf_of,
        reason=reason,
    )

    snapshot = apply_resource_event_to_snapshot(None, event)
    write_resource_event(
        lattice_dir,
        resource_id,
        resource_name,
        [event],
        snapshot,
        config,
        _caller_holds_lock=True,
    )
    return resource_id, resource_name, snapshot


def _show_single_resource(lattice_dir: Path, name: str, is_json: bool) -> None:
    """Display status for a single resource."""
    from lattice.cli.helpers import json_envelope

    resource_id, resource_name, snapshot = resolve_resource(lattice_dir, name, is_json)
    if snapshot is None:
        output_error(f"Resource '{name}' does not exist.", "NOT_FOUND", is_json)

    from lattice.core.events import utc_now

    now = utc_now()
    active_holders = _filter_active_holders(snapshot, now)

    if is_json:
        # Return snapshot with only active holders for consistency with text output
        filtered = copy.deepcopy(snapshot)
        filtered["holders"] = active_holders
        click.echo(json_envelope(True, data=filtered))
    else:
        status_str = "HELD" if active_holders else "available"
        line = f"{resource_name:<20} {status_str:<12} max:{snapshot.get('max_holders', 1)}  ttl:{snapshot.get('ttl_seconds', 300)}s"
        if snapshot.get("description"):
            line += f'  "{snapshot["description"]}"'
        click.echo(line)
        for h in active_holders:
            holder_line = f"  held by {h['actor']}"
            if h.get("task_id"):
                holder_line += f" ({h['task_id']})"
            holder_line += f" since {format_duration_ago(h['acquired_at'], now)}"
            if h.get("expires_at"):
                holder_line += f", expires {format_duration_remaining(h['expires_at'], now)}"
            click.echo(holder_line)


def _show_all_resources(lattice_dir: Path, is_json: bool) -> None:
    """Display status for all resources."""
    from lattice.cli.helpers import json_envelope

    resources = list_all_resources(lattice_dir)

    # Also include config-declared resources that haven't been created yet
    config = load_project_config(lattice_dir)
    config_resources = config.get("resources", {})
    existing_names = {r.get("name") for r in resources}
    for cfg_name, cfg_def in config_resources.items():
        if cfg_name not in existing_names:
            resources.append(
                {
                    "name": cfg_name,
                    "description": cfg_def.get("description"),
                    "max_holders": cfg_def.get("max_holders", 1),
                    "ttl_seconds": cfg_def.get("ttl_seconds", 300),
                    "holders": [],
                    "_config_only": True,
                }
            )

    from lattice.core.events import utc_now

    now = utc_now()

    if is_json:
        # Filter stale holders in JSON output for consistency
        filtered_resources = []
        for r in resources:
            rc = copy.deepcopy(r)
            rc["holders"] = _filter_active_holders(rc, now)
            rc.pop("_config_only", None)
            filtered_resources.append(rc)
        click.echo(json_envelope(True, data={"resources": filtered_resources}))
        return

    if not resources:
        click.echo("No resources defined.")
        return

    for r in resources:
        rname = r.get("name", "?")
        active_holders = _filter_active_holders(r, now)

        if active_holders:
            h = active_holders[0]
            status_str = f"HELD by {h['actor']}"
            if h.get("task_id"):
                status_str += f" ({h['task_id']})"
            status_str += f" since {format_duration_ago(h['acquired_at'], now)}"
            if h.get("expires_at"):
                status_str += f", expires {format_duration_remaining(h['expires_at'], now)}"
        else:
            status_str = "available"

        line = f"{rname:<20} {status_str}"
        desc = r.get("description")
        if desc and not active_holders:
            line += f'  "{desc}"'

        max_h = r.get("max_holders", 1)
        ttl_val = r.get("ttl_seconds", 300)
        line += f"  max:{max_h}  ttl:{ttl_val}s"

        if r.get("_config_only"):
            line += "  (config-only, auto-creates on acquire)"

        click.echo(line)
