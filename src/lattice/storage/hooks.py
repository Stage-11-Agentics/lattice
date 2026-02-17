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

    Execution order when multiple are configured:
    1. ``hooks.post_event`` (catch-all)
    2. ``hooks.on.<event_type>`` (type-specific)
    3. ``hooks.transitions`` (transition-specific, status_changed only)
       - Exact match: ``"from -> to"``
       - Wildcard source: ``"* -> to"``
       - Wildcard target: ``"from -> *"``
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

    # 3. transitions (status_changed only)
    transitions = hooks.get("transitions")
    if transitions and isinstance(transitions, dict) and event["type"] == "status_changed":
        data = event.get("data", {})
        from_status = data.get("from", "")
        to_status = data.get("to", "")

        if from_status and to_status:
            # Add transition-specific env vars
            transition_env = env.copy()
            transition_env["LATTICE_FROM_STATUS"] = from_status
            transition_env["LATTICE_TO_STATUS"] = to_status

            for cmd in _match_transitions(transitions, from_status, to_status):
                _run_hook(cmd, transition_env, stdin_data)


def _match_transitions(
    transitions: dict[str, str],
    from_status: str,
    to_status: str,
) -> list[str]:
    """Return commands matching the given transition, in priority order.

    Match order:
    1. Exact (``"from -> to"``)
    2. Wildcard source (``"* -> to"``)
    3. Wildcard target (``"from -> *"``)
    4. Double wildcard (``"* -> *"``)
    """
    exact: list[str] = []
    wild_src: list[str] = []
    wild_tgt: list[str] = []
    wild_both: list[str] = []

    for pattern, cmd in transitions.items():
        parsed = _parse_transition_key(pattern)
        if parsed is None:
            continue

        pat_from, pat_to = parsed
        from_matches = pat_from == "*" or pat_from == from_status
        to_matches = pat_to == "*" or pat_to == to_status

        if not from_matches or not to_matches:
            continue

        if pat_from != "*" and pat_to != "*":
            exact.append(cmd)
        elif pat_from == "*" and pat_to != "*":
            wild_src.append(cmd)
        elif pat_from != "*" and pat_to == "*":
            wild_tgt.append(cmd)
        else:
            wild_both.append(cmd)

    return exact + wild_src + wild_tgt + wild_both


def _parse_transition_key(key: str) -> tuple[str, str] | None:
    """Parse a transition key like ``"from -> to"`` into ``(from, to)``.

    Returns ``None`` if the key doesn't match the expected format.
    Tolerates whitespace around the arrow.
    """
    parts = key.split("->")
    if len(parts) != 2:
        return None
    left = parts[0].strip()
    right = parts[1].strip()
    if not left or not right:
        return None
    return (left, right)


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
