"""Shell hook execution after events are written."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

HOOK_TIMEOUT_SECONDS = 10


def execute_hooks(
    config: dict,
    lattice_dir: Path,
    task_id: str,
    event: dict,
) -> None:
    """Fire configured hooks for a single event.

    Hooks are fire-and-forget: failures are logged to stderr but never
    raise exceptions or fail the calling CLI command.

    Execution order when both are configured:
    1. ``hooks.post_event`` (catch-all)
    2. ``hooks.on.<event_type>`` (type-specific)
    """
    hooks = config.get("hooks")
    if not hooks:
        return

    env = _build_env(lattice_dir, task_id, event)
    stdin_data = json.dumps(event, sort_keys=True, separators=(",", ":"))

    # 1. post_event (catch-all)
    post_event_cmd = hooks.get("post_event")
    if post_event_cmd:
        _run_hook(post_event_cmd, env, stdin_data)

    # 2. on.<event_type>
    on_hooks = hooks.get("on") or {}
    type_cmd = on_hooks.get(event["type"])
    if type_cmd:
        _run_hook(type_cmd, env, stdin_data)


def _build_env(lattice_dir: Path, task_id: str, event: dict) -> dict[str, str]:
    """Build the environment dict for hook subprocesses."""
    env = os.environ.copy()
    env["LATTICE_ROOT"] = str(lattice_dir)
    env["LATTICE_TASK_ID"] = task_id
    env["LATTICE_EVENT_TYPE"] = event["type"]
    env["LATTICE_EVENT_ID"] = event["id"]
    env["LATTICE_ACTOR"] = event["actor"]
    return env


def _run_hook(cmd: str, env: dict[str, str], stdin_data: str) -> None:
    """Execute a single hook command. Never raises."""
    try:
        subprocess.run(
            cmd,
            shell=True,
            input=stdin_data,
            env=env,
            timeout=HOOK_TIMEOUT_SECONDS,
            capture_output=True,
            text=True,
        )
    except subprocess.TimeoutExpired:
        print(f"lattice: hook timed out after {HOOK_TIMEOUT_SECONDS}s: {cmd}", file=sys.stderr)
    except Exception as exc:
        print(f"lattice: hook error: {exc}", file=sys.stderr)
