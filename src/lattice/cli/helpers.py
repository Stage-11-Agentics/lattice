"""Shared CLI helpers, decorators, and output utilities."""

from __future__ import annotations

import json
from pathlib import Path
from typing import TYPE_CHECKING, NoReturn

import click

from lattice.core.actors import build_actor_dict
from lattice.core.errors import OpError
from lattice.core.ids import is_short_id, validate_actor, validate_id
from lattice.core.plans import is_scaffold_plan  # noqa: F401 - CLI re-export
from lattice.storage.fs import LATTICE_DIR, LatticeRootError, find_root
from lattice.storage.operations import (
    AuthoritativeLogError,
    mutate_task_events,  # noqa: F401 - CLI re-export
    read_task_authority,
)
from lattice.storage.resources import (  # noqa: F401 - CLI re-export
    list_all_resources,
    read_resource_snapshot,
)
from lattice.storage.short_ids import resolve_short_id as _resolve_short

if TYPE_CHECKING:
    from lattice.remote.binding import Hosted

# ---------------------------------------------------------------------------
# Session → actor dict helper (single source of truth)
# ---------------------------------------------------------------------------


_build_actor_dict = build_actor_dict


# ---------------------------------------------------------------------------
# Root & config
# ---------------------------------------------------------------------------


def end_read_phase(lattice_dir: Path) -> None:
    """Release a hosted cache's shared read lock before this command starts or
    waits on another process (SPEC §9.4, "Writer preference"); a later read
    through :func:`require_root` or a board takes it again. A no-op on a local
    board, which never imports the hosted client for it."""
    import sys

    if "lattice.remote.session" not in sys.modules:
        return
    from lattice.remote.session import release_read_lock

    release_read_lock(Path(lattice_dir).parent)


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
    hosted = hosted_or_exit(root, is_json)
    if hosted is not None:
        from lattice.remote.session import prepare_read

        try:
            return prepare_read(hosted, lock=not _long_running_command())
        except OpError as exc:
            output_error(exc.message, exc.code, is_json)
    return root / LATTICE_DIR


#: Commands that run until stopped. They catch up before their first read like
#: any command, but holding the cache's shared read lock for their lifetime
#: would starve every sync on the machine, so they read without it.
LONG_RUNNING_COMMANDS = frozenset({"dashboard", "watch", "wait"})


def _long_running_command() -> bool:
    ctx = click.get_current_context(silent=True)
    while ctx is not None and ctx.parent is not None and ctx.parent.parent is not None:
        ctx = ctx.parent
    return ctx is not None and ctx.info_name in LONG_RUNNING_COMMANDS


def hosted_or_exit(root: Path, is_json: bool) -> Hosted | None:
    """The hosted identity of *root* (SPEC §9.3), ``None`` for a local board, or
    the routing error printed as usual. Local boards import nothing hosted."""
    from lattice.storage.fs import BINDING_FILE

    if not (root / BINDING_FILE).exists() and not (root / LATTICE_DIR / "cache").is_dir():
        return None
    from lattice.remote.binding import classify, require_supported

    try:
        hosted = classify(root)
        if hosted is not None:
            require_supported()
    except OpError as exc:
        _scrub_hosted_output()  # the message quotes the binding (SPEC §4)
        output_error(exc.message, exc.code, is_json)
    return hosted


def _scrub_hosted_output() -> None:
    from lattice.remote.session import scrub_output

    scrub_output()


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


#: Error codes whose ``--json`` envelope carries the error's ``details``
#: (SPEC §8.6: an unreachable server's URL and raw OS error). Only hosted
#: checkouts raise them, so local output is unchanged.
_ENVELOPE_DETAILS = frozenset({"SERVER_UNREACHABLE"})


def _handled_details(code: str) -> dict | None:
    """The ``details`` of the ``OpError`` being handled, for the codes above."""
    import sys

    exc = sys.exc_info()[1]
    if code in _ENVELOPE_DETAILS and isinstance(exc, OpError) and exc.code == code:
        return exc.details or None
    return None


def output_error(message: str, code: str, is_json: bool, exit_code: int = 1) -> NoReturn:
    """Print error and exit. JSON errors go to stdout; human errors to stderr.

    Called while handling an ``OpError`` whose code is in ``_ENVELOPE_DETAILS``,
    the JSON error also carries that error's ``details``."""
    if is_json:
        error = json_error_obj(code, message)
        details = _handled_details(code)
        if details:
            error["details"] = details
        click.echo(json_envelope(False, error=error))
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
# Prose bodies: inline argument or --file
# ---------------------------------------------------------------------------


