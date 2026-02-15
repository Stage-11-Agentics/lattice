"""Shared CLI helpers, decorators, and output utilities."""

from __future__ import annotations

import json
from pathlib import Path
from typing import NoReturn

import click

from lattice.core.events import GLOBAL_LOG_TYPES, serialize_event
from lattice.core.ids import validate_actor
from lattice.core.tasks import serialize_snapshot
from lattice.storage.fs import LATTICE_DIR, LatticeRootError, atomic_write, find_root, jsonl_append
from lattice.storage.locks import multi_lock


# ---------------------------------------------------------------------------
# Root & config
# ---------------------------------------------------------------------------


def require_root(is_json: bool = False) -> Path:
    """Find .lattice/ directory or exit with error."""
    try:
        root = find_root()
    except LatticeRootError as e:
        output_error(str(e), "NOT_INITIALIZED", is_json)
    if root is None:
        output_error(
            "Not a Lattice project (no .lattice/ found). Run 'lattice init' first.",
            "NOT_INITIALIZED",
            is_json,
        )
    return root / LATTICE_DIR


def load_project_config(lattice_dir: Path) -> dict:
    """Load and return config.json from the lattice directory."""
    return json.loads((lattice_dir / "config.json").read_text())


# ---------------------------------------------------------------------------
# Output helpers
# ---------------------------------------------------------------------------


def json_envelope(ok: bool, *, data: object = None, error: object = None) -> str:
    """Build a structured JSON output envelope."""
    result: dict = {"ok": ok}
    if data is not None:
        result["data"] = data
    if error is not None:
        result["error"] = error
    return json.dumps(result, sort_keys=True, indent=2) + "\n"


def json_error_obj(code: str, message: str) -> dict:
    """Build an error object for the JSON envelope."""
    return {"code": code, "message": message}


def output_error(message: str, code: str, is_json: bool, exit_code: int = 1) -> NoReturn:
    """Print error and exit. JSON errors go to stdout; human errors to stderr."""
    if is_json:
        click.echo(json_envelope(False, error=json_error_obj(code, message)))
    else:
        click.echo(f"Error: {message}", err=True)
    raise SystemExit(exit_code)


def output_result(
    *,
    data: object,
    human_message: str,
    quiet_value: str,
    is_json: bool,
    is_quiet: bool,
) -> None:
    """Print success result in the appropriate format."""
    if is_json:
        click.echo(json_envelope(True, data=data))
    elif is_quiet:
        click.echo(quiet_value)
    else:
        click.echo(human_message)


# ---------------------------------------------------------------------------
# Validation helpers
# ---------------------------------------------------------------------------


def resolve_actor(actor: str | None, lattice_dir: Path, is_json: bool) -> str:
    """Resolve actor from flag, LATTICE_ACTOR env var (via Click), or config default_actor."""
    if actor:
        return actor
    config = load_project_config(lattice_dir)
    default = config.get("default_actor")
    if default:
        return default
    output_error(
        "No --actor provided. Set LATTICE_ACTOR env var or default_actor in config.json.",
        "MISSING_ACTOR",
        is_json,
    )


def validate_actor_or_exit(actor: str, is_json: bool) -> None:
    """Validate actor format or exit with error."""
    if not validate_actor(actor):
        output_error(
            f"Invalid actor format: '{actor}'. "
            "Expected prefix:identifier (e.g., human:atin, agent:claude).",
            "INVALID_ACTOR",
            is_json,
        )


# ---------------------------------------------------------------------------
# Click decorator
# ---------------------------------------------------------------------------


def common_options(f):  # noqa: ANN001, ANN201
    """Decorator adding common write-command options."""
    f = click.option("--quiet", is_flag=True, help="Print only the primary ID.")(f)
    f = click.option("--json", "output_json", is_flag=True, help="Output structured JSON.")(f)
    f = click.option("--session", default=None, help="Session identifier.")(f)
    f = click.option("--model", default=None, help="Model identifier.")(f)
    f = click.option(
        "--actor",
        default=None,
        envvar="LATTICE_ACTOR",
        help="Actor (e.g., human:atin, agent:claude). "
        "Defaults to LATTICE_ACTOR env var or config default_actor.",
    )(f)
    return f


# ---------------------------------------------------------------------------
# Write path
# ---------------------------------------------------------------------------


def write_task_event(
    lattice_dir: Path,
    task_id: str,
    events: list[dict],
    snapshot: dict,
) -> None:
    """Write event(s) and snapshot atomically with proper locking.

    Follows the write path pattern:
    1. Acquire locks in sorted order
    2. Append events to per-task JSONL
    3. Append lifecycle events to global JSONL
    4. Atomic-write snapshot
    5. Release locks
    """
    locks_dir = lattice_dir / "locks"

    # Determine which events go to global log
    global_events = [e for e in events if e["type"] in GLOBAL_LOG_TYPES]

    # Build lock keys
    lock_keys = [f"events_{task_id}", f"tasks_{task_id}"]
    if global_events:
        lock_keys.append("events__global")
    lock_keys.sort()

    with multi_lock(locks_dir, lock_keys):
        # Event-first: append to per-task log
        event_path = lattice_dir / "events" / f"{task_id}.jsonl"
        for event in events:
            jsonl_append(event_path, serialize_event(event))

        # Lifecycle events go to global log
        if global_events:
            global_path = lattice_dir / "events" / "_global.jsonl"
            for event in global_events:
                jsonl_append(global_path, serialize_event(event))

        # Then materialize snapshot
        snapshot_path = lattice_dir / "tasks" / f"{task_id}.json"
        atomic_write(snapshot_path, serialize_snapshot(snapshot))


def read_snapshot(lattice_dir: Path, task_id: str) -> dict | None:
    """Read a task snapshot, returning None if not found."""
    path = lattice_dir / "tasks" / f"{task_id}.json"
    if not path.exists():
        return None
    return json.loads(path.read_text())


def read_snapshot_or_exit(lattice_dir: Path, task_id: str, is_json: bool) -> dict:
    """Read a task snapshot or exit with NOT_FOUND error."""
    snapshot = read_snapshot(lattice_dir, task_id)
    if snapshot is None:
        output_error(f"Task {task_id} not found.", "NOT_FOUND", is_json)
    return snapshot
