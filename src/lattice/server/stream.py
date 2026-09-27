"""The change stream's moving parts: broadcaster, subscribers, and the SSE response (SPEC §8.9).

Publication happens in a worker thread, under the project's work lock, in
``seq`` order (the finish step of each transaction, and ``external`` lines).
It must never wait on a reader, so each :class:`Subscriber` holds a bounded
queue: :meth:`Subscriber.offer` appends in O(1) and wakes the subscriber's
pump on the event loop with ``call_soon_threadsafe``. A subscriber whose
queue is full is removed and its pump cancelled, even mid-``send`` on a
connection that has stopped reading; uvicorn then closes the transport of the
unfinished response. The reader resumes later from its ``Last-Event-ID``.

The stream is written directly on Starlette, with no SSE library (SPEC §8.9,
framing and lifecycle): the overflow rule needs to cancel a ``send`` blocked
on back-pressure, and shutdown needs to end every stream at once.

Wire format (one frame per event, ``\\n`` line endings, compact sorted JSON)::

    id: <epoch>:<seq>:<line hash>
    event: journal
    data: {<journal entry>, "events": [...]}

    event: reset
    data: {"epoch": <new epoch>}

    event: heartbeat
    data: {"epoch": ..., "head_seq": ...}
"""

from __future__ import annotations

import asyncio
import json
import threading
import time
from collections import deque
from collections.abc import Callable
from typing import Any

import anyio
from starlette.types import Receive, Scope, Send

JOURNAL, RESET = "journal", "reset"

#: The scope key under which :class:`~lattice.server.app.HeadersMiddleware` keeps the
#: server's own ``send``, so an aborted stream can abort its connection.
RAW_SEND = "lattice.raw_send"


def frame(event: str, data: Any, event_id: str | None = None) -> bytes:
    """One SSE event. JSON escapes every line break, so ``data`` is one line; and
    every non-ASCII character, so event data holding a lone surrogate still encodes."""
    text = json.dumps(data, sort_keys=True, separators=(",", ":"))
    head = f"id: {event_id}\n" if event_id is not None else ""
    return f"{head}event: {event}\ndata: {text}\n\n".encode()


def entry_id(epoch: str, seq: int, digest: str) -> str:
    return f"{epoch}:{seq}:{digest}"


def journal_frame(epoch: str, line: dict, digest: str, events: list[dict]) -> bytes:
    return frame(JOURNAL, {**line, "events": events}, entry_id(epoch, line["seq"], digest))


def reset_frame(epoch: str) -> bytes:
    return frame(RESET, {"epoch": epoch})


def heartbeat_frame(epoch: str, head_seq: int) -> bytes:
    return frame("heartbeat", {"epoch": epoch, "head_seq": head_seq})


def parse_entry_id(value: str | None) -> tuple[str, int, str] | None:
    """``<epoch>:<seq>:<line hash>`` → its parts, or ``None`` when malformed."""
    if not value:
        return None
    parts = value.strip().split(":")
    if len(parts) != 3 or not parts[1].isdigit() or not parts[0] or not parts[2]:
        return None
    return parts[0], int(parts[1]), parts[2]


# ---------------------------------------------------------------------------
# Subscribers and the broadcaster
# ---------------------------------------------------------------------------


