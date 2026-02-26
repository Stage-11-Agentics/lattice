"""``lattice dashboard`` and ``lattice restart`` commands."""

from __future__ import annotations

import errno
import os
import signal
import socket
import subprocess
import sys

import click

from lattice.cli.helpers import json_envelope, json_error_obj, load_project_config, require_root
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
@click.option("--port", default=None, type=int, help="Port to bind to. Defaults to dashboard_port in config, or 8799.")
@click.option("--relay-port", default=9801, type=int, help="WebSocket relay port for browser sync (default: 9801).")
@click.option("--no-relay", is_flag=True, help="Don't host a WebSocket relay (browser can still connect to a remote one via --relay-url).")
@click.option("--relay-url", default=None, help="WebSocket relay URL for browser sync (e.g. ws://localhost:9801). Auto-set when hosting.")
@click.option("--json", "output_json", is_flag=True, help="Output structured JSON.")
def dashboard_cmd(host: str, port: int | None, relay_port: int, no_relay: bool, relay_url: str | None, output_json: bool) -> None:
    """Launch a read-only local web dashboard.

    Supports graceful restart via SIGHUP — the server shuts down and
    relaunches on the same port without losing the terminal session.
    Use ``lattice restart`` to send the signal from another terminal.
    """
    global _active_server, _restart_requested

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

    from lattice.dashboard.server import create_server

    relay = None
    first_start = True

    while True:
        _restart_requested = False

        try:
            server = create_server(lattice_dir, host, port, readonly=readonly)
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

        # Start WebSocket relay on first successful server creation
        if relay is None and not no_relay:
            try:
                from lattice.dashboard.relay import start_relay_thread

                relay = start_relay_thread(host, relay_port)

                import json as _json

                from lattice.storage.bus import register_listener

                def _on_server_write(task_id: str, events: list, snapshot: dict) -> None:  # noqa: ARG001
                    msg = _json.dumps({"type": "server_write", "task_id": task_id, "origin": "server"})
                    relay.inject_message(msg)

                register_listener(_on_server_write)
            except ImportError:
                click.echo("Warning: websockets not installed, relay disabled.", err=True)
            except Exception as exc:
                click.echo(f"Warning: relay failed to start: {exc}", err=True)

        # Determine relay URL for browser sync-config endpoint
        if relay is not None:
            server._relay_url = f"ws://{host}:{relay_port}"
        elif relay_url:
            server._relay_url = relay_url
        else:
            server._relay_url = ""
        server._relay_enabled = bool(server._relay_url)

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
@click.option("--port", default=None, type=int, help="Port of the dashboard to restart. Defaults to dashboard_port in config, or 8799.")
def restart_cmd(port: int | None) -> None:
    """Send a restart signal to a running Lattice dashboard.

    Finds the process listening on the given port and sends SIGHUP,
    causing the dashboard to gracefully restart in place.
    """
    if port is None:
        lattice_dir = require_root(False)
        config = load_project_config(lattice_dir)
        port = config.get("dashboard_port", _DEFAULT_PORT)

    if not hasattr(signal, "SIGHUP"):
        click.echo("Error: restart via signal is not supported on this platform.", err=True)
        raise SystemExit(1)

    result = subprocess.run(
        ["lsof", "-ti", f":{port}"],
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
