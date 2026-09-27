"""Run a named operation from a Click command and render its errors as today."""

from __future__ import annotations

from typing import Any

import click

from lattice.boards import HostedBoard, LocalBoard, resolve_board
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


def board_or_exit(is_json: bool) -> LocalBoard | HostedBoard:
    """The board the cwd belongs to, or today's ``NOT_INITIALIZED`` error.

    On a hosted checkout, plain output shows other people's control characters
    as U+FFFD from here on (SPEC §4), whatever the command prints."""
    try:
        board = resolve_board()
    except OpError as exc:
        if exc.code != "NOT_INITIALIZED":  # a routing error about a binding (SPEC §4)
            from lattice.remote.session import scrub_output

            scrub_output()
        output_error(exc.message, exc.code, is_json)
    if isinstance(board, HostedBoard):
        from lattice.remote.session import scrub_output

        scrub_output()
    return board


def is_hosted(board: object) -> bool:
    """Whether *board* is a hosted checkout's board (writes go to a server)."""
    return isinstance(board, HostedBoard)


def run_operation(
    op_name: str,
    params: Any,
    is_json: bool,
    *,
    caller: Caller | None = None,
    board: LocalBoard | HostedBoard | None = None,
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


def params_or_exit(op_name: str, params: dict, is_json: bool) -> Any:
    """Parse and check *op_name*'s params before the board is looked up.

    Commands that validate their arguments before finding the board keep that
    order: an argument error wins over ``NOT_INITIALIZED``, as it always has.
    """
    from lattice.ops import get_operation, parse_params

    try:
        return parse_params(get_operation(op_name).Params, params, op_name=op_name)
    except OpError as exc:
        output_error(exc.message, exc.code, is_json)


def provenance_params(
    model: str | None,
    session: str | None,
    triggered_by: str | None,
    on_behalf_of: str | None,
    reason: str | None,
) -> dict:
    """The ``common_options`` provenance flags as operation params."""
    return {
        "model": model,
        "session": session,
        "triggered_by": triggered_by,
        "on_behalf_of": on_behalf_of,
        "reason": reason,
    }


def check_or_exit(is_json: bool, check: Any, *args: Any) -> None:
    """Run one of an operation's input checks now, so it keeps its place in the
    command's argument order (for example, before a ``--file`` is read)."""
    try:
        check(*args)
    except OpError as exc:
        output_error(exc.message, exc.code, is_json)


def run_attested_operation(
    op_name: str,
    params: Any,
    is_json: bool,
    *,
    board: LocalBoard | HostedBoard,
    attest: Any,
    config: dict | None = None,
) -> OpResult:
    """Run *op_name* with the attestations ``attest()`` computes (SPEC §3.4).

    A stale attestation (``COMPLETION_BLOCKED`` with ``details.reason``
    ``STALE_ATTESTATION``) means the task changed between computing and
    writing: re-sync the board (``board.refresh()``), recompute, and retry
    once, as a new operation call with its own ``op_id``. Any other error
    prints as ``run_operation`` prints it.
    """
    from lattice.core.attestations import STALE_ATTESTATION

    for attempt in range(2):
        if attempt:
            board.refresh()
        caller = caller_from_context(attestations=attest())
        try:
            return board.execute(op_name, params, caller, config=config)
        except OpError as exc:
            stale = exc.code == "COMPLETION_BLOCKED" and (
                exc.details.get("reason") == STALE_ATTESTATION
            )
            if stale and attempt == 0:
                continue
            output_error(exc.message, exc.code, is_json)
    raise AssertionError("unreachable")
