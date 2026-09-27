"""``lattice server``: run and administer a Lattice server (SPEC §8.2).

Admin is shell access to the server host. Every command takes ``--root``
(else ``$LATTICE_SERVER_ROOT``, else ``$XDG_DATA_HOME/lattice-server``) and
``--json``. Only ``serve`` needs the ``server`` extra.
"""

from __future__ import annotations

import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any

import click

from lattice.cli.helpers import json_envelope, output_error
from lattice.cli.main import cli
from lattice.core.errors import OpError

_REVIEW_MODES = click.Choice(["inline", "single", "triple"], case_sensitive=False)


def _root_option(fn: Callable) -> Callable:
    return click.option(
        "--root",
        "root",
        default=None,
        help="Server root (default: $LATTICE_SERVER_ROOT, else $XDG_DATA_HOME/lattice-server).",
    )(fn)


def _json_option(fn: Callable) -> Callable:
    return click.option("--json", "is_json", is_flag=True, help="Output as JSON.")(fn)


def _root(root: str | None) -> Path:
    from lattice.server.config import resolve_root

    return resolve_root(root)


def _hosted_platform() -> None:
    try:
        import fcntl  # noqa: F401
    except ImportError:
        raise OpError(
            "HOSTED_UNSUPPORTED_PLATFORM",
            "Hosted mode needs a POSIX platform (macOS or Linux).",
        ) from None


def _run(is_json: bool, fn: Callable[[], Any], render: Callable[[Any], str]) -> None:
    """Run an admin action and print its result, or its ``OpError`` in the usual envelope."""
    try:
        _hosted_platform()
        data = fn()
    except OpError as exc:
        output_error(exc.message, exc.code, is_json)
    if is_json:
        click.echo(json_envelope(True, data=data), nl=False)
    else:
        text = render(data)
        if text:
            click.echo(text)


@cli.group()
def server() -> None:
    """Run and administer a Lattice server (hosted mode)."""


@server.command("init")
@_root_option
@_json_option
def server_init(root: str | None, is_json: bool) -> None:
    """Create the server root, server.json, and an empty tokens.json. Idempotent."""
    from lattice.server import admin

    path = _root(root)

    def render(data: dict) -> str:
        created = ", ".join(data["created"]) or "nothing new"
        return f"Server root {data['root']} ready (created: {created})."

    _run(is_json, lambda: admin.init_root(path), render)


@server.command("serve")
@_root_option
@click.option("--host", default=None, help="Bind address (default: server.json bind).")
@click.option("--port", type=int, default=None, help="Port (default: server.json port).")
def server_serve(root: str | None, host: str | None, port: int | None) -> None:
    """Run the server in the foreground until SIGTERM."""
    from lattice.server.serve import INSTALL_HINT, server_extra_available

    try:
        _hosted_platform()
    except OpError as exc:
        output_error(exc.message, exc.code, False)
    if not server_extra_available():
        click.echo(f"Error: {INSTALL_HINT}", err=True)
        sys.exit(1)
    from lattice.server.serve import serve

    try:
        serve(_root(root), host=host, port=port)
    except OpError as exc:
        output_error(exc.message, exc.code, False)


# ---------------------------------------------------------------------------
# Projects
# ---------------------------------------------------------------------------


@server.group("project")
def project_group() -> None:
    """Create and administer hosted projects."""


@project_group.command("create")
@click.argument("slug")
@click.option("--code", default=None, help="Project code for short IDs (e.g. LAT).")
@click.option("--subproject-code", default=None, help="Subproject code (needs --code).")
@click.option("--review-mode", type=_REVIEW_MODES, default=None, help="Code review mode.")
@click.option("--plan-review-mode", type=_REVIEW_MODES, default=None, help="Plan review mode.")
@click.option(
    "--plan-approval",
    type=click.Choice(["auto", "human"], case_sensitive=False),
    default=None,
    help="Plan approval gate.",
)
@click.option(
    "--auto-code-review/--no-auto-code-review",
    default=None,
    help="Auto-run a code review on a move to review (default on).",
)
@click.option(
    "--auto-plan-review/--no-auto-plan-review",
    default=None,
    help="Auto-run a plan review on a move to planned (default on).",
)
@_root_option
@_json_option
def project_create(
    slug: str,
    code: str | None,
    subproject_code: str | None,
    review_mode: str | None,
    plan_review_mode: str | None,
    plan_approval: str | None,
    auto_code_review: bool | None,
    auto_plan_review: bool | None,
    root: str | None,
    is_json: bool,
) -> None:
    """Create a project's board, exactly as 'lattice init' would, plus its journal."""
    from lattice.server import admin

    def action() -> dict:
        return admin.create_project(
            _root(root),
            slug,
            code=code,
            subproject_code=subproject_code,
            review_mode=review_mode.lower() if review_mode else None,
            plan_review_mode=plan_review_mode.lower() if plan_review_mode else None,
            plan_approval=plan_approval.lower() if plan_approval else None,
            auto_code_review=auto_code_review,
            auto_plan_review=auto_plan_review,
        )

    _run(is_json, action, lambda d: f"Created project {d['slug']} at {d['path']}.")


@project_group.command("list")
@_root_option
@_json_option
def project_list(root: str | None, is_json: bool) -> None:
    """Slug, project code, head seq, task count, state, owner."""
    from lattice.server import admin

    def render(rows: list[dict]) -> str:
        if not rows:
            return "No projects."
        lines = []
        for row in rows:
            owner = row.get("owner") or {}
            where = f"{owner.get('host')} pid {owner.get('pid')}" if owner else "-"
            lines.append(
                f"{row['slug']}  code={row['project_code'] or '-'}  seq={row['head_seq']}  "
                f"tasks={row['task_count']}  {row['state']}  owner={where}"
            )
        return "\n".join(lines)

    _run(is_json, lambda: admin.list_projects(_root(root)), render)


