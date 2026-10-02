"""``lattice dashboard`` and ``lattice restart`` commands."""

from __future__ import annotations

import contextlib
import errno
import os
import signal
import socket
import subprocess
import sys

import click

from lattice.cli.helpers import (
    end_read_phase,
    json_envelope,
    json_error_obj,
    load_project_config,
    output_error,
    require_root,
)
from lattice.core.errors import OpError
from lattice.cli.main import cli

_DEFAULT_PORT = 8799

_LOOPBACK_HOSTS = frozenset({"127.0.0.1", "::1", "localhost"})

# Module-level state for SIGHUP restart coordination.
_restart_requested = False
_active_server = None


def _handle_sighup(signum, frame):  # noqa: ARG001
    """Handle SIGHUP by requesting a graceful restart of serve_forever()."""
    global _restart_requested
    _restart_requested = True
    if _active_server is not None:
        # Signal-handler safe: set the internal flag directly.
        # server.shutdown() would deadlock here because it waits for
        # serve_forever() to exit, but we're in the same thread.
        _active_server._BaseServer__shutdown_request = True


def _find_free_port(host: str, near: int) -> int | None:
    """Return an available port close to *near*, or ``None`` on failure."""
    for candidate in range(near + 1, near + 20):
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
                s.bind((host, candidate))
                return candidate
        except OSError:
            continue
    return None


@cli.command("dashboard")
@click.option("--host", default="127.0.0.1", help="Host to bind to.")
@click.option(
    "--port",
    default=None,
    type=int,
    help="Port to bind to. Defaults to dashboard_port in config, or 8799.",
)
@click.option("--json", "output_json", is_flag=True, help="Output structured JSON.")
def dashboard_cmd(host: str, port: int | None, output_json: bool) -> None:
    """Launch a read-only local web dashboard.

    Supports graceful restart via SIGHUP — the server shuts down and
    relaunches on the same port without losing the terminal session.
    Use ``lattice restart`` to send the signal from another terminal.

    On a checkout bound to a server, it reads the checkout's cache, kept caught
    up by an embedded follower, and writes to the server as the browser actor.
    """
    lattice_dir = require_root(output_json)

    # Resolve port: CLI flag > config.dashboard_port > 8799
    if port is None:
        config = load_project_config(lattice_dir)
        port = config.get("dashboard_port", _DEFAULT_PORT)

    # Non-loopback binds are forced into read-only mode
    readonly = host not in _LOOPBACK_HOSTS
    if readonly:
        click.echo(
            "Warning: dashboard is exposed on the network — writes are disabled. "
            "Bind to 127.0.0.1 for local-only access with full write support.",
            err=True,
        )

    # Register SIGHUP handler for graceful restart (Unix only)
    if hasattr(signal, "SIGHUP"):
        signal.signal(signal.SIGHUP, _handle_sighup)

    with contextlib.ExitStack() as stack:
        target = _dashboard_target(lattice_dir, stack, output_json)
        _serve(lattice_dir, host, port, readonly, output_json, target)


def _dashboard_target(lattice_dir, stack, is_json):  # noqa: ANN001, ANN202
    """The board to serve: ``None`` for a local board (the server builds its own),
    or, on a bound checkout, the server's board with an embedded follower that
    runs until *stack* closes (SPEC §9.6). SIGTERM then stops the dashboard
    cleanly, so the follower clears its freshness record on the way out."""
    from lattice.cli.helpers import hosted_or_exit

    root = lattice_dir.parent
    hosted = hosted_or_exit(root, is_json)
    if hosted is None:
        return None
    from lattice.boards import resolve_board
    from lattice.dashboard.bound import bound_dashboard

    try:
        target = stack.enter_context(bound_dashboard(resolve_board(root)))
    except OpError as exc:
        output_error(exc.message, exc.code, is_json)

    def _terminate(signum, frame):  # noqa: ANN001, ANN202, ARG001
        raise KeyboardInterrupt

    previous = signal.signal(signal.SIGTERM, _terminate)
    stack.callback(signal.signal, signal.SIGTERM, previous)
    return target


def _serve(lattice_dir, host, port, readonly, output_json, target):  # noqa: ANN001, ANN202, PLR0913
    """Serve until stopped, restarting in place on SIGHUP."""
    global _active_server, _restart_requested

    from lattice.dashboard.server import create_server

    first_start = True

    while True:
        _restart_requested = False

        try:
            server = create_server(lattice_dir, host, port, readonly=readonly, board=target)
        except OSError as exc:
            if exc.errno == errno.EADDRINUSE:
                alt = _find_free_port(host, port)
                hint = (
                    f"  lattice dashboard --port {alt}"
                    if alt
                    else "  lattice dashboard --port <PORT>"
                )
                msg = (
                    f"Port {port} is already in use — is another dashboard running?\n"
                    f"You can stop the other process, or start on a free port:\n\n"
                    f"{hint}"
                )
                code = "PORT_IN_USE"
            else:
                msg = str(exc)
                code = "BIND_ERROR"
            if output_json:
                click.echo(json_envelope(False, error=json_error_obj(code, msg)))
            else:
                click.echo(f"Error: {msg}", err=True)
            raise SystemExit(1)

        _active_server = server
        url = f"http://{host}:{port}/"

        if first_start:
            if output_json:
                click.echo(json_envelope(True, data={"host": host, "port": port, "url": url}))
            else:
                click.echo(f"Lattice dashboard: {url}")
                click.echo("Press Ctrl+C to stop.")
                import webbrowser

                try:
                    webbrowser.open(url)
                except Exception:
                    pass
            first_start = False
        else:
            click.echo(f"Lattice dashboard restarted: {url}", err=True)

        try:
            server.serve_forever()
        except KeyboardInterrupt:
            server.server_close()
            _active_server = None
            sys.exit(0)

        server.server_close()
        _active_server = None

        if not _restart_requested:
            break

        click.echo("Restarting dashboard...", err=True)


@cli.command("restart")
@click.option(
    "--port",
    default=None,
    type=int,
    help="Port of the dashboard to restart. Defaults to dashboard_port in config, or 8799.",
)
def restart_cmd(port: int | None) -> None:
    """Send a restart signal to a running Lattice dashboard.

    Finds the process listening on the given port and sends SIGHUP,
    causing the dashboard to gracefully restart in place.
    """
    if port is None:
        lattice_dir = require_root(False)
        config = load_project_config(lattice_dir)
        end_read_phase(lattice_dir)  # before lsof (SPEC §9.4)
        port = config.get("dashboard_port", _DEFAULT_PORT)

    if not hasattr(signal, "SIGHUP"):
        click.echo("Error: restart via signal is not supported on this platform.", err=True)
        raise SystemExit(1)

    result = subprocess.run(
        ["lsof", "-ti", f"tcp:{port}", "-sTCP:LISTEN"],
        capture_output=True,
        text=True,
    )
    pids = sorted(set(p.strip() for p in result.stdout.strip().split("\n") if p.strip()))

    if not pids:
        click.echo(f"No process found on port {port}.", err=True)
        raise SystemExit(1)

    for pid in pids:
        os.kill(int(pid), signal.SIGHUP)

    click.echo(f"Restart signal sent to dashboard on port {port}.")
