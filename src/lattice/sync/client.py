"""WebSocket sync client that connects to a Lattice sync peer."""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

from automerge import core

from lattice.storage.bus import register_listener, unregister_listener
from lattice.sync.bridge import SyncBridge
from lattice.sync.store import AutomergeStore


class LatticeSyncClient:
    """Connect to a remote Lattice sync server and exchange changes."""

    def __init__(self, lattice_dir: Path, peer_url: str) -> None:
        self.lattice_dir = lattice_dir
        self.peer_url = peer_url

        sync_dir = lattice_dir / "sync"
        self.store = AutomergeStore(sync_dir)

        config = None
        config_path = lattice_dir / "config.json"
        if config_path.exists():
            config = json.loads(config_path.read_text())

        self.bridge = SyncBridge(lattice_dir, self.store, config)
        self._sync_states: dict[str, core.SyncState] = {}
        self._ws = None

    async def connect(self) -> None:
        """Connect to the peer and begin bidirectional sync."""
        register_listener(self._on_local_write)

        try:
            import websockets

            async with websockets.connect(self.peer_url) as ws:
                self._ws = ws
                print(
                    f"lattice sync: connected to {self.peer_url}",
                    file=sys.stderr,
                )
                await self._sync_loop(ws)
        except ImportError:
            # Fallback: raw TCP
            host, port = _parse_url(self.peer_url)
            reader, writer = await asyncio.open_connection(host, port)
            print(
                f"lattice sync: connected to {host}:{port} (raw TCP)",
                file=sys.stderr,
            )
            await self._raw_sync_loop(reader, writer)
        finally:
            unregister_listener(self._on_local_write)

    async def _sync_loop(self, ws) -> None:
        """Main WebSocket sync loop."""
        # Send our documents first
        for task_id in self.store.list_task_ids():
            await self._send_sync_messages(task_id, ws)

        # Process incoming messages
        async for message in ws:
            await self._handle_message(ws, message)

    async def _raw_sync_loop(self, reader, writer) -> None:
        """Main raw TCP sync loop."""
        while True:
            line = await reader.readline()
            if not line:
                break
            parts = line.decode().strip().split(":", 1)
            if len(parts) != 2:
                continue
            task_id, size_str = parts
            size = int(size_str)
            data = await reader.readexactly(size)

            # Apply the remote document
            remote_doc = core.Document.load(data)
            doc = self.store.get_or_create(task_id)
            old_state = doc.to_py()

            doc._doc.merge(remote_doc)
            new_state = doc.to_py()
            self.store.save(task_id, doc)

            if old_state != new_state:
                self.bridge.on_crdt_change(task_id, old_state, new_state)

    async def _send_sync_messages(self, task_id: str, ws) -> None:
        """Send sync messages for a specific task."""
        doc = self.store.get_or_create(task_id)
        sync_state = self._get_sync_state(task_id)

        while True:
            msg = doc._doc.generate_sync_message(sync_state)
            if msg is None:
                break
            payload = json.dumps({
                "task_id": task_id,
                "sync_msg": msg.encode().hex(),
            })
            await ws.send(payload)

    async def _handle_message(self, ws, raw_message) -> None:
        """Process an incoming sync message."""
        try:
            data = json.loads(raw_message)
            task_id = data["task_id"]
            sync_bytes = bytes.fromhex(data["sync_msg"])
            sync_msg = core.Message.decode(sync_bytes)
        except (json.JSONDecodeError, KeyError, ValueError) as exc:
            print(f"lattice sync: bad message: {exc}", file=sys.stderr)
            return

        doc = self.store.get_or_create(task_id)
        old_state = doc.to_py()

        sync_state = self._get_sync_state(task_id)
        doc._doc.receive_sync_message(sync_state, sync_msg)

        new_state = doc.to_py()
        self.store.save(task_id, doc)

        if old_state != new_state:
            self.bridge.on_crdt_change(task_id, old_state, new_state)

        # Send response
        response = doc._doc.generate_sync_message(sync_state)
        if response is not None:
            payload = json.dumps({
                "task_id": task_id,
                "sync_msg": response.encode().hex(),
            })
            await ws.send(payload)

    def _on_local_write(self, task_id: str, events: list[dict], snapshot: dict) -> None:
        """Bus listener: update CRDT from local writes."""
        self.bridge.on_local_write(task_id, events, snapshot)

    def _get_sync_state(self, task_id: str) -> core.SyncState:
        if task_id not in self._sync_states:
            self._sync_states[task_id] = core.SyncState()
        return self._sync_states[task_id]


def _parse_url(url: str) -> tuple[str, int]:
    """Extract host and port from ws://host:port URL."""
    url = url.replace("ws://", "").replace("wss://", "")
    if ":" in url:
        host, port_str = url.split(":", 1)
        port_str = port_str.rstrip("/")
        return host, int(port_str)
    return url, 9800
