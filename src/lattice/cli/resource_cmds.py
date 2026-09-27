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
    require_root,
    resolve_resource,
)
from lattice.cli.main import cli
from lattice.cli.ops_bridge import board_or_exit, caller_from_context, run_operation
from lattice.core.resources import (
    format_duration_ago,
    format_duration_remaining,
    is_holder_stale,
)
from lattice.ops import OpError


# ---------------------------------------------------------------------------
# Resource command group
# ---------------------------------------------------------------------------


@cli.group()
def resource() -> None:
    """Manage shared resources (locks, coordination)."""


# ---------------------------------------------------------------------------
# lattice resource create
# ---------------------------------------------------------------------------


def _provenance(
    model: str | None,
    session: str | None,
    triggered_by: str | None,
    on_behalf_of: str | None,
    reason: str | None,
) -> dict:
    """The ``common_options`` provenance flags as operation params."""
    return {
        "model": model,
        "session": session,
        "triggered_by": triggered_by,
        "on_behalf_of": on_behalf_of,
        "reason": reason,
    }


@resource.command("create")
@click.argument("name")
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
    result = run_operation(
        "resource.create",
        {
            "name": name,
            "description": description,
            "max_holders": max_holders,
            "ttl": ttl,
            "id": resource_id,
            **_provenance(model, session, triggered_by, on_behalf_of, provenance_reason),
        },
        is_json,
    )
    if result.idempotent:
        message = f"Resource '{name}' already exists ({result.resource_id})"
    else:
        message = f"Created resource '{name}' ({result.resource_id})"
    output_result(
        data=result.value,
        human_message=message,
        quiet_value=result.resource_id,
        is_json=is_json,
        is_quiet=quiet,
    )


# ---------------------------------------------------------------------------
# lattice resource acquire
# ---------------------------------------------------------------------------


@resource.command("acquire")
@click.argument("name")
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
    board = board_or_exit(is_json)
    params = {
        "name": name,
        "task": task_id,
        "force": force,
        **_provenance(model, session, triggered_by, on_behalf_of, provenance_reason),
    }

    # Each attempt is its own resource.acquire call (its own op_id and lock);
    # nothing is held between attempts, so a release can land while we wait.
    start_time = time.monotonic()
    poll_interval = 0.1  # start at 100ms
    while True:
        try:
            result = board.execute("resource.acquire", params, caller_from_context())
            break
        except OpError as exc:
            if exc.code != "RESOURCE_HELD" or not do_wait:
                output_error(exc.message, exc.code, is_json)
        elapsed = time.monotonic() - start_time
        if elapsed >= timeout:
            output_error(
                f"Timed out waiting for resource '{name}' after {timeout}s.",
                "TIMEOUT",
                is_json,
            )
        time.sleep(min(poll_interval, timeout - elapsed))
        poll_interval = min(poll_interval * 2, 1.0)  # backoff to 1s max

    last = result.events[-1]
    if last["type"] == "resource_heartbeat":
        message = f"Already holding '{result.resource_name}' (TTL extended)"
    else:
        remaining = format_duration_remaining(last["data"]["expires_at"], last["ts"])
        message = f"Acquired '{result.resource_name}' (expires {remaining})"
    output_result(
        data=result.value,
        human_message=message,
        quiet_value=result.resource_id,
        is_json=is_json,
        is_quiet=quiet,
    )


# ---------------------------------------------------------------------------
# lattice resource release
# ---------------------------------------------------------------------------


@resource.command("release")
@click.argument("name")
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
    result = run_operation(
        "resource.release",
        {
            "name": name,
            **_provenance(model, session, triggered_by, on_behalf_of, provenance_reason),
        },
        is_json,
    )
    output_result(
        data=result.value,
        human_message=f"Released '{result.resource_name}'",
        quiet_value=result.resource_id,
        is_json=is_json,
        is_quiet=quiet,
    )


# ---------------------------------------------------------------------------
# lattice resource heartbeat
# ---------------------------------------------------------------------------


@resource.command("heartbeat")
@click.argument("name")
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
    result = run_operation(
        "resource.heartbeat",
        {
            "name": name,
            **_provenance(model, session, triggered_by, on_behalf_of, provenance_reason),
        },
        is_json,
    )
    event = result.events[-1]
    remaining = format_duration_remaining(event["data"]["expires_at"], event["ts"])
    output_result(
        data=result.value,
        human_message=f"Heartbeat: '{result.resource_name}' TTL extended to {remaining}",
        quiet_value=result.resource_id,
        is_json=is_json,
        is_quiet=quiet,
    )


# ---------------------------------------------------------------------------
# lattice resource status / list (read-only, no locking needed)
# ---------------------------------------------------------------------------


@resource.command("status")
@click.argument("name", required=False, default=None)
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


def _filter_active_holders(snapshot: dict, now: str) -> list[dict]:
    """Return holders that are not expired at *now*."""
    return [h for h in snapshot.get("holders", []) if not is_holder_stale(h, now)]


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
