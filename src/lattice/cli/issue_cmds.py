"""The optional issue log (LAT-361): ``lattice issue file|list|show|promote|link|...``.

Issues are observations, kept apart from tasks, which are commitments. The log
is off unless ``.lattice/config.json`` has ``"issues": {"enabled": true}``, and
works only on local boards. Writes run the ``issue.*`` operations; ``list``
and ``show`` read the files directly.
"""

from __future__ import annotations

import click

from lattice.cli.helpers import (
    common_options,
    json_envelope,
    load_project_config,
    output_error,
    output_result,
    require_root,
)
from lattice.cli.main import cli
from lattice.cli.ops_bridge import provenance_params, run_operation
from lattice.core.errors import OpError


def _require_issue_log(is_json: bool) -> tuple:
    """``(lattice_dir, config)`` when the issue log can be used here, else the
    command's error: ``LOCAL_ONLY`` on a bound checkout (before anything is
    read or fetched), ``NOT_INITIALIZED``, or ``ISSUES_DISABLED``."""
    from lattice.boards import hosted_binding
    from lattice.core.config import issues_enabled
    from lattice.core.issues import issues_disabled_message
    from lattice.storage.issues import issues_dir

    try:
        binding = hosted_binding(None)
    except OpError as exc:
        output_error(exc.message, exc.code, is_json)
    if binding is not None:
        output_error(
            "The issue log works only on local boards for now; this checkout's board "
            f"lives on the server ('{binding}').",
            "LOCAL_ONLY",
            is_json,
        )
    lattice_dir = require_root(is_json)
    config = load_project_config(lattice_dir)
    if not issues_enabled(config):
        output_error(
            issues_disabled_message(issues_dir(lattice_dir).is_dir()), "ISSUES_DISABLED", is_json
        )
    return lattice_dir, config


def _write(op_name: str, params: dict, is_json: bool, checked: tuple | None = None) -> tuple:
    """``(lattice_dir, result)`` of running *op_name* after the issue-log checks."""
    lattice_dir, config = checked or _require_issue_log(is_json)
    return lattice_dir, run_operation(op_name, params, is_json, config=config)


def _name(view: dict) -> str:
    return view.get("short_id") or view["id"]


def _task_entry(view: dict, raw_task: str) -> dict | None:
    """The view's entry for the task the caller named (a ULID or a short ID)."""
    wanted = raw_task.upper()
    return next(
        (
            t
            for t in view.get("tasks", [])
            if t["id"].upper() == wanted or (t.get("short_id") or "").upper() == wanted
        ),
        None,
    )


def _warn_unreadable(path, exc: OpError) -> None:  # noqa: ANN001
    """Skip an unreadable issue file with one line on stderr (stdout stays clean)."""
    from lattice.core.issues import unreadable_issue_warning

    click.echo(unreadable_issue_warning(path, exc), err=True)


def _read_stdin_text() -> str:
    import sys

    return sys.stdin.read().rstrip()


# ---------------------------------------------------------------------------
# The group
# ---------------------------------------------------------------------------


@cli.group()
def issue() -> None:
    """The issue log: file observations, then promote or link them to tasks.

    Optional and off by default. The board owner turns it on by adding
    "issues": {"enabled": true} to .lattice/config.json. Local boards only.
    """


@issue.command("file")
@click.argument("text")
@click.option(
    "--confidence",
    type=click.Choice(["possible", "definite"]),
    default=None,
    help="How sure the filer is.",
)
@click.option("--evidence", multiple=True, help="A path or URL backing it up (repeatable).")
@click.option("--source", default=None, help="Where it came from (e.g., tester-round-8).")
@common_options
def issue_file(
    text: str,
    confidence: str | None,
    evidence: tuple[str, ...],
    source: str | None,
    output_json: bool,
    quiet: bool,
    session: str | None,
    model: str | None,
    triggered_by: str | None,
    on_behalf_of: str | None,
    provenance_reason: str | None,
) -> None:
    """File an issue. TEXT is the observation; '-' reads it from stdin."""
    is_json = output_json
    checked = _require_issue_log(is_json)
    if text == "-":
        text = _read_stdin_text()
    _lattice_dir, result = _write(
        "issue.file",
        {
            "text": text,
            "confidence": confidence,
            "evidence": evidence,
            "source": source,
            **provenance_params(model, session, triggered_by, on_behalf_of, provenance_reason),
        },
        is_json,
        checked,
    )
    from lattice.core.issues import first_line

    view = result.value
    output_result(
        data=view,
        human_message=f"Filed {_name(view)}: {first_line(view['text'])}",
        quiet_value=_name(view),
        is_json=is_json,
        is_quiet=quiet,
    )


