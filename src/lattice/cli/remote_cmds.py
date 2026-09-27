"""``lattice remote``: per-user remotes and this checkout's binding (SPEC §9.1, §9.2).

- ``add`` / ``list`` manage ``$XDG_CONFIG_HOME/lattice/remotes.json`` (0600).
- ``attach`` binds the clone to a server project from any of its worktrees.
- ``status`` reports the binding, the server's view of this token, the cache,
  and branches that still track ``.lattice/``.
- ``op-status`` asks the server whether an operation committed (after
  ``OUTCOME_UNKNOWN``).
"""

from __future__ import annotations

import json
import re
import subprocess
import sys
from pathlib import Path

import click

from lattice.cli.helpers import json_envelope, output_error
from lattice.cli.main import cli
from lattice.core.errors import OpError
from lattice.storage.fs import BINDING_FILE, LATTICE_DIR

_HEADER_RE = re.compile(r"^([!#$%&'*+.^_`|~0-9A-Za-z-]+)=([A-Za-z_][A-Za-z0-9_]*)$")
_OP_ID_RE = re.compile(r"^op_[0-9A-HJKMNP-TV-Z]{26}$")
_IGNORE_LINE = "/.lattice/"
_IGNORE_EQUIVALENTS = {".lattice", ".lattice/", "/.lattice", "/.lattice/"}
REFRESH_COMMANDS = ("lattice setup-claude --force", "lattice setup-claude-skill --force")


def _emit(is_json: bool, data: dict, lines: list[str]) -> None:
    if is_json:
        click.echo(json_envelope(True, data=data), nl=False)
    else:
        for line in lines:
            click.echo(line)


def _fail(exc: OpError, is_json: bool) -> None:
    output_error(exc.message, exc.code, is_json)


@cli.group("remote")
def remote_group() -> None:
    """Lattice servers this machine can reach, and this checkout's binding."""


# ---------------------------------------------------------------------------
# add / list
# ---------------------------------------------------------------------------


@remote_group.command("add")
@click.argument("alias")
@click.argument("url")
@click.option("--token-env", default=None, help="Environment variable holding the token.")
@click.option("--token-stdin", is_flag=True, help="Read the token from stdin (stored 0600).")
@click.option(
    "--header",
    "headers",
    multiple=True,
    help="NAME=ENVVAR: send header NAME with the value of ENVVAR (repeatable).",
)
@click.option(
    "--allow-plaintext",
    is_flag=True,
    help="Allow http:// to a host that is not loopback (an encrypted private network).",
)
@click.option("--json", "output_json", is_flag=True, help="Output structured JSON.")
def remote_add(
    alias: str,
    url: str,
    token_env: str | None,
    token_stdin: bool,
    headers: tuple[str, ...],
    allow_plaintext: bool,
    output_json: bool,
) -> None:
    """Add (or replace) a remote in remotes.json."""
    from lattice.remote.config import add_remote, check_url

    is_json = output_json
    if token_env is not None and token_stdin:
        output_error(
            "Provide either --token-env or --token-stdin, not both.", "VALIDATION_ERROR", is_json
        )
    if token_env is not None and not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", token_env):
        output_error(
            f"--token-env {token_env!r} is not an environment variable name.",
            "VALIDATION_ERROR",
            is_json,
        )
    header_map: dict[str, str] = {}
    for raw in headers:
        match = _HEADER_RE.match(raw)
        if match is None:
            output_error(
                f"--header {raw!r} must be NAME=ENVVAR (the header's value is read from "
                "ENVVAR at request time; it is never stored).",
                "VALIDATION_ERROR",
                is_json,
            )
        header_map[match.group(1)] = match.group(2)
    try:
        check_url(alias, url, allow_plaintext=allow_plaintext)
    except OpError as exc:
        _fail(exc, is_json)
    token: str | dict | None = None
    if token_env is not None:
        token = {"env": token_env}
    elif token_stdin:
        token = sys.stdin.readline().strip()
        if not token:
            output_error("--token-stdin read an empty token.", "VALIDATION_ERROR", is_json)
    try:
        path = add_remote(
            alias, url, token=token, headers=header_map, allow_plaintext=allow_plaintext
        )
    except OpError as exc:
        _fail(exc, is_json)
    source = (
        f"token from ${token_env}"
        if token_env
        else "token stored in remotes.json (0600)"
        if token_stdin
        else "no token"
    )
    _emit(
        is_json,
        {
            "alias": alias,
            "url": url.rstrip("/"),
            "path": str(path),
            "token": "env" if token_env else "stored" if token_stdin else None,
            "headers": sorted(header_map),
        },
        [f"Added remote '{alias}': {url.rstrip('/')} ({source}); wrote {path}"],
    )


