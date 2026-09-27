"""Prose commands (SPEC §3.9): plan and notes, the board's context, workspace files.

``lattice plan <task>`` is still the plan read it has always been; ``plan``
became a group, and a first argument that is not a subcommand is that read.
Writes go through operations, so they work on every kind of board:

- ``lattice plan write <task>`` / ``lattice notes write <task>``
- ``lattice context write``
- ``lattice board write <path>``

Each takes its content from ``--file PATH`` or ``--stdin``. The arguments are
checked before anything is read, and a file is read only once they pass.
"""

from __future__ import annotations

from pathlib import Path

import click

from lattice.cli.helpers import (
    common_options,
    json_envelope,
    output_error,
    output_result,
    require_root,
    resolve_task_id,
)
from lattice.cli.main import cli
from lattice.cli.ops_bridge import board_or_exit, check_or_exit, provenance_params, run_operation
from lattice.ops.board_file_write import check_board_path
from lattice.ops.prose_common import check_content_sources, check_expect_sha256
from lattice.storage.fs import LATTICE_DIR
from lattice.storage.operations import resolve_task_prose_path


def _read_content(file_path: str | None, use_stdin: bool, is_json: bool, what: str) -> dict:
    """``{"file": text}`` or ``{"stdin": text}``, after the argument checks.

    The bytes are decoded as UTF-8 exactly (no newline translation), so the
    written file is byte-identical to its source.
    """
    check_or_exit(is_json, check_content_sources, file_path is not None, use_stdin, what)
    if file_path is not None:
        try:
            data = Path(file_path).read_bytes()
        except OSError as exc:
            output_error(
                f"Cannot read --file '{file_path}': {exc.strerror or exc}.",
                "VALIDATION_ERROR",
                is_json,
            )
        source, label = "file", f"--file '{file_path}'"
    else:
        data = click.get_binary_stream("stdin").read()
        source, label = "stdin", "--stdin"
    try:
        return {source: data.decode("utf-8")}
    except UnicodeDecodeError:
        output_error(f"{label} is not UTF-8 text.", "VALIDATION_ERROR", is_json)


def _content_options(f):  # noqa: ANN001, ANN202
    f = click.option(
        "--stdin", "use_stdin", is_flag=True, help="Read the content from standard input."
    )(f)
    f = click.option("--file", "file_path", default=None, help="Read the content from a file.")(f)
    return f


def _expect_option(f):  # noqa: ANN001, ANN202
    return click.option(
        "--expect-sha256",
        default=None,
        help="Refuse (CONFLICT) unless the current file has this SHA-256.",
    )(f)


# ---------------------------------------------------------------------------
# lattice plan (group with the legacy read)
# ---------------------------------------------------------------------------


class _LegacyReadGroup(click.Group):
    """A group whose first argument, when not a subcommand, is the legacy read.

    ``lattice plan LAT-5 --json`` runs the read exactly as the old ``plan``
    command did (usage and errors included); ``lattice plan write ...`` runs
    the subcommand. ``--help`` alone shows the group.
    """

    def __init__(self, *args, legacy: click.Command, **kwargs) -> None:  # noqa: ANN002, ANN003
        super().__init__(*args, **kwargs)
        self.legacy = legacy

    def parse_args(self, ctx: click.Context, args: list[str]) -> list[str]:
        if args and (args[0] in self.commands or args[0] in ctx.help_option_names):
            return super().parse_args(ctx, args)
        ctx.meta["lattice.legacy_read_args"] = list(args)
        return []

    def invoke(self, ctx: click.Context):  # noqa: ANN201
        legacy_args = ctx.meta.pop("lattice.legacy_read_args", None)
        if legacy_args is None:
            return super().invoke(ctx)
        with self.legacy.make_context(ctx.info_name, legacy_args, parent=ctx.parent) as sub:
            return self.legacy.invoke(sub)


@click.command()
@click.argument("task_id")
@click.option("--json", "output_json", is_flag=True, help="Output as JSON.")
def _plan_read(task_id: str, output_json: bool) -> None:
    """Show the plan file for a task."""
    is_json = output_json
    lattice_dir = require_root(is_json)
    task_id = resolve_task_id(lattice_dir, task_id, is_json)

    plan_path, authority = resolve_task_prose_path(lattice_dir, task_id, "plan")
    is_archived = authority.location == "archived"
    if plan_path is None:
        output_error(f"No plan file found for task {task_id}.", "NOT_FOUND", is_json)

    if is_json:
        data = {
            "task_id": task_id,
            "plan_path": str(plan_path),
            "archived": is_archived,
            "content": plan_path.read_text(encoding="utf-8"),
        }
        click.echo(json_envelope(True, data=data))
    else:
        # Print content to stdout
        click.echo(plan_path.read_text(encoding="utf-8"))


@cli.group(cls=_LegacyReadGroup, legacy=_plan_read, invoke_without_command=True)
def plan() -> None:
    """Show a task's plan (lattice plan TASK_ID [--json]) or write it (plan write)."""


@cli.group()
def notes() -> None:
    """Write a task's notes."""


