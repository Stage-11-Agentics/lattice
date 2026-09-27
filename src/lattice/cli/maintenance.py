"""``--offline-maintenance`` for the local-only maintenance commands (SPEC §3.5).

``init``, ``demo init``, ``rebuild``, ``doctor --fix``, ``backfill-ids``, and
``migrate needs-human`` write a board directly. Each calls
:func:`maintenance_gate` before it does anything else:

- On a server-owned board (``hosted/owner.json``) the command runs only with
  ``--offline-maintenance``; without it, it is refused with ``BOARD_IS_HOSTED``
  even when it would change nothing (a dry run, an already-applied migration).
- The flag takes the owner flock for the command's duration (refused while any
  process holds it) and records ``hosted/maintenance.json``, so the project's
  next load rotates the epoch and every cache resyncs. It applies only to a
  server project's board (one with ``hosted/``).
- On a client cache the command is refused with ``BOARD_IS_CACHE``.
- ``init`` and ``demo init`` initialize a *hosted scaffold* (a server project's
  ``.lattice/`` holding ``hosted/`` but no ``config.json`` yet) under the flag;
  on an initialized board they behave as always (``init`` reports the board is
  already initialized, ``demo init`` refuses to overwrite it).

Read-only ``doctor`` (no ``--fix``) is not gated.

Before any of that, and before the command reads anything, a hosted checkout
refuses them with ``LOCAL_ONLY`` (:func:`refuse_on_hosted_checkout`): their
board lives on the server, and the message names the server-side procedure.
"""

from __future__ import annotations

from pathlib import Path

import click

from lattice.cli.helpers import output_error
from lattice.core.errors import OpError

offline_maintenance_option = click.option(
    "--offline-maintenance",
    "offline_maintenance",
    is_flag=True,
    help="Server host only: run on a project's board while no server holds it "
    "(takes the owner lock; records hosted/maintenance.json).",
)


def maintenance_gate(
    lattice_dir: Path, command: str, is_json: bool, offline_maintenance: bool
) -> None:
    """Admit a maintenance command to *lattice_dir*, or exit with its error.

    With *offline_maintenance*, holds offline maintenance until the current
    command ends. Then refuses a board this command may not write (a cache, or
    a server-owned board without the flag). A board that does not exist yet is
    not gated (``init``, ``demo init``).
    """
    from lattice.storage.ownership import check_board_writable
    from lattice.storage.ownership import offline_maintenance as maintenance

    try:
        if offline_maintenance:
            click.get_current_context().with_resource(maintenance(lattice_dir, command))
        if lattice_dir.is_dir():
            check_board_writable(lattice_dir)
    except OpError as exc:
        output_error(exc.message, exc.code, is_json)


def refuse_on_hosted_checkout(command: str, is_json: bool, start: Path | None = None) -> None:
    """Exit with ``LOCAL_ONLY`` when *start* (default: the cwd) is a hosted
    checkout (SPEC §3.5); ``init`` gets its own message."""
    from lattice.boards import check_local_only

    try:
        check_local_only(command, start)
    except OpError as exc:
        output_error(exc.message, exc.code, is_json)
