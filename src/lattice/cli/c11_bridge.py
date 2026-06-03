"""c11 integration bridge for Lattice.

All c11 interaction lives here.  Every function is a no-op when not running
inside c11 (detected via the C11_WORKSPACE_ID env var).  All subprocess
errors are logged as warnings — they never raise and never block Lattice
operations.

Design principles:
- Strictly optional: Lattice works identically without c11.
- Detection, not configuration: presence of C11_WORKSPACE_ID IS the config.
- Graceful degradation: failures are warnings, not errors.
- CLI layer only: core knows nothing about c11.

The c11 binary still accepts ``cmux`` as an OS-layer compat alias, but it
always sets ``C11_*`` env vars on every surface, so reading ``C11_*`` alone
is sufficient — we never reach for the legacy ``CMUX_*`` names in our code.
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
    "in_validation": {"icon": "checkmark.shield.fill", "color": "#1ABC9C"},
    "pr_open": {"icon": "arrow.triangle.pull", "color": "#6C5CE7"},
    "done": {"icon": "checkmark.circle.fill", "color": "#2ECC71"},
    "blocked": {"icon": "exclamationmark.triangle.fill", "color": "#E74C3C"},
    # Retained for instances whose config still has the legacy status.
    "needs_human": {"icon": "person.fill.questionmark", "color": "#E74C3C"},
    "cancelled": {"icon": "xmark.circle.fill", "color": "#95A5A6"},
}

# Visuals for the orthogonal needs_human flag (overrides status visuals).
NEEDS_HUMAN_VISUALS: dict[str, str] = {
    "icon": "person.fill.questionmark",
    "color": "#F59E0B",
}

# Display labels used in tab titles for each status
STATUS_LABELS: dict[str, str] = {
    "in_progress": "on it",
    "review": "review",
    "in_validation": "validating",
    "pr_open": "PR up",
    "blocked": "blocked",
    "needs_human": "needs human",
    "done": "done",
    "cancelled": "cancelled",
}


# ---------------------------------------------------------------------------
# Detection
# ---------------------------------------------------------------------------


def c11_available() -> bool:
    """Return True if we are running inside c11."""
    return bool(os.environ.get("C11_WORKSPACE_ID"))


def get_workspace() -> str | None:
    """Return the current c11 workspace ref from the environment."""
    return os.environ.get("C11_WORKSPACE_ID")


def get_surface() -> str | None:
    """Return the current c11 surface ref from the environment."""
    return os.environ.get("C11_SURFACE_ID")


# ---------------------------------------------------------------------------
# Low-level c11 CLI wrappers
# ---------------------------------------------------------------------------


def _run_c11(*args: str) -> bool:
    """Run a c11 CLI command.  Returns True on success, False on failure."""
    try:
        result = subprocess.run(
            ["c11", *args],
            capture_output=True,
            timeout=10,
        )
        if result.returncode != 0:
            logger.warning(
                "c11 command failed (exit %d): c11 %s\nstderr: %s",
                result.returncode,
                " ".join(args),
                result.stderr.decode(errors="replace"),
            )
            return False
        return True
    except FileNotFoundError:
        logger.warning("c11 binary not found — c11 integration unavailable")
        return False
    except subprocess.TimeoutExpired:
        logger.warning("c11 command timed out: c11 %s", " ".join(args))
        return False
    except Exception as exc:  # noqa: BLE001
        logger.warning("c11 command error: %s", exc)
        return False


def rename_tab(surface: str, title: str) -> bool:
    """Rename a c11 surface tab.  Returns True on success."""
    if not c11_available():
        return False
    workspace = get_workspace()
    args = ["rename-tab", "--surface", surface, title]
    if workspace:
        args = ["rename-tab", "--workspace", workspace, "--surface", surface, title]
    return _run_c11(*args)


def set_status(key: str, value: str, icon: str | None = None, color: str | None = None) -> bool:
    """Update a c11 sidebar status entry.  Returns True on success."""
    if not c11_available():
        return False
    args = ["set-status", key, value]
    if icon:
        args += ["--icon", icon]
    if color:
        args += ["--color", color]
    return _run_c11(*args)


def clear_status(key: str) -> bool:
    """Remove a c11 sidebar status entry.  Returns True on success."""
    if not c11_available():
        return False
    return _run_c11("clear-status", key)


def trigger_flash(surface: str) -> bool:
    """Flash a c11 surface.  Returns True on success."""
    if not c11_available():
        return False
    workspace = get_workspace()
    args = ["trigger-flash", "--surface", surface]
    if workspace:
        args = ["trigger-flash", "--workspace", workspace, "--surface", surface]
    return _run_c11(*args)


def notify(title: str, body: str | None = None) -> bool:
    """Send a c11 notification.  Returns True on success."""
    if not c11_available():
        return False
    args = ["notify", "--title", title]
    if body:
        args += ["--body", body]
    return _run_c11(*args)


# ---------------------------------------------------------------------------
# Higher-level hooks called from task_cmds.py
# ---------------------------------------------------------------------------


def on_status_changed(snapshot: dict, old_status: str, new_status: str) -> None:
    """React to a task status transition inside c11.

    Reads ``c11_surface`` and ``c11_workspace`` from the snapshot.
    Does nothing if the task has no surface binding.

    Called from task_cmds.py after write_task_event succeeds.  Must
    never raise — all errors are logged as warnings.
    """
    if not c11_available():
        return

    surface = snapshot.get("c11_surface")
    if not surface:
        return  # task not bound to any surface

    short_id = snapshot.get("short_id") or snapshot.get("id", "")
    title = snapshot.get("title") or ""
    status_label = STATUS_LABELS.get(new_status, new_status)
    visuals = STATUS_VISUALS.get(new_status, {})
    # The needs_human flag outranks status visuals — a flagged task is
    # waiting on a human regardless of its swimlane.
    if snapshot.get("needs_human"):
        status_label = f"{status_label} · needs human"
        visuals = NEEDS_HUMAN_VISUALS

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


def on_needs_human_changed(snapshot: dict, flagged: bool) -> None:
    """React to the needs_human flag being set or cleared inside c11.

    Reads ``c11_surface`` from the snapshot; does nothing if the task has
    no surface binding.  Called from flag_cmds.py after write_task_event
    succeeds.  Must never raise — all errors are logged as warnings.
    """
    if not c11_available():
        return

    surface = snapshot.get("c11_surface")
    if not surface:
        return  # task not bound to any surface

    short_id = snapshot.get("short_id") or snapshot.get("id", "")
    status = snapshot.get("status", "")
    status_label = STATUS_LABELS.get(status, status)

    if flagged:
        set_status(
            short_id,
            f"{status_label} · needs human",
            icon=NEEDS_HUMAN_VISUALS["icon"],
            color=NEEDS_HUMAN_VISUALS["color"],
        )
        trigger_flash(surface)
    else:
        visuals = STATUS_VISUALS.get(status, {})
        set_status(
            short_id,
            status_label,
            icon=visuals.get("icon"),
            color=visuals.get("color"),
        )
