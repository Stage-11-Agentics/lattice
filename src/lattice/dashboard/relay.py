"""WebSocket relay for browser-to-browser Automerge sync.

Dead simple: receive a message from one client, broadcast to all others.
No Automerge logic — just a message bus over WebSocket.
"""

from __future__ import annotations

import asyncio
import sys
import threading
from typing import Any

DEFAULT_RELAY_PORT = 9801


class SyncRelay:
    """WebSocket relay that broadcasts messages between connected browsers."""

    def __init__(self, host: str = "127.0.0.1", port: int = DEFAULT_RELAY_PORT) -> None:
        self.host = host
        self.port = port
        self._clients: set[Any] = set()
        self._loop: asyncio.AbstractEventLoop | None = None
        self._server: Any = None

    async def start(self) -> None:
        """Start the relay server."""
        import websockets

        self._loop = asyncio.get_running_loop()
        self._server = await websockets.serve(
            self._handle_client,
            self.host,
            self.port,
        )
        print(
            f"lattice relay: ws://{self.host}:{self.port}",
            file=sys.stderr,
        )

    async def run_forever(self) -> None:
        """Start and run until cancelled."""
        await self.start()
        await asyncio.Future()  # block forever

    async def stop(self) -> None:
        if self._server:
            self._server.close()
            await self._server.wait_closed()

    async def _handle_client(self, websocket: Any, path: Any = None) -> None:  # noqa: ARG002
        # Notify existing clients so they re-send their docs to the newcomer
        if self._clients:
            notification = '{"type":"new_peer"}'
            await asyncio.gather(
                *(c.send(notification) for c in self._clients),
                return_exceptions=True,
            )
        self._clients.add(websocket)
        try:
            async for message in websocket:
                others = [c for c in self._clients if c is not websocket]
                if others:
                    await asyncio.gather(
                        *(c.send(message) for c in others),
                        return_exceptions=True,
                    )
        finally:
            self._clients.discard(websocket)

    def inject_message(self, message: str | bytes) -> None:
        """Thread-safe: push a message from a non-async context (e.g. bus listener).

        Broadcasts to ALL connected clients.
        """
        if self._loop is None or not self._loop.is_running():
            return
        asyncio.run_coroutine_threadsafe(self._broadcast_all(message), self._loop)

    async def _broadcast_all(self, message: str | bytes) -> None:
        if self._clients:
            await asyncio.gather(
                *(c.send(message) for c in self._clients),
                return_exceptions=True,
            )


def start_relay_thread(
    host: str = "127.0.0.1",
    port: int = DEFAULT_RELAY_PORT,
) -> SyncRelay:
    """Start the relay in a daemon thread.  Returns the ``SyncRelay`` instance."""
    relay = SyncRelay(host, port)
    ready = threading.Event()

    def _run() -> None:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        loop.run_until_complete(relay.start())
        ready.set()
        loop.run_forever()

    t = threading.Thread(target=_run, name="lattice-relay", daemon=True)
    t.start()
    ready.wait(timeout=5.0)
    return relay