class Subscriber:
    """One open stream: a bounded queue filled by publication, drained by its pump."""

    def __init__(self, loop: asyncio.AbstractEventLoop, max_entries: int) -> None:
        self.loop = loop
        self.max_entries = max(1, max_entries)
        self.wake = asyncio.Event()
        #: The stream is ending at once: its queue filled, or it was aborted.
        self.aborted = False
        self.overflowed = False
        self._lock = threading.Lock()
        self._items: deque[tuple[str, int, bytes]] = deque()
        self._abort: Callable[[], None] | None = None

    def _call(self, fn: Callable[[], Any]) -> None:
        try:
            self.loop.call_soon_threadsafe(fn)
        except RuntimeError:
            pass  # the loop is closed: the server is gone, and so is this stream

    def offer(self, item: tuple[str, int, bytes]) -> bool:
        """Queue *item* without blocking; ``False`` when this subscriber is gone
        (a full queue ends it at once)."""
        with self._lock:
            if self.aborted:
                return False
            if len(self._items) < self.max_entries:
                self._items.append(item)
                self._call(self.wake.set)
                return True
            self.overflowed = True
        self.abort()
        return False

    def abort(self) -> None:
        """End the stream at once, even while its pump waits on a ``send`` a stalled
        client never drains: the pump is cancelled and the connection aborted
        (server shutdown, a failed publication, a quarantine, a full queue)."""
        with self._lock:
            self.aborted = True
            self._items.clear()
        self._call(self._do_abort)

    def take(self) -> list[tuple[str, int, bytes]]:
        with self._lock:
            items = list(self._items)
            self._items.clear()
        return items

    def attach_abort(self, abort: Callable[[], None]) -> None:
        """Called on the loop by the response once its pump runs."""
        self._abort = abort
        if self.aborted:
            abort()

    def _do_abort(self) -> None:
        self.wake.set()
        if self._abort is not None:
            self._abort()