@issue.command("list")
@click.option(
    "--state",
    "states",
    multiple=True,
    type=click.Choice(["open", "linked", "resolved", "dismissed", "duplicate"]),
    help="Show only issues in this state (repeatable). Default: open and linked.",
)
@click.option("--all", "show_all", is_flag=True, help="Show every issue, closed ones too.")
@click.option("--json", "output_json", is_flag=True, help="Output structured JSON.")
def issue_list(states: tuple[str, ...], show_all: bool, output_json: bool) -> None:
    """List issues: open, then linked, each oldest first."""
    from lattice.core.issues import DEFAULT_LIST_STATES, ISSUE_STATES, format_issue_row, id_width
    from lattice.storage.issues import issue_views, list_issue_snapshots

    is_json = output_json
    lattice_dir, _config = _require_issue_log(is_json)
    snapshots = list_issue_snapshots(lattice_dir, on_unreadable=_warn_unreadable)
    views = issue_views(lattice_dir, snapshots)
    wanted = ISSUE_STATES if show_all else (states or DEFAULT_LIST_STATES)
    order = {state: i for i, state in enumerate(ISSUE_STATES)}
    shown = sorted(
        (v for v in views if v["state"] in wanted),
        key=lambda v: (order[v["state"]], v.get("seq") or 0),
    )
    if is_json:
        click.echo(json_envelope(True, data=shown))
        return
    width = id_width(shown)
    for view in shown:
        click.echo(format_issue_row(view, width))
    counts = {state: sum(1 for v in shown if v["state"] == state) for state in ISSUE_STATES}
    summary = ", ".join(f"{counts[s]} {s}" for s in ISSUE_STATES if s in wanted)
    footer = f"{len(shown)} issue{'s' if len(shown) != 1 else ''} ({summary})"
    hidden = len(views) - len(shown)
    if hidden and not show_all:
        footer += f"; {hidden} other{'s' if hidden != 1 else ''} hidden (--all to show)"
    click.echo(footer)


@issue.command("show")
@click.argument("issue_id")
@click.option("--json", "output_json", is_flag=True, help="Output structured JSON.")
def issue_show(issue_id: str, output_json: bool) -> None:
    """Show one issue: its text, evidence, state, linked tasks and history."""
    from lattice.core.events import get_actor_display
    from lattice.core.issues import format_task_link_line, id_width, task_status
    from lattice.storage.issues import (
        issue_views,
        read_issue_events,
        read_issue_snapshot,
        resolve_issue,
    )

    is_json = output_json
    lattice_dir, _config = _require_issue_log(is_json)
    try:
        resolved = resolve_issue(lattice_dir, issue_id)
    except OpError as exc:
        output_error(exc.message, exc.code, is_json)
    snapshot = read_issue_snapshot(lattice_dir, resolved, on_unreadable=_warn_unreadable)
    if snapshot is None:
        output_error(f"Issue '{issue_id}' not found.", "NOT_FOUND", is_json)
    view = issue_views(lattice_dir, [snapshot])[0]
    events = read_issue_events(lattice_dir, resolved)
    if is_json:
        click.echo(json_envelope(True, data={**view, "events": events}))
        return

    click.echo(f"{_name(view)} ({view['id']})  {view['state']}")
    click.echo(f"Filed: {view['filed_at']} by {get_actor_display(view['filed_by'] or '?')}")
    if view.get("confidence"):
        click.echo(f"Confidence: {view['confidence']}")
    if view.get("source"):
        click.echo(f"Source: {view['source']}")
    click.echo("")
    click.echo("Text:")
    for line in view["text"].splitlines() or [""]:
        click.echo(f"  {line}")
    if view["evidence"]:
        click.echo("")
        click.echo("Evidence:")
        for item in view["evidence"]:
            click.echo(f"  {item}")
    if view["tasks"]:
        click.echo("")
        click.echo("Tasks:")
        widths = (
            id_width(view["tasks"]),
            max(len(task_status(t)) for t in view["tasks"]),
        )
        for task in view["tasks"]:
            by = get_actor_display(task.get("linked_by") or "?")
            line = format_task_link_line(task, *widths)
            click.echo(f"  {line}  (linked {task.get('linked_at')} by {by})")
    closure = view.get("closure")
    if closure:
        click.echo("")
        by = get_actor_display(closure.get("by") or "?")
        if closure["kind"] == "duplicate":
            original = read_issue_snapshot(lattice_dir, closure["duplicate_of"]) or {}
            what = f"duplicate of {original.get('short_id') or closure['duplicate_of']}"
        else:
            what = f"dismissed: {closure.get('reason')}"
        click.echo(f"Closed: {what} ({closure.get('at')} by {by})")
    click.echo("")
    click.echo("History:")
    for event in events:
        click.echo(
            f"  {event.get('ts')}  {event.get('type')}  {get_actor_display(event['actor'])}"
        )


