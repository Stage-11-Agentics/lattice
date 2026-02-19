"""Shared CLI helpers, decorators, and output utilities."""

from __future__ import annotations

import json
from pathlib import Path
from typing import NoReturn

import click

from lattice.core.ids import is_short_id, validate_actor, validate_id
from lattice.storage.fs import LATTICE_DIR, LatticeRootError, find_root
from lattice.storage.operations import write_task_event  # noqa: F401 — re-exported
from lattice.storage.short_ids import resolve_short_id as _resolve_short


# ---------------------------------------------------------------------------
# Session → actor dict helper (single source of truth)
# ---------------------------------------------------------------------------


def _build_actor_dict(session_data: dict) -> dict:
    """Build a structured actor dict from session data.

    This is the single place that maps session fields to the actor
    identity dict stored in events.  All session resolution paths
    must use this function.
    """
    d: dict = {
        "name": session_data["name"],
        "base_name": session_data["base_name"],
        "serial": session_data["serial"],
        "session": session_data["session"],
        "model": session_data["model"],
    }
    if session_data.get("framework"):
        d["framework"] = session_data["framework"]
    if session_data.get("agent_type"):
        d["agent_type"] = session_data["agent_type"]
    return d


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
# Task ID resolution (short ID -> ULID)
# ---------------------------------------------------------------------------


def resolve_task_id(
    lattice_dir: Path,
    raw_id: str,
    is_json: bool,
    *,
    allow_archived: bool = False,
) -> str:
    """Resolve a raw task identifier to a canonical ULID.

    Accepts both ULIDs (``task_01...``) and short IDs (``LAT-42``).
    Exits with an error if the ID is unrecognized.
    """
    # Direct ULID
    if validate_id(raw_id, "task"):
        return raw_id

    # Try short ID
    if is_short_id(raw_id):
        normalized = raw_id.upper()
        ulid = _resolve_short(lattice_dir, normalized)
        if ulid is not None:
            return ulid
        output_error(
            f"Short ID '{normalized}' not found.",
            "NOT_FOUND",
            is_json,
        )

    # Not a valid format
    output_error(
        f"Invalid task ID format: '{raw_id}'.",
        "INVALID_ID",
        is_json,
    )


# ---------------------------------------------------------------------------
# Actor resolution
# ---------------------------------------------------------------------------


def require_actor(is_json: bool, *, optional: bool = False) -> str | dict | None:
    """Resolve actor identity from Click context.  Caches the result.

    Reads ``--name`` and ``--actor`` from the Click context (stored by
    ``_store_session_name`` and ``_store_actor`` callbacks in
    ``common_options``).  Returns a structured dict (from session) or
    a validated legacy string.

    Set *optional* to ``True`` for commands where identity is not
    required (e.g., ``lattice next`` without ``--claim``).  Returns
    ``None`` when no identity flags were provided.
    """
    from lattice.storage.sessions import resolve_session, touch_session

    ctx = click.get_current_context()
    ctx.ensure_object(dict)

    # Return cached result
    if "_resolved_actor" in ctx.obj:
        return ctx.obj["_resolved_actor"]

    session_name = ctx.obj.get("_session_name")
    actor_str = ctx.obj.get("_actor")

    if session_name is not None:
        lattice_dir = ctx.obj.get("_lattice_dir")
        if lattice_dir is None:
            lattice_dir = require_root(is_json)
            ctx.obj["_lattice_dir"] = lattice_dir

        session_data = resolve_session(lattice_dir, session_name)
        if session_data is None:
            output_error(
                f"No active session named '{session_name}'. "
                "Start one with 'lattice session start'.",
                "SESSION_NOT_FOUND",
                is_json,
            )
        touch_session(lattice_dir, session_name)

        result: str | dict = _build_actor_dict(session_data)
        ctx.obj["_resolved_actor"] = result
        return result

    if actor_str is not None:
        validate_actor_format_or_exit(actor_str, is_json)
        ctx.obj["_resolved_actor"] = actor_str
        return actor_str

    if optional:
        return None

    output_error(
        "Either --name (session) or --actor (legacy) is required.",
        "MISSING_ACTOR",
        is_json,
    )


def validate_actor_format_or_exit(actor: str, is_json: bool) -> None:
    """Validate a legacy actor string format.  Exits on failure.

    Used for secondary actor fields like ``--on-behalf-of`` where only
    format validation is needed (no session resolution).
    """
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


