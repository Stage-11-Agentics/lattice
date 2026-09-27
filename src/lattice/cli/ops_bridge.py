"""Run a named operation from a Click command and render its errors as today."""

from __future__ import annotations

from typing import Any

import click

from lattice.boards import LocalBoard, resolve_board
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


def board_or_exit(is_json: bool) -> LocalBoard:
    """The board the cwd belongs to, or today's ``NOT_INITIALIZED`` error."""
    try:
        return resolve_board()
    except OpError as exc:
        output_error(exc.message, exc.code, is_json)


def run_operation(
    op_name: str,
    params: dict,
    is_json: bool,
    *,
    caller: Caller | None = None,
    board: LocalBoard | None = None,
    config: dict | None = None,
) -> OpResult:
    """Run *op_name* on *board* (default: the board the cwd belongs to).

    Pass *config* when the command runs client-side effects afterwards, so
    they use the same configuration the operation did. An ``OpError`` is
    printed exactly as ``output_error`` always has (code and message; exit 1).
    """
    board = board if board is not None else board_or_exit(is_json)
    try:
        return board.execute(
            op_name,
            params,
            caller if caller is not None else caller_from_context(),
            config=config,
        )
    except OpError as exc:
        output_error(exc.message, exc.code, is_json)
