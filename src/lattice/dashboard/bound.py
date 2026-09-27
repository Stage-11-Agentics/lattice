"""``lattice dashboard`` on a bound (hosted) checkout (SPEC §8.3, §9.6, §10).

The dashboard reads the checkout's cache, which an embedded follower keeps
caught up for the dashboard's lifetime, taking the cache's shared read lock
around each read (never for its lifetime, which would starve every sync on the
machine). Its writes go to the server through ``HostedBoard.execute``, as the
browser actor: the actor ``/v1/info`` names for the checkout's token.
"""

from __future__ import annotations

import sys
import threading
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from typing import Any

from lattice.dashboard import api
from lattice.dashboard.server import DashboardBoard


class BrowserActor:
    """The browser actor of a remote's token, fetched from ``/v1/info`` once.

    Resolved on the first write rather than at startup, so a dashboard started
    while the server is unreachable still serves the cache; a failed fetch
    (``SERVER_UNREACHABLE``) or a token with no browser actor
    (``MISSING_ACTOR``) refuses that write and is tried again on the next.
    """

    def __init__(self, remote: Any, info_getter: Callable[[Any], dict] | None = None) -> None:
        if info_getter is None:
            from lattice.remote.stream import get_info

            info_getter = get_info
        self._remote = remote
        self._get_info = info_getter
        self._lock = threading.Lock()
        self._actor: str | None = None

    def __call__(self) -> str:
        with self._lock:
            if self._actor is None:
                identity = self._get_info(self._remote).get("identity") or {}
                self._actor = api.browser_actor(identity)
            return self._actor


def _notice(line: str) -> None:
    sys.stderr.write(f"lattice dashboard: {line}\n")


@contextmanager
def bound_dashboard(
    board: Any,
    *,
    follower_factory: Callable[..., Any] | None = None,
    on_notice: Callable[[str], None] = _notice,
) -> Iterator[DashboardBoard]:
    """Serve *board* (a ``HostedBoard``) with a follower running until the block exits.

    The caller has already caught the cache up (``prepare_read``), so the
    follower starts from the stream. A follower that stops on a hard error is
    reported on stderr; the dashboard keeps serving the cache it has.
    """
    from lattice.remote import cache
    from lattice.remote.follower import Follower, follow_target

    remote, project = follow_target(board.root)
    factory = follower_factory if follower_factory is not None else Follower
    follower = factory(
        board.root,
        remote,
        project,
        catch_up=cache.catch_up,
        on_notice=on_notice,
        initial_sync=False,
    )

    def run() -> None:
        try:
            follower.run()
        except Exception as exc:  # noqa: BLE001 - a background thread reports, never raises
            on_notice(f"the follower stopped ({exc}); the board may go stale")

    thread = threading.Thread(target=run, name="lattice-dashboard-follower", daemon=True)
    thread.start()
    try:
        yield DashboardBoard(
            board,
            browser_actor=BrowserActor(board.remote),
            hosted=True,
            read_dir=board.cache_dir,
            reading=lambda: cache.read_lock(board.root),
        )
    finally:
        follower.stop()
        thread.join(timeout=5)