@issue.command("promote")
@click.argument("issue_ids", nargs=-1, required=True)
@click.option("--title", default=None, help="The task's title (default: the first issue's text).")
@click.option("--priority", default=None, help="The task's priority.")
@click.option("--type", "task_type", default=None, help="The task's type.")
@common_options
def issue_promote(
    issue_ids: tuple[str, ...],
    title: str | None,
    priority: str | None,
    task_type: str | None,
    output_json: bool,
    quiet: bool,
    session: str | None,
    model: str | None,
    triggered_by: str | None,
    on_behalf_of: str | None,
    provenance_reason: str | None,
) -> None:
    """Create one backlog task from one or more issues and link them to it."""
    is_json = output_json
    _lattice_dir, result = _write(
        "issue.promote",
        {
            "issues": issue_ids,
            "title": title,
            "priority": priority,
            "type": task_type,
            **provenance_params(model, session, triggered_by, on_behalf_of, provenance_reason),
        },
        is_json,
    )
    task = result.value["task"]
    task_name = task.get("short_id") or task["id"]
    names = ", ".join(_name(v) for v in result.value["issues"])
    output_result(
        data=result.value,
        human_message=f'Created {task_name} "{task["title"]}" from {names}',
        quiet_value=task_name,
        is_json=is_json,
        is_quiet=quiet,
    )


def _issue_task_command(op_name: str, verb: str):  # noqa: ANN202
    """``issue link`` and ``issue unlink``: ISSUE_ID TASK_ID and the write options."""

    @click.argument("issue_id")
    @click.argument("task_id")
    @common_options
    def command(
        issue_id: str,
        task_id: str,
        output_json: bool,
        quiet: bool,
        session: str | None,
        model: str | None,
        triggered_by: str | None,
        on_behalf_of: str | None,
        provenance_reason: str | None,
    ) -> None:
        from lattice.core.issues import task_label

        is_json = output_json
        _lattice_dir, result = _write(
            op_name,
            {
                "issue": issue_id,
                "task": task_id,
                **provenance_params(model, session, triggered_by, on_behalf_of, provenance_reason),
            },
            is_json,
        )
        view = result.value
        if op_name == "issue.link":
            entry = _task_entry(view, task_id)
            label = task_label(entry) if entry else task_id
            message = (
                f"{_name(view)} is already linked to {label}"
                if result.idempotent
                else f"Linked {_name(view)} to {label}"
            )
        else:
            message = (
                f"{_name(view)} is not linked to {task_id}"
                if result.idempotent
                else f"Unlinked {_name(view)} from {task_id}"
            )
        output_result(
            data=view,
            human_message=message,
            quiet_value=_name(view),
            is_json=is_json,
            is_quiet=quiet,
        )

    command.__doc__ = verb
    return command


issue.command("link")(
    _issue_task_command("issue.link", "Link an issue to a task (active or archived).")
)
issue.command("unlink")(_issue_task_command("issue.unlink", "Remove an issue's link to a task."))


def _closing_command(op_name: str, doc: str, *, of: bool = False):  # noqa: ANN202
    """``issue dismiss``, ``issue duplicate`` and ``issue reopen``."""

    def body(
        issue_id: str,
        of_id: str | None,
        output_json: bool,
        quiet: bool,
        session: str | None,
        model: str | None,
        triggered_by: str | None,
        on_behalf_of: str | None,
        provenance_reason: str | None,
    ) -> None:
        is_json = output_json
        params = {
            "issue": issue_id,
            **provenance_params(model, session, triggered_by, on_behalf_of, provenance_reason),
        }
        if of:
            params["of"] = of_id
        lattice_dir, result = _write(op_name, params, is_json)
        view = result.value
        closure = view.get("closure") or {}
        if op_name == "issue.dismiss":
            message = f"Dismissed {_name(view)}: {closure.get('reason')}"
        elif op_name == "issue.duplicate":
            from lattice.storage.issues import read_issue_snapshot

            original = read_issue_snapshot(lattice_dir, closure["duplicate_of"])
            original_name = (original or {}).get("short_id") or closure["duplicate_of"]
            message = f"Marked {_name(view)} as a duplicate of {original_name}"
        else:
            message = f"Reopened {_name(view)}"
        output_result(
            data=view,
            human_message=message,
            quiet_value=_name(view),
            is_json=is_json,
            is_quiet=quiet,
        )

    if of:

        @click.argument("issue_id")
        @click.option("--of", "of_id", required=True, help="The issue this one repeats.")
        @common_options
        def command(issue_id: str, of_id: str, **kwargs) -> None:  # noqa: ANN003
            body(issue_id, of_id, **kwargs)

    else:

        @click.argument("issue_id")
        @common_options
        def command(issue_id: str, **kwargs) -> None:  # noqa: ANN003
            body(issue_id, None, **kwargs)

    command.__doc__ = doc
    return command


issue.command("dismiss")(
    _closing_command("issue.dismiss", "Close an issue as not worth acting on. Requires --reason.")
)
issue.command("duplicate")(
    _closing_command("issue.duplicate", "Close an issue as a duplicate of another.", of=True)
)
issue.command("reopen")(_closing_command("issue.reopen", "Reopen a dismissed or duplicate issue."))
