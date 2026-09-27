"""Hosted ``lattice watch`` / ``lattice wait``: events from the stream, not the files.

On a hosted checkout nothing writes the cache's event logs but the syncer, so
watching the files would only ever see this machine's own syncs. Instead
:func:`hosted_stream_events` runs a :class:`~lattice.remote.follower.Follower`
(which holds the stream, or polls when it cannot) and, after each sync that
applied a delta, reads what that sync appended to ``events/*.jsonl`` under the
cache's shared read lock. The events are the same dicts local
:func:`lattice.core.event_stream.stream_events` yields (parsed lines with
``task_id`` from the file name), so ``watch`` and ``wait`` print the same
output as local, whether the stream delivered the entry, a heartbeat announced
it, or a poll found it.
"""

from __future__ import annotations

import json
import queue
import threading
import time
from collections.abc import Callable, Iterator
from pathlib import Path

from lattice.core.event_stream import _matches_filters, _parse_jsonl_file
from lattice.remote import cache
from lattice.remote.cache import SyncOutcome
from lattice.remote.http import Remote
from lattice.remote.follower import (
    CatchUp,
    Follower,
    ReadLock,
    follow_target,
    hosted_root_of,
    succeeded,
)
from lattice.storage.fs import LATTICE_DIR

FollowerFactory = Callable[..., Follower]


def _snapshot_offsets(events_dir: Path) -> dict[Path, int]:
    offsets: dict[Path, int] = {}
    if events_dir.is_dir():
        for path in events_dir.glob("*.jsonl"):
            try:
                offsets[path] = path.stat().st_size
            except OSError:
                offsets[path] = 0
    return offsets


def _cache_epoch(root: Path) -> str | None:
    """The epoch the cache holds (``cache/state.json``, SPEC §9.4), or ``None``."""
    try:
        state = json.loads((root / LATTICE_DIR / "cache" / "state.json").read_text("utf-8"))
    except (OSError, ValueError):
        return None
    epoch = state.get("epoch") if isinstance(state, dict) else None
    return epoch if isinstance(epoch, str) else None


def _scan(events_dir: Path, offsets: dict[Path, int]) -> list[dict]:
    """Every event appended to ``events/*.jsonl`` since *offsets*, advancing them.

    A log shorter than its offset was replaced, not appended to (a reset this
    process did not see announced, for example one another process applied):
    its offset restarts at its current size, so its history is not replayed
    and its next appended line is.
    """
    batch: list[dict] = []
    if not events_dir.is_dir():
        return batch
    for path in sorted(events_dir.glob("*.jsonl")):
        try:
            size = path.stat().st_size
        except OSError:
            continue
        offset = offsets.get(path, 0)
        if size < offset:
            offsets[path] = size
            continue
        if size == offset:
            continue
        new_events, offsets[path] = _parse_jsonl_file(path, offset)
        batch.extend(new_events)
    batch.sort(key=lambda event: str(event.get("ts", "")))
    return batch


def hosted_stream_events(
    hosted_root: Path,
    remote: Remote,
    project: str,
    *,
    catch_up: CatchUp,
    read_lock: ReadLock,
    task_filter: list[str] | None = None,
    type_filter: list[str] | None = None,
    timeout: float = 0,
    ready: Callable[[], bool] | None = None,
    follower_factory: FollowerFactory = Follower,
    heartbeat_seconds: float | None = None,
) -> Iterator[dict]:
    """Yield each event appended to the hosted board after this call starts.

    One catch-up runs first, so history the cache had not yet synced is not
    replayed (local ``watch`` also starts at the present). Then *ready*, when
    given, is asked whether the caller is already satisfied by the caught-up
    cache (``wait`` rechecks its tasks); if so the iteration ends at once.
    *timeout* stops the iteration after that many seconds (0 = never). The
    follower stops when the generator is closed. A hard sync error
    (``OpError``) from the first catch-up or from the follower propagates.

    The cache is scanned after every successful sync, ``unchanged`` included:
    another process may have applied the update this follower was told about.
    Whenever the cache's epoch changed since the last scan (a reset applied by
    any catch-up, streamed or polled, announced or not), or the follower saw a
    ``reset``, the offsets are taken afresh from the resynced cache instead:
    a full resync is not new events.
    """
    root = Path(hosted_root)
    events_dir = root / LATTICE_DIR / "events"
    start = time.monotonic()

    catch_up(root, bulk=True)
    with read_lock(root):
        offsets = _snapshot_offsets(events_dir)
        epoch = _cache_epoch(root)
    if ready is not None and ready():
        return

    # (reset seen since the previous successful sync?) per successful sync.
    synced: queue.Queue[bool] = queue.Queue()
    reset_pending = False

    # Both callbacks run in the follower's control loop, in order.
    def on_reset() -> None:
        nonlocal reset_pending
        reset_pending = True

    def on_sync(outcome: SyncOutcome) -> None:
        nonlocal reset_pending
        if succeeded(outcome):
            synced.put(reset_pending)
            reset_pending = False

    follower = follower_factory(
        root,
        remote,
        project,
        catch_up=catch_up,
        on_sync=on_sync,
        on_reset=on_reset,
        heartbeat_seconds=heartbeat_seconds,
        initial_sync=False,
    )
    failure: list[BaseException] = []

    def run() -> None:
        try:
            follower.run()
        except BaseException as exc:  # noqa: BLE001 - re-raised in the consumer
            failure.append(exc)

    thread = threading.Thread(target=run, name="lattice-watch-follower", daemon=True)
    thread.start()
    try:
        while True:
            remaining = None
            if timeout > 0:
                remaining = timeout - (time.monotonic() - start)
                if remaining <= 0:
                    return
            try:
                reset = synced.get(timeout=min(remaining, 0.5) if remaining is not None else 0.5)
            except queue.Empty:
                if not thread.is_alive():
                    if failure:
                        raise failure[0]
                    return
                continue
            while True:  # coalesce syncs that landed meanwhile
                try:
                    reset = synced.get_nowait() or reset
                except queue.Empty:
                    break
            with read_lock(root):
                current = _cache_epoch(root)
                if reset or current != epoch:
                    epoch = current
                    offsets = _snapshot_offsets(events_dir)
                    batch: list[dict] = []
                else:
                    batch = _scan(events_dir, offsets)
            for event in batch:
                if _matches_filters(event, task_filter, type_filter):
                    yield event
    finally:
        follower.stop()
        thread.join(timeout=5)


def is_hosted(lattice_dir: Path) -> bool:
    """Whether the board at *lattice_dir* is a hosted checkout's cache."""
    return hosted_root_of(Path(lattice_dir).parent) is not None


def event_source(
    local_stream: Callable[..., Iterator[dict]],
    lattice_dir: Path,
    *,
    task_filter: list[str] | None,
    type_filter: list[str] | None,
    poll_interval: int,
    timeout: int,
    ready: Callable[[], bool] | None = None,
) -> Iterator[dict]:
    """The events ``watch`` / ``wait`` consume: *local_stream* on a local board,
    :func:`hosted_stream_events` on a hosted checkout (*ready* is hosted-only)."""
    root = hosted_root_of(Path(lattice_dir).parent)
    if root is None:
        return local_stream(
            lattice_dir,
            task_filter=task_filter,
            type_filter=type_filter,
            poll_interval=poll_interval,
            timeout=timeout,
        )
    remote, project = follow_target(root)
    return hosted_stream_events(
        root,
        remote,
        project,
        catch_up=cache.catch_up,
        read_lock=cache.read_lock,
        task_filter=task_filter,
        type_filter=type_filter,
        timeout=timeout,
        ready=ready,
    )
