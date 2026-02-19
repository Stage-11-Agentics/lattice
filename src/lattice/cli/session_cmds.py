"""CLI commands for session management (start, end, list)."""

from __future__ import annotations

import json

import click

from lattice.cli.helpers import (
    output_error,
    require_root,
)
from lattice.cli.main import cli
from lattice.storage.sessions import (
    create_session,
    end_session,
    list_sessions,
    resolve_session,
)


# ---------------------------------------------------------------------------
# Session command group
# ---------------------------------------------------------------------------


@cli.group()
def session() -> None:
    """Manage actor sessions (identity registration)."""


# ---------------------------------------------------------------------------
# lattice session start
# ---------------------------------------------------------------------------


@session.command("start")
@click.option("--name", "base_name", default=None, help="Self-chosen name (e.g., 'Argus').")
@click.option("--model", required=True, help="Model identifier ('human', 'claude-opus-4', etc.).")
@click.option(
    "--framework", default=None, help="Framework/interface ('claude-code', 'codex-cli', etc.)."
)
@click.option(
    "--agent-type", default=None, help="Categorical type for grouping ('advance', 'review', etc.)."
)
@click.option("--prompt", default=None, help="Active skill/workflow.")
@click.option("--parent", default=None, help="Who launched this agent (e.g., 'human:atin').")
@click.option("--json", "output_json", is_flag=True, help="Output structured JSON.")
@click.option("--quiet", is_flag=True, help="Print only the disambiguated name.")
def session_start(
    base_name: str | None,
    model: str,
    framework: str | None,
    agent_type: str | None,
    prompt: str | None,
    parent: str | None,
    output_json: bool,
    quiet: bool,
) -> None:
    """Register a new session and get a disambiguated name."""
    lattice_dir = require_root(output_json)

    try:
        identity = create_session(
            lattice_dir,
            base_name=base_name,
            agent_type=agent_type,
            model=model,
            framework=framework,
            prompt=prompt,
            parent=parent,
        )
    except ValueError as e:
        output_error(str(e), "VALIDATION_ERROR", output_json)

    if output_json:
        envelope = {
            "ok": True,
            "data": {
                "name": identity.name,
                "base_name": identity.base_name,
                "serial": identity.serial,
                "session": identity.session,
                "model": identity.model,
                "framework": identity.framework,
            },
        }
        click.echo(json.dumps(envelope, sort_keys=True, indent=2))
    elif quiet:
        click.echo(identity.name)
    else:
        click.echo(f"Session created: {identity.name}")
        click.echo(f"  Serial: {identity.serial}", nl=False)
        if identity.serial > 1:
            click.echo(f" ({identity.serial - 1} previous {identity.base_name} session(s))")
        else:
            click.echo()
        click.echo(f"  Session ID: {identity.session}")
        click.echo(f"  Model: {identity.model}")
        if identity.framework:
            click.echo(f"  Framework: {identity.framework}")


# ---------------------------------------------------------------------------
# lattice session end
# ---------------------------------------------------------------------------


@session.command("end")
@click.argument("name")
@click.option("--reason", default=None, help="Reason for ending the session.")
@click.option("--json", "output_json", is_flag=True, help="Output structured JSON.")
def session_end(
    name: str,
    reason: str | None,
    output_json: bool,
) -> None:
    """End an active session and archive it."""
    lattice_dir = require_root(output_json)

    success = end_session(lattice_dir, name, reason=reason)
    if not success:
        output_error(f"No active session named '{name}'.", "NOT_FOUND", output_json)

    if output_json:
        envelope = {"ok": True, "data": {"name": name, "status": "ended"}}
        click.echo(json.dumps(envelope, sort_keys=True, indent=2))
    else:
        click.echo(f"Session ended: {name}")
        if reason:
            click.echo(f"  Reason: {reason}")


# ---------------------------------------------------------------------------
# lattice session list
# ---------------------------------------------------------------------------


@session.command("list")
@click.option("--json", "output_json", is_flag=True, help="Output structured JSON.")
def session_list(
    output_json: bool,
) -> None:
    """List all active sessions."""
    lattice_dir = require_root(output_json)

    sessions = list_sessions(lattice_dir)

    if output_json:
        envelope = {"ok": True, "data": {"sessions": sessions, "count": len(sessions)}}
        click.echo(json.dumps(envelope, sort_keys=True, indent=2))
    else:
        if not sessions:
            click.echo("No active sessions.")
            return

        click.echo(f"Active sessions ({len(sessions)}):")
        click.echo()
        for s in sessions:
            name = s.get("name", "?")
            model = s.get("model", "?")
            framework = s.get("framework", "")
            last_active = s.get("last_active", "?")
            agent_type = s.get("agent_type", "")

            label = f"  {name}"
            meta_parts = [model]
            if framework:
                meta_parts.append(framework)
            if agent_type:
                meta_parts.append(f"type={agent_type}")
            label += f"  ({', '.join(meta_parts)})"
            label += f"  last active: {last_active}"

            click.echo(label)


# ---------------------------------------------------------------------------
# lattice session show
# ---------------------------------------------------------------------------


@session.command("show")
@click.argument("name")
@click.option("--json", "output_json", is_flag=True, help="Output structured JSON.")
def session_show(
    name: str,
    output_json: bool,
) -> None:
    """Show details of an active session."""
    lattice_dir = require_root(output_json)

    data = resolve_session(lattice_dir, name)
    if data is None:
        output_error(f"No active session named '{name}'.", "NOT_FOUND", output_json)

    if output_json:
        envelope = {"ok": True, "data": data}
        click.echo(json.dumps(envelope, sort_keys=True, indent=2))
    else:
        click.echo(f"Session: {data.get('name', '?')}")
        click.echo(f"  Base name: {data.get('base_name', '?')}")
        click.echo(f"  Serial: {data.get('serial', '?')}")
        click.echo(f"  Session ID: {data.get('session', '?')}")
        click.echo(f"  Model: {data.get('model', '?')}")
        if data.get("framework"):
            click.echo(f"  Framework: {data['framework']}")
        if data.get("agent_type"):
            click.echo(f"  Agent type: {data['agent_type']}")
        if data.get("prompt"):
            click.echo(f"  Prompt: {data['prompt']}")
        if data.get("parent"):
            click.echo(f"  Parent: {data['parent']}")
        click.echo(f"  Status: {data.get('status', '?')}")
        click.echo(f"  Started: {data.get('started_at', '?')}")
        click.echo(f"  Last active: {data.get('last_active', '?')}")