def _prose_write(
    kind: str,
    task_id: str,
    file_path: str | None,
    use_stdin: bool,
    expect_sha256: str | None,
    provenance: dict,
    is_json: bool,
    quiet: bool,
) -> None:
    check_or_exit(is_json, check_content_sources, file_path is not None, use_stdin, f"the {kind}")
    check_or_exit(is_json, check_expect_sha256, expect_sha256)
    content = _read_content(file_path, use_stdin, is_json, f"the {kind}")
    result = run_operation(
        f"task.{kind}_write",
        {"task": task_id, "expect_sha256": expect_sha256, **content, **provenance},
        is_json,
    )
    value = result.value
    display_id = result.task.get("short_id") or value["task_id"]
    label = "Plan" if kind == "plan" else "Notes"
    message = (
        f"{label} unchanged for {display_id} ({value['path']})"
        if result.idempotent
        else f"{label} written for {display_id}: {value['path']} ({value['bytes']} bytes)"
    )
    output_result(
        data=value, human_message=message, quiet_value="ok", is_json=is_json, is_quiet=quiet
    )


def _write_command(kind: str):  # noqa: ANN202
    @click.argument("task_id")
    @_content_options
    @_expect_option
    @common_options
    def write(
        task_id: str,
        file_path: str | None,
        use_stdin: bool,
        expect_sha256: str | None,
        model: str | None,
        session: str | None,
        output_json: bool,
        quiet: bool,
        triggered_by: str | None,
        on_behalf_of: str | None,
        provenance_reason: str | None,
    ) -> None:
        _prose_write(
            kind,
            task_id,
            file_path,
            use_stdin,
            expect_sha256,
            provenance_params(model, session, triggered_by, on_behalf_of, provenance_reason),
            output_json,
            quiet,
        )

    write.__doc__ = (
        f"Replace a task's {kind} with the content of --file PATH or --stdin.\n\n"
        f"Works on every board, including a hosted cache where the {kind} file is "
        "read-only. Appends a "
        f"{kind}_written event recording the content's SHA-256 and size."
    )
    return write


plan.command("write")(_write_command("plan"))
notes.command("write")(_write_command("notes"))


# ---------------------------------------------------------------------------
# lattice context write, lattice board write
# ---------------------------------------------------------------------------


def _render_board_write(result, is_json: bool, quiet: bool) -> None:  # noqa: ANN001
    value = result.value
    message = (
        f"Unchanged .lattice/{value['path']}"
        if result.idempotent
        else f"Wrote .lattice/{value['path']} ({value['bytes']} bytes)"
    )
    output_result(
        data=value, human_message=message, quiet_value="ok", is_json=is_json, is_quiet=quiet
    )


@cli.group()
def context() -> None:
    """Write the board's context.md."""


@context.command("write")
@_content_options
@click.option("--json", "output_json", is_flag=True, help="Output structured JSON.")
@click.option("--quiet", is_flag=True, help="Print only 'ok'.")
def context_write(file_path: str | None, use_stdin: bool, output_json: bool, quiet: bool) -> None:
    """Replace .lattice/context.md with the content of --file PATH or --stdin."""
    content = _read_content(file_path, use_stdin, output_json, "the context")
    _render_board_write(
        run_operation("board.context_write", content, output_json), output_json, quiet
    )


@cli.group()
def board() -> None:
    """Write the board's workspace files (orchestration/, loose plans/ and notes/ files)."""


def normalize_board_path(raw: str, lattice_dir: Path | None) -> str:
    """What the user typed, as a path relative to ``.lattice/`` (client-side).

    Accepts ``./x``, ``.lattice/x``, and an absolute path inside this board's
    ``.lattice/``; the operation checks the result against the workspace rules.
    """
    path = raw
    if lattice_dir is not None and Path(raw).is_absolute():
        try:
            path = Path(raw).relative_to(lattice_dir.resolve()).as_posix()
        except ValueError:
            try:
                path = Path(raw).relative_to(lattice_dir).as_posix()
            except ValueError:
                return raw
    if path.startswith("/"):
        return path  # outside this board: the operation refuses it
    parts = [p for p in path.split("/") if p not in ("", ".")]
    if parts[:1] == [LATTICE_DIR]:
        parts = parts[1:]
    return "/".join(parts)


@board.command("write")
@click.argument("path")
@_content_options
@_expect_option
@click.option("--json", "output_json", is_flag=True, help="Output structured JSON.")
@click.option("--quiet", is_flag=True, help="Print only 'ok'.")
def board_write(
    path: str,
    file_path: str | None,
    use_stdin: bool,
    expect_sha256: str | None,
    output_json: bool,
    quiet: bool,
) -> None:
    """Write one workspace file: PATH is relative to .lattice/.

    PATH must be under orchestration/ (any depth; missing directories are
    created) or a loose file directly under plans/ or notes/ that is not a
    task's own <task_id>.md. There is no remove: overwrite a file instead.
    """
    is_json = output_json
    board_obj = board_or_exit(is_json)
    relative = normalize_board_path(path, board_obj.lattice_dir)
    check_or_exit(is_json, check_board_path, relative)
    check_or_exit(is_json, check_content_sources, file_path is not None, use_stdin, "the content")
    check_or_exit(is_json, check_expect_sha256, expect_sha256)
    content = _read_content(file_path, use_stdin, is_json, "the content")
    result = run_operation(
        "board.file_write",
        {"path": relative, "expect_sha256": expect_sha256, **content},
        is_json,
        board=board_obj,
    )
    _render_board_write(result, is_json, quiet)
