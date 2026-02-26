"""WebSocket sync server for Automerge document synchronization.

Each task is a separate Automerge document.  The server maintains
documents in an AutomergeStore and uses the Automerge sync protocol
(generate_sync_message / receive_sync_message) to exchange changes
with connected peers over WebSocket.
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

from automerge import core

from lattice.storage.bus import register_listener, unregister_listener
from lattice.sync.bridge import SyncBridge
from lattice.sync.config import load_sync_config
from lattice.sync.store import AutomergeStore


class LatticeSyncServer:
    """WebSocket server that syncs Lattice tasks via Automerge CRDTs."""

    def __init__(
        self,
        lattice_dir: Path,
        host: str | None = None,
        port: int | None = None,
    ) -> None:
        self.lattice_dir = lattice_dir
        sync_config = load_sync_config(lattice_dir)
        self.host = host or sync_config["listen"]["host"]
        self.port = port or sync_config["listen"]["port"]

        sync_dir = lattice_dir / "sync"
        self.store = AutomergeStore(sync_dir)

        config = None
        config_path = lattice_dir / "config.json"
        if config_path.exists():
            config = json.loads(config_path.read_text())

        self.bridge = SyncBridge(lattice_dir, self.store, config)
        self._peers: dict[str, _PeerConnection] = {}
        self._server = None

    async def start(self) -> None:
        """Bootstrap existing tasks and start the WebSocket server."""
        self._bootstrap_from_files()

        # Register bus listener so local writes propagate to peers
        register_listener(self._on_local_write)

        try:
            import websockets

            self._server = await websockets.serve(
                self._handle_peer,
                self.host,
                self.port,
            )
            print(
                f"lattice sync: listening on ws://{self.host}:{self.port}",
                file=sys.stderr,
            )
            await asyncio.Future()  # Run forever
        except ImportError:
            # Fallback: simple asyncio server with raw protocol
            server = await asyncio.start_server(
                self._handle_raw_peer,
                self.host,
                self.port,
            )
            print(
                f"lattice sync: listening on {self.host}:{self.port} (raw TCP)",
                file=sys.stderr,
            )
            async with server:
                await server.serve_forever()
        finally:
            unregister_listener(self._on_local_write)

    async def stop(self) -> None:
        """Stop the server."""
        if self._server:
            self._server.close()

    def _bootstrap_from_files(self) -> None:
        """Convert existing task snapshots into Automerge documents."""
        tasks_dir = self.lattice_dir / "tasks"
        if not tasks_dir.exists():
            return

        count = 0
        for snapshot_file in tasks_dir.glob("*.json"):
            task_id = snapshot_file.stem
            if not self.store.has(task_id):
                self.bridge.bootstrap_task(task_id)
                count += 1

        if count:
            print(f"lattice sync: bootstrapped {count} tasks", file=sys.stderr)

    def _on_local_write(self, task_id: str, events: list[dict], snapshot: dict) -> None:
        """Bus listener: update CRDT and queue sync to peers."""
        self.bridge.on_local_write(task_id, events, snapshot)
        # Notify all connected peers about the change
        for peer in self._peers.values():
            peer.needs_sync.add(task_id)

    async def _handle_peer(self, websocket, path=None) -> None:
        """Handle a WebSocket peer connection."""
        peer_id = str(id(websocket))
        peer = _PeerConnection(peer_id)
        self._peers[peer_id] = peer

        print(f"lattice sync: peer connected: {peer_id}", file=sys.stderr)

        try:
            # Initial sync: send all documents
            for task_id in self.store.list_task_ids():
                await self._sync_task_to_peer(task_id, peer, websocket)

            # Message loop
            async for message in websocket:
                await self._handle_message(peer, websocket, message)
        except Exception as exc:
            print(f"lattice sync: peer {peer_id} error: {exc}", file=sys.stderr)
        finally:
            del self._peers[peer_id]
            print(f"lattice sync: peer disconnected: {peer_id}", file=sys.stderr)

    async def _handle_raw_peer(self, reader, writer) -> None:
        """Handle a raw TCP peer connection (fallback without websockets)."""
        peer_id = str(id(writer))
        peer = _PeerConnection(peer_id)
        self._peers[peer_id] = peer

        print(f"lattice sync: peer connected (raw): {peer_id}", file=sys.stderr)

        try:
            for task_id in self.store.list_task_ids():
                doc = self.store.get_or_create(task_id)
                saved = doc._doc.save()
                header = f"{task_id}:{len(saved)}\n".encode()
                writer.write(header + saved)
                await writer.drain()

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
                await self._apply_remote_doc(task_id, data)
        except (asyncio.IncompleteReadError, ConnectionError):
            pass
        finally:
            del self._peers[peer_id]
            writer.close()

    async def _sync_task_to_peer(self, task_id: str, peer: _PeerConnection, websocket) -> None:
        """Send a task's Automerge document to a peer via sync protocol."""
        doc = self.store.get_or_create(task_id)
        sync_state = peer.get_sync_state(task_id)

        # Sync loop for this document
        while True:
            msg = doc._doc.generate_sync_message(sync_state)
            if msg is None:
                break
            encoded = msg.encode()
            payload = json.dumps({"task_id": task_id, "sync_msg": encoded.hex()})
            await websocket.send(payload)

    async def _handle_message(self, peer: _PeerConnection, websocket, raw_message) -> None:
        """Process an incoming sync message from a peer."""
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

        sync_state = peer.get_sync_state(task_id)
        doc._doc.receive_sync_message(sync_state, sync_msg)

        new_state = doc.to_py()
        self.store.save(task_id, doc)

        # Generate events from the diff
        if old_state != new_state:
            self.bridge.on_crdt_change(task_id, old_state, new_state)

        # Send response
        response = doc._doc.generate_sync_message(sync_state)
        if response is not None:
            payload = json.dumps({
                "task_id": task_id,
                "sync_msg": response.encode().hex(),
            })
            await websocket.send(payload)

    async def _apply_remote_doc(self, task_id: str, data: bytes) -> None:
        """Apply a full document from a remote peer."""
        remote_doc = core.Document.load(data)
        doc = self.store.get_or_create(task_id)
        old_state = doc.to_py()

        doc._doc.merge(remote_doc)
        new_state = doc.to_py()
        self.store.save(task_id, doc)

        if old_state != new_state:
            self.bridge.on_crdt_change(task_id, old_state, new_state)


class _PeerConnection:
    """Track per-peer sync state."""

    def __init__(self, peer_id: str) -> None:
        self.peer_id = peer_id
        self.sync_states: dict[str, core.SyncState] = {}
        self.needs_sync: set[str] = set()

    def get_sync_state(self, task_id: str) -> core.SyncState:
        if task_id not in self.sync_states:
            self.sync_states[task_id] = core.SyncState()
        return self.sync_states[task_id]
