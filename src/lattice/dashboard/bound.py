"""``lattice dashboard`` on a bound (hosted) checkout (SPEC §8.3, §9.6, §10).

The dashboard reads the checkout's cache, which an embedded follower keeps
caught up for the dashboard's lifetime. Each read runs the same freshness
step a CLI read runs (a catch-up unless the follower is live, SPEC §9.5), then
holds the cache's shared read lock for that read only (never for the
dashboard's lifetime, which would starve every sync on the machine). Its writes go to the server through ``HostedBoard.execute``, as the
browser actor: the actor ``/v1/info`` names for the checkout's token.
"""

from __future__ import annotations

import sys
import threading
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from lattice.dashboard import api
from lattice.dashboard.server import DashboardBoard


class BrowserActor:
    """The browser actor of a remote's token, read from ``/v1/info`` for every write.

    Never cached: a token can be re-scoped while the dashboard runs (an actor
    granted or removed changes which actor a browser writes as, SPEC §8.3),
    and dashboard writes are human-paced, so one extra request per write is
    cheap. An unreachable server (``SERVER_UNREACHABLE``) or a token with no
    browser actor (``MISSING_ACTOR``) refuses the write before anything is sent.
    """

    def __init__(self, remote: Any, info_getter: Callable[[Any], dict] | None = None) -> None:
        if info_getter is None:
            from lattice.remote.stream import get_info

            info_getter = get_info
        self._remote = remote
        self._get_info = info_getter

    def __call__(self) -> str:
        identity = self._get_info(self._remote).get("identity") or {}
        return api.browser_actor(identity)


def _notice(line: str) -> None:
    sys.stderr.write(f"lattice dashboard: {line}\n")


class _OnceEach:
    """Pass a notice line on only when it differs from the previous one, so a
    page polling every few seconds while the server is down logs it once."""

    def __init__(self, sink: Callable[[str], None]) -> None:
        self._sink = sink
        self._last: str | None = None
        self._lock = threading.Lock()

    def __call__(self, line: str) -> None:
        with self._lock:
            if line == self._last:
                return
            self._last = line
        self._sink(line)


class _FollowerKeeper:
    """Runs the embedded follower in a thread, and runs a new one if it died.

    A follower stops on a hard error (a revoked token, a proxy refusing the
    stream); reads fall back to their own catch-up meanwhile (SPEC §9.5), and
    :meth:`ensure_running` starts a fresh follower at most once per
    *restart_after* seconds, so a repaired token or server resumes live updates.
    """

    def __init__(
        self,
        make: Callable[[], Any],
        on_notice: Callable[[str], None],
        restart_after: float,
    ) -> None:
        self._make = make
        self._on_notice = on_notice
        self._restart_after = restart_after
        self._lock = threading.Lock()
        self._stopping = False
        self._follower: Any = None
        self._thread: threading.Thread | None = None
        self._started_at = 0.0
        self.starts = 0

    def _start(self) -> None:
        follower = self._make()

        def run() -> None:
            try:
                follower.run()
            except Exception as exc:  # noqa: BLE001 - a background thread reports, never raises
                self._on_notice(f"the follower stopped ({exc}); reads catch up on their own")

        self._follower = follower
        self._thread = threading.Thread(target=run, name="lattice-dashboard-follower", daemon=True)
        self._started_at = time.monotonic()
        self.starts += 1
        self._thread.start()

    def start(self) -> None:
        with self._lock:
            self._start()

    def ensure_running(self) -> None:
        with self._lock:
            if self._stopping or self._thread is None or self._thread.is_alive():
                return
            if time.monotonic() - self._started_at < self._restart_after:
                return
            self._on_notice("restarting the follower")
            self._start()

    def stop(self) -> None:
        with self._lock:
            self._stopping = True
            follower, thread = self._follower, self._thread
        if follower is not None:
            follower.stop()
        if thread is not None:
            thread.join(timeout=5)


@contextmanager
def bound_dashboard(
    board: Any,
    *,
    follower_factory: Callable[..., Any] | None = None,
    on_notice: Callable[[str], None] = _notice,
    restart_after: float = 30.0,
) -> Iterator[DashboardBoard]:
    """Serve *board* (a ``HostedBoard``) with a follower running until the block exits.

    The caller has already caught the cache up (``prepare_read``), so the
    follower starts from the stream. Every read first runs the freshness step
    CLI reads run (``session.catch_up_unless_live``): nothing while the
    follower is live, else a catch-up or, offline, the cache with its notice;
    then it holds the cache's shared read lock for that read only. A follower
    that died is restarted (see :class:`_FollowerKeeper`).
    """
    from lattice.remote import cache, session
    from lattice.remote.follower import Follower, follow_target

    remote, project = follow_target(board.root)
    factory = follower_factory if follower_factory is not None else Follower
    notify = _OnceEach(on_notice)
    keeper = _FollowerKeeper(
        lambda: factory(
            board.root,
            remote,
            project,
            catch_up=cache.catch_up,
            on_notice=notify,
            initial_sync=False,
        ),
        notify,
        restart_after,
    )

    @contextmanager
    def reading() -> Iterator[Path]:
        keeper.ensure_running()
        session.catch_up_unless_live(board.hosted, defer_to_running_sync=True, notify=notify)
        with cache.read_lock(board.root) as lattice_dir:
            yield lattice_dir

    keeper.start()
    try:
        yield DashboardBoard(
            board,
            browser_actor=BrowserActor(board.remote),
            hosted=True,
            read_dir=board.cache_dir,
            reading=reading,
        )
    finally:
        keeper.stop()