@remote_group.command("list")
@click.option("--json", "output_json", is_flag=True, help="Output structured JSON.")
def remote_list(output_json: bool) -> None:
    """Show configured remotes (never tokens)."""
    from lattice.remote.config import list_remotes

    try:
        rows = list_remotes()
    except OpError as exc:
        _fail(exc, output_json)
    lines = [f"{row['alias']}\t{row['url']}" for row in rows] or ["No remotes configured."]
    _emit(output_json, {"remotes": rows}, lines)


# ---------------------------------------------------------------------------
# attach
# ---------------------------------------------------------------------------


def _git(cwd: Path, *args: str) -> str | None:
    try:
        proc = subprocess.run(
            ["git", *args], cwd=cwd, capture_output=True, text=True, timeout=10, check=False
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return proc.stdout.strip() if proc.returncode == 0 else None


def _git_common_dir(start: Path) -> Path | None:
    common = _git(start, "rev-parse", "--path-format=absolute", "--git-common-dir")
    return Path(common) if common else None


def _primary_checkout(start: Path) -> tuple[Path, Path | None]:
    """The primary checkout a command in *start* belongs to, and the clone's
    ``$GIT_COMMON_DIR`` (``None`` outside git)."""
    common = _git_common_dir(start)
    if common is None:
        return start.resolve(), None
    if common.name == ".git":
        return common.parent, common
    top = _git(start, "rev-parse", "--show-toplevel")
    return (Path(top) if top else start.resolve()), common


def _append_ignore(path: Path) -> bool:
    """Add ``/.lattice/`` to *path* unless a line already ignores it; returns
    whether the file changed."""
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        text = ""
    if any(line.strip() in _IGNORE_EQUIVALENTS for line in text.splitlines()):
        return False
    path.parent.mkdir(parents=True, exist_ok=True)
    prefix = "" if not text or text.endswith("\n") else "\n"
    path.write_text(f"{text}{prefix}{_IGNORE_LINE}\n", encoding="utf-8")
    return True


def _visible_projects(remote: object) -> list[str]:
    from lattice.remote.client import get_json

    data = get_json(remote, "/v1/projects")  # type: ignore[arg-type]
    return [row.get("slug") for row in data.get("projects", []) if isinstance(row, dict)]


@remote_group.command("attach")
@click.argument("alias")
@click.argument("project")
@click.option("--json", "output_json", is_flag=True, help="Output structured JSON.")
def remote_attach(alias: str, project: str, output_json: bool) -> None:
    """Bind this clone (every worktree of it) to PROJECT on remote ALIAS."""
    from lattice.remote import session
    from lattice.remote.binding import (
        MOVE_GUIDE,
        Hosted,
        holds_local_board,
        marker_identity,
        require_supported,
    )
    from lattice.remote.cache import catch_up
    from lattice.remote.config import resolve_remote

    is_json = output_json
    try:
        require_supported("lattice remote attach")
        remote = resolve_remote(alias)
        if project not in _visible_projects(remote):
            raise OpError(
                "NOT_FOUND",
                f"project '{project}' does not exist on {alias}, or this token cannot see "
                "it. Ask your server admin to create it or grant your token access.",
                {"remote": alias, "project": project},
            )
    except OpError as exc:
        _fail(exc, is_json)

    primary, common = _primary_checkout(Path.cwd())
    if holds_local_board(primary):
        output_error(
            f"{primary} holds a local board in {LATTICE_DIR}/; attaching would hide it. "
            f"Move the board to the server first: follow {MOVE_GUIDE}.",
            "BINDING_CONFLICT",
            is_json,
        )
    cached = marker_identity(primary)
    if cached is not None and cached != (alias, project):
        output_error(
            f"{primary} holds a cache of {cached[0]}/{cached[1]}. To rebind it to "
            f"{alias}/{project}, run 'lattice cache clear --forget' first.",
            "BINDING_CONFLICT",
            is_json,
        )

    binding = json.dumps({"project": project, "remote": alias}, sort_keys=True, indent=2) + "\n"
    (primary / BINDING_FILE).write_text(binding, encoding="utf-8")
    changed_gitignore = _append_ignore(primary / ".gitignore")
    if common is not None:
        _append_ignore(common / "info" / "exclude")

    hosted = Hosted(primary, alias, project)
    try:
        outcome = catch_up(primary, bulk=True)
    except OpError as exc:
        _fail(exc, is_json)
    if outcome.kind not in ("applied", "unchanged"):
        output_error(
            f"bound {primary} to {alias}/{project}, but the first sync did not complete "
            f"({outcome.kind}: {outcome.detail or 'no detail'}); run 'lattice sync' to retry.",
            "SERVER_UNREACHABLE",
            is_json,
        )
    session.close_unreachable_window(hosted)
    session.refresh_server_info(hosted, force=True)

    to_commit = [BINDING_FILE] + ([".gitignore"] if changed_gitignore else [])
    lines = [
        f"Attached {primary} to {alias}/{project} (cache at seq {outcome.head_seq}).",
        f"Commit: git add {' '.join(to_commit)} && git commit -m 'Bind the board to "
        f"{alias}/{project}'",
        "Refresh agent instructions installed before v2 (they tell agents to write plan "
        "files directly, which a cache refuses):",
        *(f"  {command}" for command in REFRESH_COMMANDS),
    ]
    _emit(
        is_json,
        {
            "root": str(primary),
            "remote": alias,
            "project": project,
            "head_seq": outcome.head_seq,
            "commit": to_commit,
            "refresh_commands": list(REFRESH_COMMANDS),
        },
        lines,
    )


# ---------------------------------------------------------------------------
# status / op-status
# ---------------------------------------------------------------------------


def _hosted_or_exit(is_json: bool):  # noqa: ANN202 - Hosted
    from lattice.remote.binding import hosted_root

    try:
        hosted = hosted_root(Path.cwd())
    except OpError as exc:
        _fail(exc, is_json)
    if hosted is None:
        output_error(
            "This checkout is not bound to a Lattice server. Bind it with "
            "'lattice remote attach <alias> <project>'.",
            "NOT_HOSTED",
            is_json,
        )
    return hosted


def tracking_branches(root: Path) -> list[str]:
    """Local and remote-tracking branches whose tree still holds ``.lattice/`` files."""
    refs = _git(root, "for-each-ref", "--format=%(refname)", "refs/heads", "refs/remotes")
    found = []
    for ref in (refs or "").splitlines():
        if ref.endswith("/HEAD"):
            continue
        listed = _git(root, "ls-tree", "-r", "--name-only", ref, "--", LATTICE_DIR)
        if listed:
            found.append(ref.removeprefix("refs/heads/").removeprefix("refs/remotes/"))
    return found


@remote_group.command("status")
@click.option("--json", "output_json", is_flag=True, help="Output structured JSON.")
def remote_status(output_json: bool) -> None:
    """Binding, server identity, cache state, and branches still tracking .lattice/."""
    from lattice.remote.binding import read_binding
    from lattice.remote.client import get_json
    from lattice.remote.config import resolve_remote
    from lattice.remote.follower import live_follower, read_follower

    is_json = output_json
    hosted = _hosted_or_exit(is_json)
    try:
        remote = resolve_remote(hosted.remote)
    except OpError as exc:
        _fail(exc, is_json)
    state: dict = {}
    try:
        state = json.loads(
            (hosted.lattice_dir / "cache" / "state.json").read_text(encoding="utf-8")
        )
    except (OSError, ValueError):
        pass
    identity = None
    server_head = None
    reachable = True
    error = None
    try:
        info = get_json(remote, "/v1/info")
        identity = info.get("identity")
        for row in get_json(remote, "/v1/projects").get("projects", []):
            if isinstance(row, dict) and row.get("slug") == hosted.project:
                server_head = row.get("head_seq")
    except OpError as exc:
        reachable = exc.code != "SERVER_UNREACHABLE"
        error = f"{exc.code}: {exc.message}"
    head = state.get("head_seq") if state.get("epoch") else None
    stale = None if server_head is None else (head is None or head < server_head)
    follower = read_follower(hosted.root)
    branches = tracking_branches(hosted.root)
    binding = read_binding(hosted.root)
    data = {
        "root": str(hosted.root),
        "remote": hosted.remote,
        "project": hosted.project,
        "binding": {"remote": binding[0], "project": binding[1]} if binding else None,
        "url": remote.url,
        "reachable": reachable,
        "error": error,
        "identity": identity,
        "cache": {
            "epoch": state.get("epoch"),
            "head_seq": head,
            "synced_at": state.get("synced_at"),
            "server_head_seq": server_head,
            "stale": stale,
        },
        "follower": {"live": live_follower(hosted.root), "record": follower},
        "branches_tracking_board": branches,
    }
    who = (
        f"{identity.get('user')} on {identity.get('machine')} (token {identity.get('token_id')})"
        if isinstance(identity, dict)
        else (error or "unknown")
    )
    freshness = (
        "unknown (server unreachable)"
        if stale is None
        else f"stale (server at {server_head})"
        if stale
        else "current"
    )
    lines = [
        f"Bound to: {hosted.label} ({remote.url})"
        + ("" if binding else f"  [no {BINDING_FILE} on this branch; routed by the cache]"),
        f"Identity: {who}",
        f"Cache: epoch {state.get('epoch') or '-'}, seq {head if head is not None else '-'}, "
        f"synced {state.get('synced_at') or 'never'}, {freshness}",
        f"Follower: {'live' if data['follower']['live'] else 'not running'}",
    ]
    if branches:
        lines.append(
            "Branches that still track .lattice/ (checking one out writes into the cache):"
        )
        lines.extend(f"  {name}" for name in branches)
        lines.append(
            "  Fix: merge the commit that untracked the board into each, or run "
            "'git rm -r --cached .lattice' on that branch and commit."
        )
    _emit(is_json, data, lines)


@remote_group.command("op-status")
@click.argument("op_id")
@click.option("--json", "output_json", is_flag=True, help="Output structured JSON.")
def remote_op_status(op_id: str, output_json: bool) -> None:
    """Whether one of this token's operations committed (after OUTCOME_UNKNOWN)."""
    from lattice.remote.client import op_status
    from lattice.remote.config import resolve_remote

    is_json = output_json
    if not _OP_ID_RE.match(op_id):
        output_error(
            f"'{op_id}' is not an operation id (op_ followed by a ULID).",
            "VALIDATION_ERROR",
            is_json,
        )
    hosted = _hosted_or_exit(is_json)
    try:
        data = op_status(resolve_remote(hosted.remote), hosted.project, op_id)
    except OpError as exc:
        _fail(exc, is_json)
    if data.get("state") == "committed":
        line = f"{op_id}: committed (epoch {data.get('epoch')}, seq {data.get('seq')})"
    else:
        line = (
            f"{op_id}: not found. It never committed, or it belongs to another token; "
            "running the command again applies it once."
        )
    _emit(is_json, {"op_id": op_id, **data}, [line])
