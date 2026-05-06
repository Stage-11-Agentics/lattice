"""Alert commands: raise and clear orthogonal "needs attention" markers.

Alerts are the LAT-210 mechanism that replaces ``needs_human`` and ``blocked``
as statuses.  An alert is a structured payload attached to a task at any
status — it does not move the task in the workflow.  The c11 integration
(see ``cmux_bridge.raise_alert_visual`` / ``clear_alert_visual``) wires the
sidebar pill, optional flash, and OS notification.
"""

from __future__ import annotations

from pathlib import Path

import click

from lattice.cli.helpers import (
    common_options,
    json_envelope,
    load_project_config,
    output_error,
    output_result,
    read_snapshot,
    require_actor,
    require_root,
    resolve_task_id,
    validate_actor_format_or_exit,
    write_task_event,
)
from lattice.cli.main import cli
from lattice.core.config import (
    get_alert_visual,
    get_workflow_alerts,
    validate_alert_name,
)
from lattice.core.events import (
    ALERT_LONG_MAX,
    ALERT_PROMPT_MAX,
    ALERT_SHORT_MAX,
    create_event,
    validate_alert_payload,
)
from lattice.core.tasks import apply_event_to_snapshot


_TERMINAL_STATUSES: frozenset[str] = frozenset({"done", "cancelled"})


def _is_archived(lattice_dir: Path, task_id: str) -> bool:
    return (lattice_dir / "archive" / "tasks" / f"{task_id}.json").exists()


def _resolve_long_text(
    long_text: str | None,
    long_from_file: str | None,
    is_json: bool,
) -> str | None:
    if long_text is not None and long_from_file is not None:
        output_error(
            "--long and --long-from-file are mutually exclusive.",
            "VALIDATION_ERROR",
            is_json,
        )
    if long_from_file is not None:
        try:
            return Path(long_from_file).read_text(encoding="utf-8")
        except OSError as exc:
            output_error(
                f"Cannot read --long-from-file: {exc}",
                "VALIDATION_ERROR",
                is_json,
            )
    return long_text


def _maybe_raise_visual(
    *,
    use_c11: bool,
    surface_override: str | None,
    workflow_visuals_override: dict | None,
    flash: bool,
    notify: bool,
    alert_name: str,
    short: str,
    long: str | None,
) -> dict | None:
    """Best-effort fire of the c11 visual hooks.  Returns the location dict
    that should be embedded in the event payload, or ``None``.
    """
    if not use_c11:
        return None

    from lattice.cli import cmux_bridge

    workspace = cmux_bridge.get_workspace()
    surface = surface_override or cmux_bridge.get_surface()
    if not workspace or not surface:
        return None

    try:
        cmux_bridge.raise_alert_visual(
            workspace=workspace,
            surface=surface,
            alert_name=alert_name,
            short=short,
            long=long,
            should_flash=flash,
            should_notify=notify,
            visual_overrides=workflow_visuals_override,
        )
    except Exception:  # noqa: BLE001 — visual side-effect must never block
        pass
    return {"type": "c11", "workspace": workspace, "surface": surface}


def _maybe_clear_visual(
    *,
    use_c11: bool,
    surface_override: str | None,
    alert_name: str,
) -> None:
    if not use_c11:
        return

    from lattice.cli import cmux_bridge

    workspace = cmux_bridge.get_workspace()
    surface = surface_override or cmux_bridge.get_surface()
    if not workspace or not surface:
        return
    try:
        cmux_bridge.clear_alert_visual(
            workspace=workspace,
            surface=surface,
            alert_name=alert_name,
        )
    except Exception:  # noqa: BLE001
        pass


# ---------------------------------------------------------------------------
# lattice raise
# ---------------------------------------------------------------------------


