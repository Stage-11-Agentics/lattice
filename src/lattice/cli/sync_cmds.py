"""CLI commands for Automerge P2P sync."""

from __future__ import annotations

import asyncio
import json

import click

from lattice.cli.main import cli


@cli.group()
def sync() -> None:
    """Peer-to-peer sync via Automerge CRDTs."""


@sync.command("start")
@click.option("--host", default=None, help="Bind address (default: 127.0.0.1).")
@click.option("--port", type=int, default=None, help="Listen port (default: 9800).")
def sync_start(host: str | None, port: int | None) -> None:
    """Start the sync server.  Other peers connect to this instance."""
    from lattice.cli.helpers import require_root
    from lattice.sync.server import LatticeSyncServer

    lattice_dir = require_root(False)
    server = LatticeSyncServer(lattice_dir, host=host, port=port)

    try:
        asyncio.run(server.start())
    except KeyboardInterrupt:
        click.echo("\nlattice sync: stopped.")


@sync.command("connect")
@click.argument("peer_url")
def sync_connect(peer_url: str) -> None:
    """Connect to a peer's sync server and begin bidirectional sync."""
    from lattice.cli.helpers import require_root
    from lattice.sync.client import LatticeSyncClient

    lattice_dir = require_root(False)
    client = LatticeSyncClient(lattice_dir, peer_url)

    try:
        asyncio.run(client.connect())
    except KeyboardInterrupt:
        click.echo("\nlattice sync: disconnected.")


@sync.command("bootstrap")
def sync_bootstrap() -> None:
    """Convert all existing tasks into Automerge CRDT documents."""
    from lattice.cli.helpers import require_root
    from lattice.sync.bridge import SyncBridge
    from lattice.sync.store import AutomergeStore

    lattice_dir = require_root(False)
    sync_dir = lattice_dir / "sync"
    store = AutomergeStore(sync_dir)

    config = None
    config_path = lattice_dir / "config.json"
    if config_path.exists():
        config = json.loads(config_path.read_text())

    bridge = SyncBridge(lattice_dir, store, config)

    tasks_dir = lattice_dir / "tasks"
    if not tasks_dir.exists():
        click.echo("No tasks found.")
        return

    count = 0
    for snapshot_file in tasks_dir.glob("*.json"):
        task_id = snapshot_file.stem
        if not store.has(task_id):
            bridge.bootstrap_task(task_id)
            count += 1
            click.echo(f"  bootstrapped: {task_id}")

    if count:
        click.echo(f"\nBootstrapped {count} tasks into Automerge documents.")
    else:
        click.echo("All tasks already have Automerge documents.")


@sync.command("status")
@click.option("--json", "as_json", is_flag=True, help="Output as JSON.")
def sync_status(as_json: bool) -> None:
    """Show sync state and document counts."""
    from lattice.cli.helpers import require_root
    from lattice.sync.config import load_sync_config
    from lattice.sync.store import AutomergeStore

    lattice_dir = require_root(False)
    sync_dir = lattice_dir / "sync"
    store = AutomergeStore(sync_dir)
    config = load_sync_config(lattice_dir)

    task_ids = store.list_task_ids()
    tasks_dir = lattice_dir / "tasks"
    total_tasks = len(list(tasks_dir.glob("*.json"))) if tasks_dir.exists() else 0

    if as_json:
        data = {
            "peer_id": config.get("peer_id", ""),
            "synced_documents": len(task_ids),
            "total_tasks": total_tasks,
            "peers": config.get("peers", []),
            "listen": config.get("listen", {}),
        }
        click.echo(json.dumps({"ok": True, "data": data}, sort_keys=True, indent=2))
        return

    click.echo(f"Peer ID: {config.get('peer_id', 'not configured')}")
    click.echo(f"Listen: {config.get('listen', {}).get('host', '?')}:{config.get('listen', {}).get('port', '?')}")
    click.echo(f"Synced documents: {len(task_ids)} / {total_tasks} tasks")

    peers = config.get("peers", [])
    if peers:
        click.echo(f"Configured peers: {len(peers)}")
        for peer in peers:
            click.echo(f"  {peer.get('name', peer.get('url', '?'))}: {peer.get('url', '?')}")
    else:
        click.echo("No peers configured.")
