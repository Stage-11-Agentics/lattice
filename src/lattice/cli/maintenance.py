"""``--offline-maintenance`` for the local-only maintenance commands (SPEC §3.5).

``init``, ``demo init``, ``rebuild``, ``doctor --fix``, ``backfill-ids``, and
``migrate needs-human`` write a board directly. On a server host they may run
against a project's board only while no server holds it, and only with this
flag: it takes the owner flock for the command's duration (refused while any
process holds it) and records ``hosted/maintenance.json``, so the project's
next load rotates the epoch and every cache resyncs.
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


def enter_offline_maintenance(lattice_dir: Path, command: str, is_json: bool) -> None:
    """Hold offline maintenance on *lattice_dir* until the current command ends."""
    from lattice.storage.ownership import offline_maintenance

    try:
        click.get_current_context().with_resource(offline_maintenance(lattice_dir, command))
    except OpError as exc:
        output_error(exc.message, exc.code, is_json)
