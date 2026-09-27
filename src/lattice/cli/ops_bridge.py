"""Run a named operation from a Click command and render its errors as today."""

from __future__ import annotations

from typing import Any

import click

from lattice.cli.helpers import output_error
from lattice.ops import Caller, OpError, OpResult


def caller_from_context(**overrides: Any) -> Caller:
    """The ``Caller`` for this command: its ``--actor`` / ``--name`` flags."""
    ctx = click.get_current_context()
    ctx.ensure_object(dict)
    fields: dict[str, Any] = {
        "actor": ctx.obj.get("_actor"),
        "actor_name": ctx.obj.get("_session_name"),
    }
    fields.update(overrides)
    return Caller(**fields)


def run_operation(
    op_name: str, params: dict, is_json: bool, *, caller: Caller | None = None
) -> OpResult:
    """Run *op_name* on the board the cwd belongs to.

    An ``OpError`` is printed exactly as ``output_error`` always has (code and
    message; exit code 1).
    """
    from lattice.boards import resolve_board

    try:
        board = resolve_board()
        return board.execute(
            op_name, params, caller if caller is not None else caller_from_context()
        )
    except OpError as exc:
        output_error(exc.message, exc.code, is_json)
