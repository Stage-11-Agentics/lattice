"""``lattice dashboard`` and ``lattice restart`` commands."""

from __future__ import annotations

import contextlib
import errno
import http.client
import json
import os
import signal
import shutil
import socket
import subprocess
import sys
import time

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
_RESTART_TIMEOUT_SECONDS = 5.0
_RESTART_POLL_INTERVAL_SECONDS = 0.01
_BOOT_ID_REQUEST_TIMEOUT_SECONDS = 0.25

_LOOPBACK_HOSTS = frozenset({"127.0.0.1", "::1", "localhost"})

# Module-level state for SIGHUP restart coordination.
_restart_requested = False
_active_server = None
_RESTART_ENV = "LATTICE_DASHBOARD_RESTART"


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
        restart = _serve(lattice_dir, host, port, readonly, output_json, target)

    if restart:
        os.environ[_RESTART_ENV] = "1"
        script = shutil.which(sys.argv[0]) or sys.argv[0]
        if hasattr(signal, "SIGHUP"):
            # exec resets caught handlers to their defaults. Ignore a second
            # restart request until the new dashboard command installs its handler.
            signal.signal(signal.SIGHUP, signal.SIG_IGN)
        try:
            os.execv(sys.executable, [sys.executable, script, *sys.argv[1:]])
        except OSError as exc:
            click.echo(f"Error: could not restart dashboard process: {exc}", err=True)
            raise SystemExit(1) from exc


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

    first_start = os.environ.pop(_RESTART_ENV, None) != "1"

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
        return True

    return False


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

    pids = _listening_pids(port)

    if not pids:
        click.echo(f"No process found on port {port}.", err=True)
        raise SystemExit(1)

    previous_boot_id = _read_dashboard_boot_id(port)
    if previous_boot_id is None:
        click.echo(
            f"Error: dashboard on port {port} did not provide a boot identity; "
            "restart was not requested.",
            err=True,
        )
        raise SystemExit(1)

    for pid in pids:
        try:
            os.kill(int(pid), signal.SIGHUP)
        except OSError as exc:
            click.echo(f"Error: could not signal dashboard PID {pid}: {exc}", err=True)
            raise SystemExit(1) from exc

    if not _wait_for_dashboard_restart(port, previous_boot_id):
        click.echo(
            f"Error: dashboard on port {port} did not restart within "
            f"{_RESTART_TIMEOUT_SECONDS:g} seconds (boot identity unchanged).",
            err=True,
        )
        raise SystemExit(1)

    click.echo(f"Dashboard restarted and is listening on port {port}.")


def _listening_pids(port: int) -> list[str]:
    """Return process IDs that own a listening socket on *port*."""
    result = subprocess.run(
        ["lsof", "-ti", f"tcp:{port}", "-sTCP:LISTEN"],
        capture_output=True,
        text=True,
    )
    return sorted(set(pid.strip() for pid in result.stdout.splitlines() if pid.strip()))


def _read_dashboard_boot_id(
    port: int, *, timeout: float = _BOOT_ID_REQUEST_TIMEOUT_SECONDS
) -> str | None:
    """Read the live dashboard's uncached boot identity, if it is responding."""
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=timeout)
    try:
        connection.request("GET", "/api/boot")
        response = connection.getresponse()
        body = response.read()
        if response.status != 200:
            return None
        payload = json.loads(body)
        if not isinstance(payload, dict) or not isinstance(payload.get("data"), dict):
            return None
        boot_id = payload["data"].get("boot_id")
        return boot_id if isinstance(boot_id, str) and boot_id else None
    except (http.client.HTTPException, OSError, TypeError, ValueError):
        return None
    finally:
        connection.close()


def _wait_for_dashboard_restart(port: int, previous_boot_id: str) -> bool:
    """Wait for a responding dashboard on *port* to expose a different boot ID."""
    deadline = time.monotonic() + _RESTART_TIMEOUT_SECONDS
    while time.monotonic() < deadline:
        remaining = deadline - time.monotonic()
        boot_id = _read_dashboard_boot_id(
            port, timeout=min(_BOOT_ID_REQUEST_TIMEOUT_SECONDS, max(remaining, 0.001))
        )
        if boot_id is not None and boot_id != previous_boot_id:
            return True
        time.sleep(min(_RESTART_POLL_INTERVAL_SECONDS, max(deadline - time.monotonic(), 0)))
    return False