def _store_session_name(ctx: click.Context, _param: click.Parameter, value: str | None) -> None:
    """Store --name value on Click context for later resolution."""
    ctx.ensure_object(dict)
    ctx.obj["_session_name"] = value


def _store_actor(ctx: click.Context, _param: click.Parameter, value: str | None) -> None:
    """Store --actor value on Click context for later resolution."""
    ctx.ensure_object(dict)
    ctx.obj["_actor"] = value


def common_options(f):  # noqa: ANN001, ANN201
    """Decorator adding common write-command options.

    Identity flags (``--name``, ``--actor``) are stored on the Click
    context and resolved lazily via ``require_actor()``.  Commands
    should call ``require_actor(is_json)`` instead of reading an
    ``actor`` parameter.
    """
    f = click.option("--quiet", is_flag=True, help="Print only the primary ID.")(f)
    f = click.option("--json", "output_json", is_flag=True, help="Output structured JSON.")(f)
    f = click.option("--session", default=None, help="Session identifier (legacy).")(f)
    f = click.option("--model", default=None, help="Model identifier (legacy).")(f)
    f = click.option(
        "--actor",
        default=None,
        expose_value=False,
        callback=_store_actor,
        help="Actor (e.g., human:atin, agent:claude). Deprecated: prefer --name.",
    )(f)
    f = click.option(
        "--name",
        "session_name",
        default=None,
        expose_value=False,
        callback=_store_session_name,
        is_eager=True,
        help="Session name (e.g., Argus-3). Resolves to full identity.",
    )(f)
    f = click.option("--reason", "provenance_reason", default=None, help="Reason (provenance).")(f)
    f = click.option(
        "--on-behalf-of", default=None, help="Actor on whose behalf this action is taken."
    )(f)
    f = click.option("--triggered-by", default=None, help="Event ID that triggered this action.")(
        f
    )
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


# ---------------------------------------------------------------------------
# Resource helpers
# ---------------------------------------------------------------------------


def resolve_resource(
    lattice_dir: Path,
    name_or_id: str,
    is_json: bool,
) -> tuple[str, str, dict | None]:
    """Resolve a resource name or ID to (resource_id, name, snapshot_or_None).

    Resolution order:
    1. Check ``res_`` ULID format -> direct lookup in events/
    2. Scan ``.lattice/resources/*/resource.json`` for matching ``name``
    3. Check ``config.resources`` for matching key -> return (None, name, None) for auto-create
    4. Error out
    """
    # 1. Direct ULID
    if validate_id(name_or_id, "res"):
        # Find by scanning resource dirs for matching id
        resources_dir = lattice_dir / "resources"
        if resources_dir.is_dir():
            for res_dir in resources_dir.iterdir():
                if not res_dir.is_dir():
                    continue
                snap_path = res_dir / "resource.json"
                if snap_path.exists():
                    snap = json.loads(snap_path.read_text())
                    if snap.get("id") == name_or_id:
                        return name_or_id, snap["name"], snap
        output_error(f"Resource with ID '{name_or_id}' not found.", "NOT_FOUND", is_json)

    # 2. Scan by name
    resources_dir = lattice_dir / "resources"
    if resources_dir.is_dir():
        for res_dir in resources_dir.iterdir():
            if not res_dir.is_dir():
                continue
            snap_path = res_dir / "resource.json"
            if snap_path.exists():
                snap = json.loads(snap_path.read_text())
                if snap.get("name") == name_or_id:
                    return snap["id"], name_or_id, snap

    # 3. Check config for auto-create
    config = load_project_config(lattice_dir)
    config_resources = config.get("resources", {})
    if name_or_id in config_resources:
        return "", name_or_id, None  # empty id signals auto-create needed

    output_error(
        f"Resource '{name_or_id}' not found. Create it with 'lattice resource create {name_or_id}'.",
        "NOT_FOUND",
        is_json,
    )


def read_resource_snapshot(lattice_dir: Path, resource_name: str) -> dict | None:
    """Read a resource snapshot by name, returning None if not found."""
    snap_path = lattice_dir / "resources" / resource_name / "resource.json"
    if not snap_path.exists():
        return None
    return json.loads(snap_path.read_text())


