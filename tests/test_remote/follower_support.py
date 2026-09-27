"""H-10c test support: run a follower in a thread against a stream stub."""

from __future__ import annotations

import contextlib
import threading
from collections.abc import Iterator
from pathlib import Path

from lattice.remote.follower import Follower
from tests.test_remote.stream_stub import SLUG, StubSyncer, stub_remote

HEARTBEAT = 0.2


@contextlib.contextmanager
def following(
    root: Path, url: str, syncer: StubSyncer, *, max_backoff: float = 1.0, **kwargs
) -> Iterator[Follower]:
    """Run a follower on *url* in a thread; stop and join it on exit."""
    follower = Follower(
        root, stub_remote(url), SLUG, catch_up=syncer, max_backoff=max_backoff, **kwargs
    )
    thread = threading.Thread(target=follower.run, daemon=True)
    thread.start()
    try:
        yield follower
    finally:
        follower.stop()
        thread.join(timeout=5)
        assert not thread.is_alive(), "follower did not stop"
