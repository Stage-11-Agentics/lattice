"""Shared CLI helpers, decorators, and output utilities."""

from __future__ import annotations

import json
from pathlib import Path
from typing import NoReturn

import click

from lattice.core.ids import validate_actor
from lattice.storage.fs import LATTICE_DIR, LatticeRootError, find_root
from lattice.storage.operations import write_task_event  # noqa: F401 — re-exported


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
    f = click.option("--actor", required=True, help="Actor (e.g., human:atin, agent:claude).")(f)
    return f


# ---------------------------------------------------------------------------
# Read helpers
# ---------------------------------------------------------------------------


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
