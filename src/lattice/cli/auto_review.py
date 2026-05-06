"""Side-effect layer for auto-firing reviews on status transitions (LAT-211).

This module owns everything the pure :mod:`lattice.core.auto_review` does
*not*: locating the ``lattice`` executable, claiming the in-flight review
slot via :func:`lattice.core.review.claim_review_state`, opening the log
file, and spawning the detached subprocess. It is callable from the Click
``status_cmd`` and returns a result dict describing what happened — never
raises so the cardinal property of ``status_cmd`` is preserved (auto-fire
must never block a status transition).

See LAT-211 plan §4 (wiring) and §5 (coordination) for the design.
"""

from __future__ import annotations

import logging
import os
import shutil
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

from lattice.core.auto_review import (
    AUTO_REVIEW_ACTOR,
    DAEMON_DIR_NAME,
    resolve_mode,
    review_type_for_status,
    should_auto_fire,
)
from lattice.core.review import claim_review_state, clear_review_state

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Executable discovery
# ---------------------------------------------------------------------------


def find_lattice_executable() -> str | None:
    """Return the absolute path to the ``lattice`` CLI binary, or None.

    Prefers ``$PATH`` (``shutil.which``).  Falls back to the directory
    containing the current Python interpreter (covers the common
    ``uv tool install`` layout where ``lattice`` lives next to ``python``).
    Mirrors the discovery code at :func:`lattice.cli.main.open_in_browser`
    so behavior stays consistent with the existing dashboard launcher.
    """
    found = shutil.which("lattice")
    if found:
        return found
    candidate = Path(sys.executable).parent / "lattice"
    if candidate.exists():
        return str(candidate)
    return None


# ---------------------------------------------------------------------------
# Log path
# ---------------------------------------------------------------------------


def log_path_for(lattice_dir: Path, review_type: str, task_id: str) -> Path:
    """Return the per-task log path for an auto-fired review.

    Path shape: ``.lattice/.daemon/auto-{review_type}-{task_id}.log``.
    Overwritten per spawn — debug aid only, not an audit trail.
    """
    return lattice_dir / DAEMON_DIR_NAME / f"auto-{review_type}-{task_id}.log"


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------


def auto_fire_review(
    lattice_dir: Path,
    task_id: str,
    new_status: str,
    *,
    status_event_id: str,
    config: dict,
    no_auto_review_flag: bool,
) -> dict:
    """Spawn a detached ``lattice {code,plan}-review`` if gating allows.

    The function is structured to swallow all errors internally and return
    a result dict.  Callers (``status_cmd``) wrap it in a defensive
    ``try/except`` regardless, but in practice this returns rather than
    raising on every code path.

    Result dict shapes:

    * On success::

        {
            "fired": True,
            "review_type": "code-review" | "plan-review",
            "mode": "single" | "triple",
            "log_path": "<absolute path>",
            "pid": <child PID>,
            "spawned_at": "<RFC3339 UTC>",
        }

    * On skip / failure::

        {
            "fired": False,
            "reason": "<stable code>",
            # plus optional details: holder_pid, holder_auto_fired, etc.
        }
    """
    fire, skip_reason = should_auto_fire(
        config,
        new_status,
        no_auto_review_flag=no_auto_review_flag,
    )
    if not fire:
        assert skip_reason is not None
        return {"fired": False, "reason": skip_reason}

    review_type = review_type_for_status(new_status)
    if review_type is None:
        # ``should_auto_fire`` already excludes non-gate statuses; this
        # branch is defensive — keeps the type-checker happy and guards
        # against future drift.
        return {"fired": False, "reason": "not_a_review_gate"}

    mode = resolve_mode(config, new_status)

    # Synchronous parent-side claim — see LAT-211 §5.
    claimed, existing = claim_review_state(
        lattice_dir,
        task_id,
        mode=mode,
        review_type=review_type,
        started_by_pid=os.getpid(),
        auto_fired=True,
    )
    if not claimed:
        result: dict = {
            "fired": False,
            "reason": "review_in_flight",
        }
        if isinstance(existing, dict):
            holder = existing.get("started_by_pid")
            if isinstance(holder, int):
                result["holder_pid"] = holder
            holder_auto = existing.get("auto_fired")
            if isinstance(holder_auto, bool):
                result["holder_auto_fired"] = holder_auto
        return result

    lattice_bin = find_lattice_executable()
    if lattice_bin is None:
        # Release the claim so a manual retry isn't blocked by our
        # phantom record.  Best effort — failures here are silently
        # ignored because we're already on the failure path.
        try:
            clear_review_state(lattice_dir, task_id)
        except Exception:  # noqa: BLE001 — best effort cleanup
            logger.debug("clear_review_state failed during executable lookup miss", exc_info=True)
        return {"fired": False, "reason": "executable_not_found"}

    log_path = log_path_for(lattice_dir, review_type, task_id)
    try:
        log_path.parent.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        try:
            clear_review_state(lattice_dir, task_id)
        except Exception:  # noqa: BLE001
            logger.debug("clear_review_state failed during mkdir failure", exc_info=True)
        return {"fired": False, "reason": f"spawn_failed:{exc}"}

    spawned_at = _now_iso()
    cmd = [
        lattice_bin,
        review_type,
        task_id,
        "--actor",
        AUTO_REVIEW_ACTOR,
        "--triggered-by",
        status_event_id,
    ]

    log_fh = None
    try:
        log_fh = open(log_path, "w", encoding="utf-8")
        log_fh.write(f"# auto-{review_type} for {task_id} started {spawned_at}\n")
        log_fh.flush()

        proc = subprocess.Popen(
            cmd,
            cwd=str(lattice_dir.parent),
            stdout=log_fh,
            stderr=subprocess.STDOUT,
            start_new_session=True,
            close_fds=True,
        )
    except Exception as exc:  # noqa: BLE001 — return the failure cleanly
        try:
            clear_review_state(lattice_dir, task_id)
        except Exception:  # noqa: BLE001
            logger.debug("clear_review_state failed during Popen failure", exc_info=True)
        # An empty header file may have been written before Popen raised;
        # remove it so future debug spelunkers don't read it as "a review
        # spawned and produced nothing."
        try:
            log_path.unlink(missing_ok=True)
        except OSError:
            logger.debug("log_path.unlink failed during Popen failure", exc_info=True)
        return {"fired": False, "reason": f"spawn_failed:{exc}"}
    finally:
        # The child holds its own dup of the log fd.  Close the parent's
        # copy immediately to avoid a leaked descriptor — important if
        # this helper ever runs from a longer-lived caller (the daemon
        # path or plugins).
        if log_fh is not None:
            try:
                log_fh.close()
            except OSError:
                logger.debug("log fd close raised", exc_info=True)

    return {
        "fired": True,
        "review_type": review_type,
        "mode": mode,
        "log_path": str(log_path),
        "pid": proc.pid,
        "spawned_at": spawned_at,
    }


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