def resolve_body(
    text: str | None,
    file_path: str | None,
    is_json: bool,
    *,
    what: str,
    arg_label: str,
    file_label: str = "--file",
    missing_message: str | None = None,
) -> str:
    """Resolve a prose body from an inline argument or a file.

    Exactly one of *text* / *file_path* must be given; both or neither exits
    through ``output_error(..., "VALIDATION_ERROR", ...)``.

    The file path exists so a long body never has to survive a shell: inside a
    double-quoted argument, backticks and ``$(...)`` are command substitution.
    That has silently eaten a clause from one comment and spliced 15 KB of
    pytest output into another (LAT-263). Reading from a file is byte-exact.
    """
    if text is not None and file_path is not None:
        output_error(
            f"Provide either {arg_label} or {file_label}, not both.",
            "VALIDATION_ERROR",
            is_json,
        )
    if text is None and file_path is None:
        output_error(
            missing_message or f"Provide {what} as {arg_label} or via {file_label}.",
            "VALIDATION_ERROR",
            is_json,
        )
    if file_path is not None:
        return Path(file_path).read_text(encoding="utf-8")
    return text  # type: ignore[return-value]


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

    Resolution is ``lattice.ops.base.resolve_actor`` (SPEC §3.7), the same
    function operations use; a session actor's session is then touched under
    the ``sessions_index`` lock.

    Set *optional* to ``True`` for commands where identity is not
    required (e.g., ``lattice next`` without ``--claim``).  Returns
    ``None`` when no identity flags were provided.
    """
    from lattice.ops.base import Caller, resolve_actor
    from lattice.storage.sessions import touch_session

    ctx = click.get_current_context()
    ctx.ensure_object(dict)

    # Return cached result
    if "_resolved_actor" in ctx.obj:
        return ctx.obj["_resolved_actor"]

    session_name = ctx.obj.get("_session_name")
    actor_str = ctx.obj.get("_actor")

    if session_name is None and actor_str is None and optional:
        return None

    lattice_dir = ctx.obj.get("_lattice_dir")
    if session_name is not None and lattice_dir is None:
        lattice_dir = require_root(is_json)
        ctx.obj["_lattice_dir"] = lattice_dir

    try:
        result = resolve_actor(lattice_dir, Caller(actor=actor_str, actor_name=session_name))
    except OpError as exc:
        output_error(exc.message, exc.code, is_json)
    if session_name is not None and not _is_cache(lattice_dir):
        # A hosted cache is read-only: only the writer (the server) touches
        # sessions, so a read resolves the session without writing (SPEC §9.5).
        touch_session(lattice_dir, session_name)
    ctx.obj["_resolved_actor"] = result
    return result


def program_name() -> str:
    """The name this program was invoked as, for the commands hints print.

    The console script's basename (``lattice``, or an alias such as
    ``lattice-v2``) with a Windows ``.exe`` dropped. Outside a command, and
    under Click's test runner (whose default name is the group's, ``cli``),
    and under ``python -c`` (whose name is ``-c``), it is ``lattice``.
    """
    ctx = click.get_current_context(silent=True)
    name = ctx.find_root().info_name if ctx is not None else None
    if not name or name == "cli" or name.startswith("-"):
        return "lattice"
    return name[:-4] if name.lower().endswith(".exe") else name


def _is_cache(lattice_dir: Path | None) -> bool:
    if lattice_dir is None:
        return False
    cache_dir = Path(lattice_dir) / "cache"
    return (cache_dir / "state.json").exists() or (cache_dir / "applying").exists()


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
    """Read the authoritative active task view, returning None if not found."""
    try:
        authority = read_task_authority(lattice_dir, task_id, allow_missing=True)
    except AuthoritativeLogError:
        return None
    if authority is None or authority.location != "active":
        return None
    return authority.snapshot


def read_task_view(lattice_dir: Path, task_id: str) -> tuple[dict, bool] | None:
    """Read a task at its event-selected placement."""
    try:
        authority = read_task_authority(lattice_dir, task_id, allow_missing=True)
    except AuthoritativeLogError:
        return None
    if authority is None:
        return None
    return authority.snapshot, authority.location == "archived"


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
    """``storage.resources.find_resource`` against the board's config; exits on ``NOT_FOUND``."""
    from lattice.storage.resources import find_resource

    try:
        return find_resource(lattice_dir, name_or_id, load_project_config(lattice_dir))
    except OpError as exc:
        output_error(exc.message, exc.code, is_json)


def read_resource_snapshot_or_exit(lattice_dir: Path, resource_name: str, is_json: bool) -> dict:
    """Read a resource snapshot or exit with NOT_FOUND error."""
    snapshot = read_resource_snapshot(lattice_dir, resource_name)
    if snapshot is None:
        output_error(f"Resource '{resource_name}' not found.", "NOT_FOUND", is_json)
    return snapshot


# ---------------------------------------------------------------------------
# Plan validation helpers (shared by status + next --claim)
# ---------------------------------------------------------------------------


def check_plan_gate(
    lattice_dir: Path,
    task_id: str,
    target_status: str,
    is_json: bool,
    config: dict,
    *,
    force: bool = False,
    reason: str | None = None,
    authoritative_snapshot: dict | None = None,
    authoritative_location: str | None = None,
) -> None:
    """The plan gate for commands not yet converted to operations; exits on refusal."""
    from lattice.ops.plan_gate import check_plan_gate as _check_plan_gate

    try:
        _check_plan_gate(
            lattice_dir,
            task_id,
            target_status,
            config,
            force=force,
            reason=reason,
            authoritative_snapshot=authoritative_snapshot,
            authoritative_location=authoritative_location,
        )
    except OpError as exc:
        output_error(exc.message, exc.code, is_json)
