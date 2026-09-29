"""``lattice sync [--follow]``: catch a hosted checkout's cache up, or follow it (SPEC §9.6)."""

from __future__ import annotations

import signal
import threading
from pathlib import Path
from typing import TYPE_CHECKING

import click

from lattice.cli.helpers import json_envelope, output_error
from lattice.cli.main import cli
from lattice.core.errors import OpError

if TYPE_CHECKING:
    from lattice.remote.follower import Follower

NOT_HOSTED_MESSAGE = (
    "This checkout is not bound to a Lattice server; 'lattice sync' works only on a "
    "hosted checkout. Bind one with 'lattice remote attach <alias> <project>'."
)

# SPEC §9.5 / §9.4: why a sync did not complete.
_FAILURES = {
    "unreachable": ("SERVER_UNREACHABLE", "Cannot reach {alias}; the cache is as of {synced_at}."),
    "busy": ("BOARD_BUSY", "{alias} is busy; the cache is as of {synced_at}. Try again shortly."),
    "incomplete": (
        "CACHE_INCOMPLETE",
        "The cache was interrupted mid-update and the server is unreachable; "
        "run `lattice sync` when it is back.",
    ),
}


def _hosted_root_or_exit(is_json: bool) -> Path:
    """The hosted root the cwd routes to (SPEC §9.3: ``LATTICE_ROOT``, the
    worktree jump, then walking up), or the routing error, or ``NOT_HOSTED``."""
    from lattice.remote.binding import hosted_root
    from lattice.remote.session import scrub_output

    try:
        hosted = hosted_root(Path.cwd())
    except OpError as exc:
        scrub_output()  # the message quotes the binding (SPEC §4)
        output_error(exc.message, exc.code, is_json)
    if hosted is None:
        output_error(NOT_HOSTED_MESSAGE, "NOT_HOSTED", is_json)
    # From here on this command prints server-supplied text (SPEC §4).
    scrub_output()
    return hosted.root


def _alias(root: Path) -> str:
    from lattice.remote import cache

    identity = cache.cache_identity(root)
    return identity[0] if identity else "the server"


def _sync_once(root: Path, is_json: bool) -> None:
    from lattice.remote import cache
    from lattice.remote.follower import succeeded

    try:
        outcome = cache.catch_up(root, bulk=True)
    except OpError as exc:
        output_error(exc.message, exc.code, is_json)
    if not succeeded(outcome):
        code, template = _FAILURES.get(outcome.kind, _FAILURES["unreachable"])
        message = template.format(alias=_alias(root), synced_at=outcome.synced_at or "never")
        output_error(message, code, is_json)
    data = {"status": outcome.kind, "head_seq": outcome.head_seq, "synced_at": outcome.synced_at}
    if is_json:
        click.echo(json_envelope(True, data=data), nl=False)
    elif outcome.kind == "applied":
        click.echo(f"Synced to seq {outcome.head_seq}.")
    else:
        click.echo(f"Already up to date at seq {outcome.head_seq}.")


def run_foreground(follower: Follower) -> None:
    """Run *follower* in this (the main) thread until SIGTERM, SIGINT, or ``stop``.

    Both signals stop it cleanly: ``stream_live_until`` is cleared on the way out.
    """
    previous: dict[int, object] = {}
    in_main = threading.current_thread() is threading.main_thread()

    def _stop(signum: int, frame: object) -> None:
        follower.stop()

    if in_main:
        for signum in (signal.SIGTERM, signal.SIGINT):
            previous[signum] = signal.signal(signum, _stop)
    try:
        follower.run()
    except KeyboardInterrupt:
        follower.stop()
    finally:
        for signum, handler in previous.items():
            signal.signal(signum, handler)  # type: ignore[arg-type]


@cli.command("sync")
@click.option("--follow", is_flag=True, help="Stay in the foreground and follow the stream.")
@click.option("--json", "output_json", is_flag=True, help="Output structured JSON.")
def sync_cmd(follow: bool, output_json: bool) -> None:
    """Catch a hosted checkout's cache up with its server.

    With --follow, hold the server's change stream and sync on every change
    (polling when the stream is unavailable) until stopped with SIGTERM or
    Ctrl-C. While it runs, reads on this machine skip their own catch-up.
    """
    is_json = output_json
    if follow and is_json:
        output_error("--follow and --json cannot be combined.", "VALIDATION_ERROR", is_json)
    root = _hosted_root_or_exit(is_json)
    from lattice.remote import cache
    from lattice.remote.follower import Follower, follow_target

    if not follow:
        _sync_once(root, is_json)
        return
    try:
        remote, project = follow_target(root)
        follower = Follower(
            root,
            remote,
            project,
            catch_up=cache.catch_up,
            on_notice=lambda line: click.echo(line, err=True),
        )
        click.echo(f"Following {remote.alias}/{project}... (Ctrl-C to stop)", err=True)
        run_foreground(follower)
    except OpError as exc:
        output_error(exc.message, exc.code, is_json)
    click.echo("Stopped.", err=True)