def read_resource_snapshot_or_exit(lattice_dir: Path, resource_name: str, is_json: bool) -> dict:
    """Read a resource snapshot or exit with NOT_FOUND error."""
    snapshot = read_resource_snapshot(lattice_dir, resource_name)
    if snapshot is None:
        output_error(f"Resource '{resource_name}' not found.", "NOT_FOUND", is_json)
    return snapshot


def list_all_resources(lattice_dir: Path) -> list[dict]:
    """Return a list of all resource snapshots."""
    resources_dir = lattice_dir / "resources"
    results = []
    if not resources_dir.is_dir():
        return results
    for res_dir in sorted(resources_dir.iterdir()):
        if not res_dir.is_dir():
            continue
        snap_path = res_dir / "resource.json"
        if snap_path.exists():
            results.append(json.loads(snap_path.read_text()))
    return results


# ---------------------------------------------------------------------------
# Plan validation helpers (shared by status + next --claim)
# ---------------------------------------------------------------------------


def is_scaffold_plan(content: str, *, description: str | None = None) -> bool:
    """Return True when plan content still matches the default scaffold placeholders.

    The scaffold is minimal: just ``# <title>`` and optionally the task
    description as a paragraph.  A plan that has been "filled in" will
    contain sub-headings, lists, code fences, or other structural
    elements — OR plain-text content that differs from the auto-generated
    description paragraph.

    When *description* is provided, a plan consisting only of heading +
    that exact description text is still considered scaffold.  Without
    *description*, any non-empty text beyond the heading is accepted as
    a real plan (one-line plans are valid).
    """
    stripped = content.strip()
    if not stripped:
        return True
    lines = stripped.splitlines()
    # Must start with a heading to look like a scaffold at all.
    if not lines[0].startswith("# "):
        return False

    # Collect non-empty, non-heading body lines.
    body_lines = [lt for line in lines[1:] if (lt := line.strip())]

    if not body_lines:
        # Only a heading, no body → still scaffold.
        return True

    # If there's structural markdown content, it's definitely filled in.
    for lt in body_lines:
        if lt.startswith(("## ", "### ", "- ", "* ", "```")):
            return False
        if len(lt) > 2 and lt[0].isdigit() and ". " in lt[:5]:
            return False

    # Plain text exists. If we have the original description, check whether
    # the body is just the auto-generated description (still scaffold).
    if description:
        body_text = "\n".join(body_lines)
        desc_text = "\n".join(
            lt for line in description.strip().splitlines() if (lt := line.strip())
        )
        if body_text == desc_text:
            return True  # Body is just the description → scaffold.

    # Plain text that isn't the auto-generated description → real plan.
    return False


def check_plan_gate(
    lattice_dir: Path,
    task_id: str,
    target_status: str,
    is_json: bool,
    *,
    force: bool = False,
    reason: str | None = None,
) -> None:
    """Block transition to in_progress if the plan file is still scaffold.

    Does nothing if *target_status* is not ``in_progress`` or if *force* is True
    (with a reason).  Calls ``output_error`` (which raises SystemExit) when the
    gate fires.
    """
    if target_status != "in_progress":
        return
    if force:
        if not reason:
            output_error(
                "--reason is required with --force.",
                "VALIDATION_ERROR",
                is_json,
            )
        return

    plan_path = lattice_dir / "plans" / f"{task_id}.md"
    if not plan_path.exists():
        output_error(
            f"Plan file missing for {task_id}. "
            "Write a plan before moving to in_progress. "
            "Override with --force --reason.",
            "PLAN_REQUIRED",
            is_json,
        )

    try:
        content = plan_path.read_text()
    except OSError:
        return  # Can't read → don't block (filesystem issue, not a planning issue)

    # Load the task description so we can distinguish "plan is just the
    # auto-generated description" from "plan has real content".
    description: str | None = None
    snap_path = lattice_dir / "tasks" / f"{task_id}.json"
    try:
        snap = json.loads(snap_path.read_text())
        description = snap.get("description")
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        pass

    if is_scaffold_plan(content, description=description):
        output_error(
            f"Plan for {task_id} is still scaffold. "
            "Write the plan (even one line) before moving to in_progress. "
            "Override with --force --reason.",
            "PLAN_REQUIRED",
            is_json,
        )