@cli.command("raise")
@click.argument("task_id")
@click.argument("alert_name")
@click.option("--short", "short_text", required=True, help=f"Short summary (≤{ALERT_SHORT_MAX} chars).")
@click.option("--long", "long_text", default=None, help=f"Long detail (≤{ALERT_LONG_MAX} chars).")
@click.option(
    "--long-from-file",
    "long_from_file",
    default=None,
    type=click.Path(exists=True),
    help="Read the long detail from a file (mutually exclusive with --long).",
)
@click.option("--prompt", "prompt_text", default=None, help=f"Suggested clear command (≤{ALERT_PROMPT_MAX} chars).")
@click.option("--c11-surface", "c11_surface", default=None, help="Override CMUX_SURFACE_ID detection.")
@click.option("--no-c11", is_flag=True, help="Skip c11 sidebar/flash/notify side-effects.")
@click.option("--no-flash", is_flag=True, help="Skip trigger-flash even when defaults say flash.")
@click.option("--notify/--no-notify", "notify_override", default=None, help="Override notify-on-raise.")
@click.option("--evidence-ref", "evidence_ref", default=None, help="Link an artifact id (e.g. plan-review).")
@common_options
def raise_alert(
    task_id: str,
    alert_name: str,
    short_text: str,
    long_text: str | None,
    long_from_file: str | None,
    prompt_text: str | None,
    c11_surface: str | None,
    no_c11: bool,
    no_flash: bool,
    notify_override: bool | None,
    evidence_ref: str | None,
    model: str | None,
    session: str | None,
    output_json: bool,
    quiet: bool,
    triggered_by: str | None,
    on_behalf_of: str | None,
    provenance_reason: str | None,
) -> None:
    """Raise an alert on a task (e.g. ``needs_human``, ``blocked``).

    Alerts are orthogonal to status: the task stays in its current
    workflow column.  Use ``lattice clear`` to remove the alert.
    """
    is_json = output_json

    lattice_dir = require_root(is_json)
    config = load_project_config(lattice_dir)
    actor = require_actor(is_json)
    if on_behalf_of is not None:
        validate_actor_format_or_exit(on_behalf_of, is_json)

    task_id = resolve_task_id(lattice_dir, task_id, is_json)

    # Validate alert name against config.
    if not validate_alert_name(config, alert_name):
        valid = ", ".join(get_workflow_alerts(config)) or "(none configured)"
        output_error(
            f"Unknown alert: '{alert_name}'. Valid alerts: {valid}.",
            "VALIDATION_ERROR",
            is_json,
        )

    # Reject archived tasks.
    if _is_archived(lattice_dir, task_id):
        output_error(
            f"Cannot raise alert on archived task {task_id}.",
            "task_archived",
            is_json,
        )

    snapshot = read_snapshot(lattice_dir, task_id)
    if snapshot is None:
        output_error(f"Task {task_id} not found.", "NOT_FOUND", is_json)

    # Reject terminal-status tasks.
    if snapshot.get("status") in _TERMINAL_STATUSES:
        output_error(
            f"Cannot raise alert on task in terminal status '{snapshot.get('status')}'.",
            "task_terminal",
            is_json,
        )

    long_resolved = _resolve_long_text(long_text, long_from_file, is_json)

    # Build event data.
    event_data: dict = {
        "name": alert_name,
        "short": short_text,
    }
    if long_resolved is not None:
        event_data["long"] = long_resolved
    if prompt_text is not None:
        event_data["prompt"] = prompt_text
    if evidence_ref is not None:
        event_data["evidence_ref"] = evidence_ref

    # Validate up-front; CLI rejects oversize.
    ok, errors = validate_alert_payload(event_data)
    if not ok:
        output_error("; ".join(errors), "VALIDATION_ERROR", is_json)

    # Resolve visual config to choose flash/notify defaults.
    visual = get_alert_visual(config, alert_name)
    flash_default = bool(visual.get("flash", False))
    notify_default = bool(visual.get("notify", False))
    flash = flash_default and not no_flash
    notify = notify_default if notify_override is None else bool(notify_override)

    use_c11 = not no_c11

    location = _maybe_raise_visual(
        use_c11=use_c11,
        surface_override=c11_surface,
        workflow_visuals_override=visual,
        flash=flash,
        notify=notify,
        alert_name=alert_name,
        short=short_text,
        long=long_resolved,
    )
    if location is not None:
        event_data["location"] = location

    event = create_event(
        type="alert_raised",
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

    display_id = updated_snapshot.get("short_id") or task_id
    output_result(
        data={
            "task_id": task_id,
            "short_id": updated_snapshot.get("short_id"),
            "alert": alert_name,
            "raised_at": event["ts"],
            "alerts": updated_snapshot.get("alerts") or {},
        },
        human_message=f"Raised {alert_name} on {display_id}: {short_text}",
        quiet_value="ok",
        is_json=is_json,
        is_quiet=quiet,
    )


# ---------------------------------------------------------------------------
# lattice clear
# ---------------------------------------------------------------------------


@cli.command("clear")
@click.argument("task_id")
@click.argument("alert_name")
@click.option("--answer", default=None, help="Recorded answer/justification (informational).")
@click.option("--note", default=None, help="Alternate framing of the clear (informational).")
@click.option("--c11-surface", "c11_surface", default=None, help="Override CMUX_SURFACE_ID detection.")
@click.option("--no-c11", is_flag=True, help="Skip c11 sidebar clear.")
@common_options
def clear_alert(
    task_id: str,
    alert_name: str,
    answer: str | None,
    note: str | None,
    c11_surface: str | None,
    no_c11: bool,
    model: str | None,
    session: str | None,
    output_json: bool,
    quiet: bool,
    triggered_by: str | None,
    on_behalf_of: str | None,
    provenance_reason: str | None,
) -> None:
    """Clear an alert from a task.  Idempotent: clearing a non-raised alert is a no-op success."""
    is_json = output_json

    lattice_dir = require_root(is_json)
    config = load_project_config(lattice_dir)
    actor = require_actor(is_json)
    if on_behalf_of is not None:
        validate_actor_format_or_exit(on_behalf_of, is_json)

    task_id = resolve_task_id(lattice_dir, task_id, is_json)

    # Validate alert name (so typos surface here, not silently no-op).
    if not validate_alert_name(config, alert_name):
        valid = ", ".join(get_workflow_alerts(config)) or "(none configured)"
        output_error(
            f"Unknown alert: '{alert_name}'. Valid alerts: {valid}.",
            "VALIDATION_ERROR",
            is_json,
        )

    if _is_archived(lattice_dir, task_id):
        output_error(
            f"Cannot clear alert on archived task {task_id}.",
            "task_archived",
            is_json,
        )

    snapshot = read_snapshot(lattice_dir, task_id)
    if snapshot is None:
        output_error(f"Task {task_id} not found.", "NOT_FOUND", is_json)

    alerts = snapshot.get("alerts") or {}

    # Idempotent re-clear: no event, no snapshot rewrite, ok=true.
    if alert_name not in alerts:
        # Best-effort clear of any stale c11 sidebar entry.
        _maybe_clear_visual(
            use_c11=not no_c11,
            surface_override=c11_surface,
            alert_name=alert_name,
        )
        if is_json:
            click.echo(
                json_envelope(
                    True,
                    data={
                        "task_id": task_id,
                        "alert": alert_name,
                        "cleared": False,
                        "alerts": alerts,
                    },
                )
            )
        elif quiet:
            click.echo("ok")
        else:
            click.echo(f"Alert {alert_name} not raised on {task_id} (no-op).")
        return

    event_data: dict = {"name": alert_name}
    if answer is not None:
        event_data["answer"] = answer
    if note is not None:
        event_data["note"] = note

    event = create_event(
        type="alert_cleared",
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

    _maybe_clear_visual(
        use_c11=not no_c11,
        surface_override=c11_surface,
        alert_name=alert_name,
    )

    display_id = updated_snapshot.get("short_id") or task_id
    output_result(
        data={
            "task_id": task_id,
            "short_id": updated_snapshot.get("short_id"),
            "alert": alert_name,
            "cleared": True,
            "alerts": updated_snapshot.get("alerts") or {},
        },
        human_message=f"Cleared {alert_name} on {display_id}.",
        quiet_value="ok",
        is_json=is_json,
        is_quiet=quiet,
    )
