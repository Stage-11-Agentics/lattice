"""``lattice cache clear``: delete a hosted checkout's read-only cache (SPEC §9.4).

A cache's directories are 0500, so ``rm -rf`` and ``git clean -xdf`` fail on
it; this command restores write modes and deletes it. It keeps
``cache/rescued/`` (naming it on stderr) and, unless ``--forget``, a routing
marker, so the checkout stays routed to its server and the next command
resyncs from scratch. On a checkout that is not a hosted cache it fails with
``NOT_HOSTED`` and deletes nothing, so it can never delete a local board.
"""

from __future__ import annotations

from pathlib import Path

import click

from lattice.cli.helpers import json_envelope, output_error
from lattice.cli.main import cli
from lattice.core.errors import OpError


def _hosted_root() -> Path | None:
    """The checkout whose cache this command acts on: ``find_root``'s order
    (``LATTICE_ROOT``, the linked-worktree jump, walking up)."""
    from lattice.storage.fs import LatticeRootError, find_root

    try:
        return find_root()
    except LatticeRootError:
        return None


@cli.group()
def cache() -> None:
    """Manage the read-only cache of a hosted checkout."""


@cache.command("clear")
@click.option(
    "--forget",
    is_flag=True,
    help="Also remove the routing marker (rebinding to another project, or moving back to local).",
)
@click.option("--json", "is_json", is_flag=True, help="Output as JSON.")
def clear(forget: bool, is_json: bool) -> None:
    """Delete the cache; the next command resyncs it from the server."""
    from lattice.remote.cache import clear_cache, not_hosted

    root = _hosted_root()
    try:
        if root is None:
            raise not_hosted(Path.cwd())
        result = clear_cache(root, forget=forget)
    except OpError as exc:
        output_error(exc.message, exc.code, is_json)
    if result.rescued is not None:
        click.echo(f"lattice: kept rescued board files in {result.rescued}", err=True)
    data = {
        "root": str(result.root),
        "remote": result.remote,
        "project": result.project,
        "forgot": result.forgot,
        "kept": [str(result.rescued)] if result.rescued is not None else [],
    }
    if is_json:
        click.echo(json_envelope(True, data=data))
    elif forget:
        click.echo(
            f"Cleared the cache at {result.root} and forgot {result.remote}/{result.project}."
        )
    else:
        click.echo(
            f"Cleared the cache of {result.remote}/{result.project} at {result.root}; "
            "the next command resyncs it."
        )
