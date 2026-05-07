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

import logging
import os
import subprocess

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Status → SF Symbol icon / color mapping
# ---------------------------------------------------------------------------

STATUS_VISUALS: dict[str, dict[str, str]] = {
    "backlog": {"icon": "tray.full.fill", "color": "#888888"},
    "in_planning": {"icon": "map.fill", "color": "#9B59B6"},
    "planned": {"icon": "checkmark.seal.fill", "color": "#3498DB"},
    "in_progress": {"icon": "play.fill", "color": "#E67E22"},
    "review": {"icon": "eye.fill", "color": "#FFD700"},
    "done": {"icon": "checkmark.circle.fill", "color": "#2ECC71"},
    "blocked": {"icon": "exclamationmark.triangle.fill", "color": "#E74C3C"},
    "needs_human": {"icon": "person.fill.questionmark", "color": "#E74C3C"},
    "cancelled": {"icon": "xmark.circle.fill", "color": "#95A5A6"},
}

# Display labels used in tab titles for each status
STATUS_LABELS: dict[str, str] = {
    "in_progress": "on it",
    "review": "review",
    "blocked": "blocked",
    "needs_human": "needs human",
    "done": "done",
    "cancelled": "cancelled",
}


# ---------------------------------------------------------------------------
# Detection
# ---------------------------------------------------------------------------


def cmux_available() -> bool:
    """Return True if we are running inside cmux."""
    return bool(os.environ.get("CMUX_WORKSPACE_ID"))


def get_workspace() -> str | None:
    """Return the current cmux workspace ref from the environment."""
    return os.environ.get("CMUX_WORKSPACE_ID")


def get_surface() -> str | None:
    """Return the current cmux surface ref from the environment."""
    return os.environ.get("CMUX_SURFACE_ID")


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
