"""Lightweight in-process event bus for post-write notifications.

Listeners are fire-and-forget: failures are logged to stderr but never
raise exceptions or interrupt the write path.

Thread-safe: a lock protects the listener list so callers from the HTTP
server thread and a background WebSocket relay thread can coexist.
"""

from __future__ import annotations

import sys
import threading
from collections.abc import Callable

_lock = threading.Lock()
_listeners: list[Callable[[str, list[dict], dict], None]] = []


def register_listener(fn: Callable[[str, list[dict], dict], None]) -> None:
    """Register a callback invoked after every write_task_event() completion.

    The callback receives ``(task_id, events, snapshot)``.
    """
    with _lock:
        _listeners.append(fn)


def unregister_listener(fn: Callable[[str, list[dict], dict], None]) -> None:
    """Remove a previously registered listener."""
    with _lock:
        try:
            _listeners.remove(fn)
        except ValueError:
            pass


def notify(task_id: str, events: list[dict], snapshot: dict) -> None:
    """Fire all registered listeners.  Never raises."""
    with _lock:
        snapshot_listeners = list(_listeners)
    for fn in snapshot_listeners:
        try:
            fn(task_id, events, snapshot)
        except Exception as exc:
            print(f"lattice: bus listener error: {exc}", file=sys.stderr)
