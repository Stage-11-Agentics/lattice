"""``lattice dashboard`` command — launch the read-only web UI."""

from __future__ import annotations

import sys

import click

from lattice.cli.helpers import json_envelope, json_error_obj, require_root
from lattice.cli.main import cli

_LOOPBACK_HOSTS = frozenset({"127.0.0.1", "::1", "localhost"})


@cli.command("dashboard")
@click.option("--host", default="127.0.0.1", help="Host to bind to.")
@click.option("--port", default=8799, type=int, help="Port to bind to.")
@click.option("--json", "output_json", is_flag=True, help="Output structured JSON.")
def dashboard_cmd(host: str, port: int, output_json: bool) -> None:
    """Launch a read-only local web dashboard."""
    lattice_dir = require_root(output_json)

    # Warn on non-loopback bind
    if host not in _LOOPBACK_HOSTS:
        click.echo(
            "Warning: dashboard is exposed on the network. "
            "Bind to 127.0.0.1 for local-only access.",
            err=True,
        )

    from lattice.dashboard.server import create_server

    try:
        server = create_server(lattice_dir, host, port)
    except OSError as exc:
        if output_json:
            click.echo(json_envelope(False, error=json_error_obj("BIND_ERROR", str(exc))))
        else:
            click.echo(f"Error: {exc}", err=True)
        raise SystemExit(1)

    url = f"http://{host}:{port}/"

    if output_json:
        click.echo(json_envelope(True, data={"host": host, "port": port, "url": url}))
    else:
        click.echo(f"Lattice dashboard: {url}")
        click.echo("Press Ctrl+C to stop.")

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        sys.exit(0)
    finally:
        server.server_close()
