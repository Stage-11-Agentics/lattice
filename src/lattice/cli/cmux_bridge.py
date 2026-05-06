"""cmux integration bridge for Lattice.

All cmux interaction lives here.  Every function is a no-op when not running
inside cmux (detected via CMUX_WORKSPACE_ID env var).  All subprocess errors
are logged as warnings — they never raise and never block Lattice operations.

Design principles:
- Strictly optional: Lattice works identically without cmux.
- Detection, not configuration: presence of CMUX_WORKSPACE_ID IS the config.
- Graceful degradation: failures are warnings, not errors.
- CLI layer only: core knows nothing about cmux.
"""

from __future__ import annotations

import json
import logging
import os
import subprocess

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Status → SF Symbol icon / color mapping
# ---------------------------------------------------------------------------

STATUS_VISUALS: dict[str, dict[str, str]] = {
    "backlog":     {"icon": "tray.full.fill",                    "color": "#888888"},
    "in_planning": {"icon": "map.fill",                          "color": "#9B59B6"},
    "planned":     {"icon": "checkmark.seal.fill",               "color": "#3498DB"},
    "in_progress": {"icon": "play.fill",                         "color": "#E67E22"},
    "review":      {"icon": "eye.fill",                          "color": "#FFD700"},
    "done":        {"icon": "checkmark.circle.fill",             "color": "#2ECC71"},
    "cancelled":   {"icon": "xmark.circle.fill",                 "color": "#95A5A6"},
}

# Display labels used in tab titles for each status
STATUS_LABELS: dict[str, str] = {
    "in_progress": "on it",
    "review": "review",
    "done": "done",
    "cancelled": "cancelled",
}

# Alert visuals (LAT-210) — orthogonal "needs attention" markers.
# Per-key overrides come from workflow.alert_visuals; these are the
# canonical defaults.
ALERT_VISUALS: dict[str, dict] = {
    "needs_human": {
        "color": "#FFD600",
        "icon": "exclamationmark.triangle.fill",
        "flash": True,
        "notify": True,
        "label": "NEEDS HUMAN",
    },
    "blocked": {
        "color": "#FFA500",
        "icon": "nosign",
        "flash": False,
        "notify": False,
        "label": "BLOCKED",
    },
}


# ---------------------------------------------------------------------------
# Detection
# ---------------------------------------------------------------------------


def cmux_available() -> bool:
    """Return True if we are running inside cmux/c11."""
    return bool(
        os.environ.get("CMUX_WORKSPACE_ID")
        or os.environ.get("C11_WORKSPACE_ID")
    )


def get_workspace() -> str | None:
    """Return the current cmux/c11 workspace ref from the environment."""
    return os.environ.get("CMUX_WORKSPACE_ID") or os.environ.get("C11_WORKSPACE_ID")


def get_surface() -> str | None:
    """Return the current cmux/c11 surface ref from the environment."""
    return os.environ.get("CMUX_SURFACE_ID") or os.environ.get("C11_SURFACE_ID")


# ---------------------------------------------------------------------------
# Low-level cmux CLI wrappers
# ---------------------------------------------------------------------------


def _run_cmux(*args: str) -> bool:
    """Run a cmux CLI command.  Returns True on success, False on failure."""
    try:
        result = subprocess.run(
            ["cmux", *args],
            capture_output=True,
            timeout=10,
        )
        if result.returncode != 0:
            logger.warning(
                "cmux command failed (exit %d): cmux %s\nstderr: %s",
                result.returncode,
                " ".join(args),
                result.stderr.decode(errors="replace"),
            )
            return False
        return True
    except FileNotFoundError:
        logger.warning("cmux binary not found — cmux integration unavailable")
        return False
    except subprocess.TimeoutExpired:
        logger.warning("cmux command timed out: cmux %s", " ".join(args))
        return False
    except Exception as exc:  # noqa: BLE001
        logger.warning("cmux command error: %s", exc)
        return False


def rename_tab(surface: str, title: str) -> bool:
    """Rename a cmux surface tab.  Returns True on success."""
    if not cmux_available():
        return False
    workspace = get_workspace()
    args = ["rename-tab", "--surface", surface, title]
    if workspace:
        args = ["rename-tab", "--workspace", workspace, "--surface", surface, title]
    return _run_cmux(*args)


def set_status(key: str, value: str, icon: str | None = None, color: str | None = None) -> bool:
    """Update a cmux sidebar status entry.  Returns True on success."""
    if not cmux_available():
        return False
    args = ["set-status", key, value]
    if icon:
        args += ["--icon", icon]
    if color:
        args += ["--color", color]
    return _run_cmux(*args)