class Broadcaster:
    """A project's open streams. Every method is thread-safe and non-blocking."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._subscribers: list[Subscriber] = []
        #: Streams disconnected because their queue filled (tests and logs).
        self.overflows = 0
        #: What heartbeats announce, ``(epoch, head_seq)``. Changed only after the
        #: matching entry or ``reset`` is queued to every subscriber, so no
        #: heartbeat names a head or epoch whose message a stream has not queued.
        self._announced: tuple[str, int] = ("", 0)

    @property
    def announced(self) -> tuple[str, int]:
        with self._lock:
            return self._announced

    def announce(self, epoch: str, head_seq: int) -> None:
        with self._lock:
            self._announced = (epoch, head_seq)

    def count(self) -> int:
        with self._lock:
            return len(self._subscribers)

    def subscribe(self, subscriber: Subscriber, cap: int) -> bool:
        """Register *subscriber* unless *cap* streams are already open."""
        with self._lock:
            if len(self._subscribers) >= cap:
                return False
            self._subscribers.append(subscriber)
            return True

    def unsubscribe(self, subscriber: Subscriber) -> None:
        with self._lock:
            if subscriber in self._subscribers:
                self._subscribers.remove(subscriber)

    def has_subscribers(self) -> bool:
        with self._lock:
            return bool(self._subscribers)

    def publish(self, epoch: str, seq: int, data: bytes | None) -> None:
        """Queue a journal entry's frame to every subscriber, then announce it.
        *data* is ``None`` when nobody listens (only the announcement changes)."""
        if data is not None:
            self._offer((JOURNAL, seq, data))
        self.announce(epoch, seq)

    def reset(self, epoch: str) -> None:
        """Queue ``reset`` to every subscriber, then announce the new epoch at seq 0."""
        self._offer((RESET, 0, reset_frame(epoch)))
        self.announce(epoch, 0)

    def _offer(self, item: tuple[str, int, bytes]) -> None:
        with self._lock:
            subscribers = list(self._subscribers)
        for subscriber in subscribers:
            if not subscriber.offer(item):
                self.unsubscribe(subscriber)
                if subscriber.overflowed:
                    with self._lock:
                        self.overflows += 1

    def close_all(self) -> None:
        """End every open stream at once (followers resume from ``Last-Event-ID``)."""
        with self._lock:
            subscribers, self._subscribers = self._subscribers, []
        for subscriber in subscribers:
            subscriber.abort()


# ---------------------------------------------------------------------------
# The SSE response
# ---------------------------------------------------------------------------


class EventStream:
    """An ASGI response: the *initial* frames (a ``reset`` or the replay), then a
    heartbeat, then live messages from *subscriber*, with a heartbeat every
    *heartbeat_seconds*.

    Before each heartbeat the stream checks *alive* (the credential still holds
    and the project still serves; otherwise it ends), then sends every message
    already queued, and only then a heartbeat naming *announced* as read before
    that queue came up empty. Messages are queued before the broadcaster
    announces them, so a heartbeat never names an epoch or head whose ``reset``
    or entry this stream has not sent yet (SPEC §8.9).

    *sent_seq* is the last ``seq`` the initial frames delivered; live entries at
    or below it are dropped. *on_close* runs once, however the stream ends.
    """

    def __init__(
        self,
        subscriber: Subscriber,
        *,
        initial: list[bytes],
        sent_seq: int,
        alive: Callable[[], bool],
        announced: Callable[[], tuple[str, int]],
        heartbeat_seconds: float,
        on_close: Callable[[Subscriber], None],
    ) -> None:
        self.subscriber = subscriber
        self.initial = initial
        self.sent_seq = sent_seq
        self.alive = alive
        self.announced = announced
        self.heartbeat_seconds = max(0.01, float(heartbeat_seconds))
        self.on_close = on_close
        self.outcome = "open"

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        sub = self.subscriber
        try:
            await send(
                {
                    "type": "http.response.start",
                    "status": 200,
                    "headers": [
                        (b"content-type", b"text/event-stream"),
                        (b"cache-control", b"no-store"),
                        (b"x-accel-buffering", b"no"),
                    ],
                }
            )
            async with anyio.create_task_group() as group:
                sub.attach_abort(group.cancel_scope.cancel)
                group.start_soon(self._watch_disconnect, receive, group.cancel_scope)
                await self._pump(send)
                group.cancel_scope.cancel()
            if sub.aborted:
                self.outcome = "overflow" if sub.overflowed else "aborted"
                _abort_connection(scope)
            elif self.outcome == "ended":
                await send({"type": "http.response.body", "body": b"", "more_body": False})
        except OSError:
            self.outcome = "disconnected"
        finally:
            self.on_close(sub)

    async def _watch_disconnect(self, receive: Receive, scope: anyio.CancelScope) -> None:
        while True:
            message = await receive()
            if message["type"] == "http.disconnect":
                self.outcome = "disconnected"
                scope.cancel()
                return

    async def _body(self, send: Send, data: bytes) -> None:
        await send({"type": "http.response.body", "body": data, "more_body": True})

    async def _deliver(self, send: Send, items: list[tuple[str, int, bytes]]) -> bool:
        """Send queued messages."""
        for kind, seq, data in items:
            if kind == JOURNAL:
                if seq <= self.sent_seq:
                    continue  # already delivered by the replay
                self.sent_seq = seq
            elif kind == RESET:
                self.sent_seq = 0  # the new epoch starts at seq 1
            await self._body(send, data)
        return True

    async def _heartbeat(self, send: Send) -> bool:
        """Recheck, drain the queue, then announce; ``False`` when the stream ends."""
        if not self.alive():
            self.outcome = "ended"
            return False
        while True:
            epoch, head_seq = self.announced()
            items = self.subscriber.take()
            if not items:
                await self._body(send, heartbeat_frame(epoch, head_seq))
                return True
            if not await self._deliver(send, items):
                return False

    async def _pump(self, send: Send) -> None:
        sub = self.subscriber
        for data in self.initial:
            await self._body(send, data)
        if not await self._heartbeat(send):
            return
        next_beat = time.monotonic() + self.heartbeat_seconds
        while True:
            if time.monotonic() >= next_beat:
                # Due even on a busy stream: the heartbeat carries the credential recheck.
                if not await self._heartbeat(send):
                    return
                next_beat = time.monotonic() + self.heartbeat_seconds
            sub.wake.clear()
            items = sub.take()
            if not items:
                if sub.aborted:
                    return
                with anyio.move_on_after(max(0.0, next_beat - time.monotonic())):
                    await sub.wake.wait()
                continue
            if not await self._deliver(send, items):
                return


def _abort_connection(scope: Scope) -> None:
    """Abort the connection under an unfinished stream. Closing it would wait to
    flush bytes a stalled client never reads, and uvicorn's graceful shutdown
    waits for every connection to close."""
    owner = getattr(scope.get(RAW_SEND), "__self__", None)
    transport = getattr(owner, "transport", None)
    abort = getattr(transport, "abort", None)
    if callable(abort):
        abort()