@project_group.command("unlock")
@click.argument("slug")
@_root_option
@_json_option
def project_unlock(slug: str, root: str | None, is_json: bool) -> None:
    """Remove a stale owner marker when no process holds the project."""
    from lattice.server import admin

    def render(data: dict) -> str:
        if data["removed"]:
            return f"Removed the stale owner marker of {slug}."
        return f"{slug} had no owner marker."

    _run(is_json, lambda: admin.unlock_project(_root(root), slug), render)


@project_group.command("config")
@click.argument("slug")
@click.option(
    "--set",
    "assignments",
    multiple=True,
    required=True,
    metavar="KEY=VALUE",
    help="review_mode, plan_review_mode, plan_approval, auto_code_review_on_transition, "
    "auto_plan_review_on_transition.",
)
@_root_option
@_json_option
def project_config(
    slug: str, assignments: tuple[str, ...], root: str | None, is_json: bool
) -> None:
    """Change a project's review workflow."""
    from lattice.server import admin

    def action() -> dict:
        changes = admin.parse_config_assignments(assignments)
        return admin.set_project_config(_root(root), slug, changes)

    def render(data: dict) -> str:
        pairs = ", ".join(f"{k}={v}" for k, v in data["set"].items())
        if data["via"] == "server":
            return f"Set {pairs} on {slug} (journal seq {data['seq']})."
        return (
            f"Set {pairs} on {slug}. No server is running; the next load starts a new "
            "epoch so every cache resyncs."
        )

    _run(is_json, action, render)


# ---------------------------------------------------------------------------
# Tokens
# ---------------------------------------------------------------------------


@server.group("token")
def token_group() -> None:
    """Mint, list, scope, and revoke access tokens."""


def _describe_token(record: dict) -> str:
    projects = ", ".join(record["projects"]) or "(none)"
    actors = ", ".join(record["actors"]) or "(none)"
    revoked = f"  revoked {record['revoked_at']}" if record.get("revoked_at") else ""
    return (
        f"{record['id']}  {record['user']} @ {record['machine']}  actors: {actors}  "
        f"projects: {projects}  created {record['created_at']}{revoked}"
    )


@token_group.command("create")
@click.option("--user", required=True, help="The person the token is issued to (human:NAME).")
@click.option("--machine", required=True, help="The machine or seat it is issued for.")
@click.option(
    "--actor",
    "actors",
    multiple=True,
    help="Actor pattern it may act as (repeatable; replaces the default <user>, agent:*).",
)
@click.option("--project", "projects", multiple=True, help="Project slug (repeatable).")
@click.option("--all-projects", is_flag=True, help="Every project on this server.")
@_root_option
@_json_option
def token_create(
    user: str,
    machine: str,
    actors: tuple[str, ...],
    projects: tuple[str, ...],
    all_projects: bool,
    root: str | None,
    is_json: bool,
) -> None:
    """Mint a token and print it once."""
    from lattice.server import tokens

    def action() -> dict:
        data = tokens.create_token(
            _root(root),
            user=user,
            machine=machine,
            actors=actors,
            projects=projects,
            all_projects=all_projects,
        )
        if data["warning"]:
            click.echo(f"Warning: {data['warning']}", err=True)
        return data

    def render(data: dict) -> str:
        record = data["record"]
        return (
            f"{data['token']}\n"
            f"Token {record['id']} for {record['user']} @ {record['machine']}; it may act as: "
            f"{', '.join(record['actors'])}; projects: {', '.join(record['projects']) or '(none)'}.\n"
            "This is the only time the token is shown. Store it now."
        )

    _run(is_json, action, render)


@token_group.command("list")
@_root_option
@_json_option
def token_list(root: str | None, is_json: bool) -> None:
    """Every token's id, user, machine, actors, projects, created, revoked (never the secret)."""
    from lattice.server import tokens

    _run(
        is_json,
        lambda: tokens.list_tokens(_root(root)),
        lambda rows: "\n".join(_describe_token(r) for r in rows) or "No tokens.",
    )


@token_group.command("revoke")
@click.argument("token_id")
@_root_option
@_json_option
def token_revoke(token_id: str, root: str | None, is_json: bool) -> None:
    """Revoke a token; effective on the server's next request."""
    from lattice.server import tokens

    _run(
        is_json,
        lambda: tokens.revoke_token(_root(root), token_id),
        lambda r: f"Revoked {r['id']} at {r['revoked_at']}.",
    )


def _scope_command(name: str, verb: str, action_text: str) -> Callable:
    @token_group.command(
        name, help=f"{action_text} projects or actor patterns (effective next request)."
    )
    @click.argument("token_id")
    @click.option("--project", "projects", multiple=True, help="Project slug (repeatable).")
    @click.option("--actor", "actors", multiple=True, help="Actor pattern (repeatable).")
    @_root_option
    @_json_option
    def command(
        token_id: str,
        projects: tuple[str, ...],
        actors: tuple[str, ...],
        root: str | None,
        is_json: bool,
    ) -> None:
        from lattice.server import tokens

        fn = getattr(tokens, name)
        _run(
            is_json,
            lambda: fn(_root(root), token_id, projects=projects, actors=actors),
            lambda r: f"{verb} {r['id']}: " + _describe_token(r),
        )

    return command


token_grant = _scope_command("grant", "Granted", "Add")
token_ungrant = _scope_command("ungrant", "Ungranted", "Remove")