def clear_status(key: str) -> bool:
    """Remove a cmux sidebar status entry.  Returns True on success."""
    if not cmux_available():
        return False
    return _run_cmux("clear-status", key)


def trigger_flash(surface: str) -> bool:
    """Flash a cmux surface.  Returns True on success."""
    if not cmux_available():
        return False
    workspace = get_workspace()
    args = ["trigger-flash", "--surface", surface]
    if workspace:
        args = ["trigger-flash", "--workspace", workspace, "--surface", surface]
    return _run_cmux(*args)


def notify(title: str, body: str | None = None) -> bool:
    """Send a cmux notification.  Returns True on success."""
    if not cmux_available():
        return False
    args = ["notify", "--title", title]
    if body:
        args += ["--body", body]
    return _run_cmux(*args)


# ---------------------------------------------------------------------------
# Alert visuals (LAT-210)
# ---------------------------------------------------------------------------


def raise_alert_visual(
    *,
    workspace: str,
    surface: str,
    alert_name: str,
    short: str,
    long: str | None,
    should_flash: bool,
    should_notify: bool,
    visual_overrides: dict | None = None,
) -> None:
    """Wire a c11 sidebar pill + optional flash + optional notify for an alert.

    Order: metadata first (durable), then sidebar pill, then flash
    (one-shot), then OS-level notification.  Subprocess failures inside
    ``_run_cmux`` are logged and swallowed — this never raises.

    The ``should_flash`` / ``should_notify`` parameters are named to avoid
    shadowing the module-level ``notify`` helper and ``trigger_flash``.
    """
    if not cmux_available():
        return

    visuals = dict(ALERT_VISUALS.get(alert_name, {}))
    if visual_overrides:
        for k, v in visual_overrides.items():
            visuals[k] = v

    color = visuals.get("color", "#FFD600")
    icon = visuals.get("icon", "exclamationmark.triangle.fill")

    # 1. Metadata (durable; survives sidebar refresh)
    _run_cmux(
        "set-metadata",
        "--workspace",
        workspace,
        "--surface",
        surface,
        "--json",
        json.dumps({"lattice": {alert_name: {"short": short}}}),
    )

    # 2. Sidebar pill
    _run_cmux(
        "set-status",
        alert_name,
        short[:32],
        "--workspace",
        workspace,
        "--surface",
        surface,
        "--color",
        color,
        "--icon",
        icon,
    )

    # 3. Flash
    if should_flash:
        _run_cmux("trigger-flash", "--workspace", workspace, "--surface", surface)

    # 4. Notification
    if should_notify:
        body = (long or short)[:200]
        _run_cmux(
            "notify",
            "--title",
            f"Lattice: {alert_name}",
            "--subtitle",
            short[:80],
            "--body",
            body,
        )


def clear_alert_visual(
    *,
    workspace: str,
    surface: str,
    alert_name: str,
) -> None:
    """Clear the c11 sidebar pill and metadata key for an alert.  No un-flash."""
    if not cmux_available():
        return
    _run_cmux(
        "clear-status",
        alert_name,
        "--workspace",
        workspace,
        "--surface",
        surface,
    )
    _run_cmux(
        "clear-metadata",
        "--key",
        f"lattice.{alert_name}",
        "--workspace",
        workspace,
        "--surface",
        surface,
    )


# ---------------------------------------------------------------------------
# Higher-level hooks called from task_cmds.py
# ---------------------------------------------------------------------------


def on_status_changed(snapshot: dict, old_status: str, new_status: str) -> None:
    """React to a task status transition inside cmux.

    Reads ``cmux_surface`` and ``cmux_workspace`` from the snapshot.
    Does nothing if the task has no surface binding.

    Called from task_cmds.py after write_task_event succeeds.  Must
    never raise — all errors are logged as warnings.
    """
    if not cmux_available():
        return

    surface = snapshot.get("cmux_surface")
    if not surface:
        return  # task not bound to any surface

    short_id = snapshot.get("short_id") or snapshot.get("id", "")
    title = snapshot.get("title") or ""
    status_label = STATUS_LABELS.get(new_status, new_status)
    visuals = STATUS_VISUALS.get(new_status, {})

    # Update tab title
    if new_status in ("done", "cancelled"):
        tab_title = f"{short_id}: {title}"
    else:
        tab_title = f"{short_id} [{status_label}]: {title}"
    rename_tab(surface, tab_title)

    # Update sidebar
    if new_status == "done":
        # Flash the surface, send notification, then clear the sidebar entry
        trigger_flash(surface)
        notify(
            title=f"{short_id} done",
            body=title if title else None,
        )
        clear_status(short_id)
    else:
        set_status(
            short_id,
            status_label,
            icon=visuals.get("icon"),
            color=visuals.get("color"),
        )
