"""The follower: holds the change stream and keeps the cache caught up (SPEC §9.6).

A reusable component: ``lattice sync --follow`` runs one in the foreground,
hosted ``watch`` / ``wait`` run one in a thread, and ``lattice dashboard``
embeds one for its lifetime (H-13a)::

    remote, project = follow_target(hosted_root)
    follower = Follower(hosted_root, remote, project)
    thread = threading.Thread(target=follower.run)
    thread.start()
    ...
    follower.stop()
    thread.join()

Two threads cooperate. The **reader** holds ``GET .../stream``, turns each SSE
event into a message on a queue, and reconnects a failed stream with backoff
capped at 60 seconds. The **control loop** (the thread that calls
:meth:`Follower.run`) drains the queue and runs every sync itself, so at most
one sync is in flight and every message that arrived during a sync is
coalesced into the next one.

**Freshness means applied syncs, not received bytes.** The loop tracks
``announced``, the highest seq any entry or heartbeat has named. It sets
``stream_live_until = now + 2 × heartbeat_seconds`` in ``cache/follower.json``
only in answer to something the stream delivered, and only when its last sync
succeeded and the cache's ``head_seq`` has reached ``announced``. A heartbeat
whose head the cache already holds extends it without a sync; an entry or
heartbeat ahead of the cache triggers a sync, and the extension waits for that
sync to apply. Any failed sync clears ``stream_live_until`` at once, so every
read on the machine falls back to its own catch-up.

When the stream delivers nothing (no entry, no heartbeat) for 2 ×
``heartbeat_seconds`` (a stream a proxy refuses, buffers, or silently drops),
the loop clears ``stream_live_until`` and polls sync every
``heartbeat_seconds`` until the stream delivers again. Polled syncs keep the
cache fresh but never extend ``stream_live_until``. A ``reset`` event (or a
heartbeat naming a new epoch) triggers a full resync: the cache's epoch no
longer matches, so its next catch-up is a reset sync (AC-23).
"""

from __future__ import annotations

import contextlib
import json
import os
import queue
import re
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from lattice.core.errors import OpError
from lattice.remote.cache import SyncOutcome
from lattice.remote import cache, cache_paths, config
from lattice.remote.http import Remote
from lattice.remote.stream import StreamConnection, get_info, open_stream
from lattice.storage.fs import LATTICE_DIR

#: ``catch_up(hosted_root, *, bulk=False) -> SyncOutcome`` (H-10b's signature).
CatchUp = Callable[..., SyncOutcome]
#: ``read_lock(hosted_root)``: a context manager yielding the cache's ``.lattice/``.
ReadLock = Callable[[Path], contextlib.AbstractContextManager[Path]]

#: Outcomes after which the cache holds the server's head as of the sync.
SUCCESS_KINDS: frozenset[str] = frozenset({"applied", "unchanged"})


def succeeded(outcome: SyncOutcome) -> bool:
    """Whether *outcome* left the cache at the server's head."""
    return outcome.kind in SUCCESS_KINDS


FOLLOWER_JSON = "follower.json"
DEFAULT_HEARTBEAT_SECONDS = 2.0
MAX_BACKOFF_SECONDS = 60.0

#: Hard sync errors a retry cannot fix: the follower stops and raises them, so
#: ``lattice sync --follow`` ends with the typed error. Every other hard error
#: (``INTEGRITY_ERROR`` for a rejected delta, a non-transient server error) is a
#: failed sync: freshness is cleared and the next sync backs off.
FATAL_SYNC_CODES = frozenset(
    {
        "PROXY_REJECTED",
        "UNAUTHENTICATED",
        "FORBIDDEN",
        "PROTOCOL_MISMATCH",
        "CLIENT_TOO_OLD",
        "NOT_HOSTED",
        "REMOTE_NOT_CONFIGURED",
        "TOKEN_ENV_UNSET",
    }
)


def follower_path(hosted_root: Path) -> Path:
    """``<hosted_root>/.lattice/cache/follower.json``."""
    return Path(hosted_root) / LATTICE_DIR / "cache" / FOLLOWER_JSON


def _iso(moment: datetime) -> str:
    return moment.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def _parse_iso(value: object) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return None
    return parsed


def read_follower(hosted_root: Path) -> dict[str, Any] | None:
    """The parsed ``cache/follower.json``, or ``None`` when absent or unreadable."""
    try:
        data = json.loads(follower_path(hosted_root).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def _pid_alive(pid: object) -> bool:
    if not isinstance(pid, int) or isinstance(pid, bool) or pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


def live_follower(hosted_root: Path, *, now: datetime | None = None) -> bool:
    """Whether a live follower keeps this cache fresh (SPEC §9.5, the reader side).

    True only when ``stream_live_until`` is in the future and ``pid`` is a
    live process (``os.kill(pid, 0)`` succeeds). A follower killed without a
    chance to clear its file therefore stops counting as soon as it dies.
    """
    data = read_follower(hosted_root)
    if data is None:
        return False
    until = _parse_iso(data.get("stream_live_until"))
    if until is None:
        return False
    moment = now if now is not None else datetime.now(timezone.utc)
    return until > moment and _pid_alive(data.get("pid"))


# ---------------------------------------------------------------------------
# Which checkout is hosted, and where its server is
# ---------------------------------------------------------------------------


def hosted_root_of(root: Path | None) -> Path | None:
    """*root* if it is a hosted checkout (SPEC §9.3, decided by
    :func:`lattice.remote.binding.classify`), else ``None``.

    Raises ``BINDING_CONFLICT`` for a binding beside a local board, or a cache
    marker naming another remote or project than the binding.
    """
    if root is None:
        return None
    from lattice.remote.binding import classify

    hosted = classify(Path(root))
    return hosted.root if hosted is not None else None


def follow_target(hosted_root: Path) -> tuple[Remote, str]:
    """The :class:`Remote` and project slug *hosted_root*'s cache is bound to.

    Raises ``NOT_HOSTED``, or the remote's first-contact errors
    (``REMOTE_NOT_CONFIGURED``, ``TOKEN_ENV_UNSET``).
    """
    identity = cache.cache_identity(hosted_root)
    if identity is None:
        raise cache.not_hosted(Path(hosted_root))
    alias, project = identity
    return config.resolve_remote(alias), project


# ---------------------------------------------------------------------------
# Messages from the reader to the control loop
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class _Entry:
    epoch: str | None
    seq: int


@dataclass(frozen=True)
class _Heartbeat:
    epoch: str | None
    head_seq: int


@dataclass(frozen=True)
class _Reset:
    epoch: str | None


@dataclass(frozen=True)
class _Down:
    error: OpError


_Delivery = _Entry | _Heartbeat | _Reset


def _as_int(value: object) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value


_ENTRY_ID = re.compile(r"([^:\s]+):([0-9]+):([0-9a-f]{32})")


def parse_stream_event(event: Any) -> _Delivery | None:
    """One SSE event as a follower message, or ``None`` when it is not one we use.

    A ``journal`` entry must carry the ``id`` SPEC §8.9 requires,
    ``<epoch>:<seq>:<line hash>`` (the hash 32 lowercase hex characters, the
    seq at least 1); its epoch and seq come from that id. An entry without a
    valid id is ignored (``None``; the reader logs it), so the resume point
    sent as ``Last-Event-ID`` is always a pinned one.
    """
    try:
        data = json.loads(event.data) if event.data else {}
    except ValueError:
        data = {}
    if not isinstance(data, dict):
        data = {}
    epoch = data.get("epoch")
    epoch = str(epoch) if epoch is not None else None
    if event.event == "heartbeat":
        head = _as_int(data.get("head_seq"))
        return None if head is None else _Heartbeat(epoch, head)
    if event.event == "reset":
        return _Reset(epoch)
    if event.event == "journal":
        match = _ENTRY_ID.fullmatch(event.id or "")
        if match is None or int(match.group(2)) < 1:
            return None
        return _Entry(match.group(1), int(match.group(2)))
    return None


# ---------------------------------------------------------------------------
# The follower
# ---------------------------------------------------------------------------


StreamOpener = Callable[..., StreamConnection]


class Follower:
    """Hold *project*'s stream on *remote* and keep *hosted_root*'s cache caught up.

    *catch_up* is the cache syncer's one-sync function (default: H-10b's
    :func:`lattice.remote.cache.catch_up`).
    *on_sync* runs in the control loop after every sync, with its outcome.
    *on_reset* runs when the follower learns of a reset or a new epoch, before
    the full resync it triggers.
    *on_notice* receives one line when the stream's state changes (connected,
    failed with a new error), for a foreground follower to print.
    *heartbeat_seconds* overrides the value read from ``/v1/info``.
    """

    def __init__(
        self,
        hosted_root: Path,
        remote: Remote,
        project: str,
        *,
        catch_up: CatchUp | None = None,
        on_sync: Callable[[SyncOutcome], None] | None = None,
        on_reset: Callable[[], None] | None = None,
        on_notice: Callable[[str], None] | None = None,
        heartbeat_seconds: float | None = None,
        max_backoff: float = MAX_BACKOFF_SECONDS,
        stream_opener: StreamOpener = open_stream,
        info_getter: Callable[..., dict[str, Any]] = get_info,
        initial_sync: bool = True,
    ) -> None:
        self.hosted_root = Path(hosted_root)
        self.remote = remote
        self.project = project
        self._catch_up = catch_up if catch_up is not None else cache.catch_up
        self._on_sync = on_sync
        self._on_reset = on_reset
        self._on_notice = on_notice
        self._heartbeat_override = heartbeat_seconds
        self.max_backoff = max_backoff
        self._open_stream = stream_opener
        self._get_info = info_getter
        self._initial_sync = initial_sync

        self.heartbeat_seconds = heartbeat_seconds or DEFAULT_HEARTBEAT_SECONDS
        self._queue: queue.Queue[_Delivery | _Down] = queue.Queue()
        self._stop = threading.Event()
        self._conn_lock = threading.Lock()
        self._conn: StreamConnection | None = None
        self._reader: threading.Thread | None = None

        # Control-loop state (touched only by the thread in run()).
        self.announced = 0
        self.epoch: str | None = None
        self.cache_head: int | None = None
        self.last_sync_ok = False
        self.polling = False
        self._dirty = False
        self._live_until: datetime | None = None
        self._last_delivery = 0.0
        self._next_poll = 0.0
        self._sync_not_before = 0.0
        self._sync_backoff = 0.0

        # Observability for callers and tests.
        self.syncs = 0
        self.last_stream_error: OpError | None = None
        self.last_sync_error: str | None = None
        self.stream_connects = 0
        self.deliveries = {"journal": 0, "heartbeat": 0, "reset": 0}
        self.ignored_entries = 0
        self._last_notice: str | None = None

    # -- public -------------------------------------------------------------

    def stop(self) -> None:
        """Ask :meth:`run` to return (thread-safe; safe from a signal handler)."""
        self._stop.set()
        self._queue.put(_Down(OpError("STOPPED", "The follower was stopped.")))
        with self._conn_lock:
            conn = self._conn
        if conn is not None:
            conn.close()

    @property
    def stopped(self) -> bool:
        return self._stop.is_set()

    def run(self) -> None:
        """Follow until :meth:`stop`; then clear ``stream_live_until`` and return."""
        try:
            self._read_heartbeat_seconds()
            if self._initial_sync and not self._stop.is_set():
                self._sync()
            self._last_delivery = time.monotonic()
            self._reader = threading.Thread(
                target=self._read_stream, name="lattice-follower-stream", daemon=True
            )
            self._reader.start()
            self._loop()
        finally:
            self._stop.set()
            self._clear(final=True)
            with self._conn_lock:
                conn = self._conn
            if conn is not None:
                conn.close()

    # -- control loop ---------------------------------------------------------

    def _read_heartbeat_seconds(self) -> None:
        if self._heartbeat_override:
            return
        try:
            info = self._get_info(self.remote)
        except OpError as exc:
            if exc.code != "SERVER_UNREACHABLE":
                raise
            self._notice(f"lattice: cannot reach {self.remote.alias}; will keep trying")
            return
        value = info.get("stream_heartbeat_seconds")
        if isinstance(value, (int, float)) and not isinstance(value, bool) and value > 0:
            self.heartbeat_seconds = float(value)

    @property
    def silence_seconds(self) -> float:
        return 2 * self.heartbeat_seconds

    def _loop(self) -> None:
        while not self._stop.is_set():
            now = time.monotonic()
            if not self.polling and now - self._last_delivery >= self.silence_seconds:
                self.polling = True
                self._clear()
                self._next_poll = now
            can_sync = now >= self._sync_not_before
            if can_sync and self.polling and now >= self._next_poll:
                self._sync()
                self._next_poll = time.monotonic() + self.heartbeat_seconds
                continue
            if can_sync and self._dirty:
                self._drain()
                self._sync_for_stream()
                continue
            deadline = (
                self._next_poll if self.polling else (self._last_delivery + self.silence_seconds)
            )
            if not can_sync:
                deadline = min(deadline, self._sync_not_before)
            try:
                message = self._queue.get(timeout=max(0.0, min(deadline - now, 0.5)))
            except queue.Empty:
                continue
            delivered = self._handle(message)
            delivered = self._drain() or delivered
            if self._dirty:
                if time.monotonic() >= self._sync_not_before:
                    self._sync_for_stream()
            elif delivered:
                self._maybe_extend()

    def _drain(self) -> bool:
        """Handle every queued message; True when any was a delivery."""
        delivered = False
        while True:
            try:
                message = self._queue.get_nowait()
            except queue.Empty:
                return delivered
            delivered = self._handle(message) or delivered

    def _handle(self, message: _Delivery | _Down) -> bool:
        if isinstance(message, _Down):
            return False
        self._last_delivery = time.monotonic()
        if self.polling:
            self.polling = False
        if isinstance(message, _Reset):
            self._new_epoch(message.epoch, 0)
            return True
        if message.epoch is not None and self.epoch is not None and message.epoch != self.epoch:
            # A new epoch without a reset event first: the same full resync.
            self._new_epoch(message.epoch, 0)
        elif message.epoch is not None:
            self.epoch = message.epoch
        seq = message.seq if isinstance(message, _Entry) else message.head_seq
        if seq > self.announced:
            self.announced = seq
        if self.cache_head is None or self.cache_head < self.announced:
            # The cache is known to be behind: withdraw freshness until a sync
            # brings it up to what the server announced.
            self._clear()
            self._dirty = True
        elif not self.last_sync_ok:
            self._dirty = True
        return True

    def _new_epoch(self, epoch: str | None, announced: int) -> None:
        self.epoch = epoch
        self.announced = announced
        self.cache_head = None
        self._dirty = True
        self._clear()
        if self._on_reset is not None:
            self._on_reset()

    def _sync_for_stream(self) -> None:
        self._dirty = False
        outcome = self._sync()
        if not succeeded(outcome):
            return
        # Count every announcement that arrived while the sync ran before
        # deciding anything: the sync may have snapshotted an older head.
        self._drain()
        if self.cache_head is not None and self.cache_head < self.announced:
            # The server moved on while we synced: stay unfresh and go again,
            # but only after a sync that made progress or a new announcement,
            # so a lagging server cannot spin us.
            self._clear()
            self._dirty = self._dirty or outcome.kind == "applied"
        else:
            self._maybe_extend()

    def _sync(self) -> SyncOutcome:
        """One catch-up; clears ``stream_live_until`` at once when it fails."""
        self.syncs += 1
        hard = False
        try:
            outcome = self._catch_up(self.hosted_root, bulk=True)
        except OpError as exc:
            if exc.code in FATAL_SYNC_CODES:
                self._clear()
                raise
            hard = True
            self.last_sync_error = f"{exc.code}: {exc.message}"
            outcome = SyncOutcome("unreachable", self.cache_head, None, self.last_sync_error)
        except Exception as exc:  # noqa: BLE001 - any other failure is a failed sync
            hard = True
            self.last_sync_error = f"{exc.__class__.__name__}: {exc}"
            outcome = SyncOutcome("unreachable", self.cache_head, None, self.last_sync_error)
        else:
            ok = succeeded(outcome)
            self.last_sync_error = None if ok else (outcome.detail or outcome.kind)
        if hard:
            # A hard error will not clear by itself at heartbeat pace: back off.
            self._sync_backoff = min(
                max(self._sync_backoff * 2, self.heartbeat_seconds), self.max_backoff
            )
            self._sync_not_before = time.monotonic() + self._sync_backoff
        elif succeeded(outcome):
            self._sync_backoff = 0.0
            self._sync_not_before = 0.0
        was_ok = self.last_sync_ok
        self.last_sync_ok = succeeded(outcome)
        if self.last_sync_ok:
            if outcome.head_seq is not None:
                self.cache_head = outcome.head_seq
        else:
            self._clear()
            if was_ok or self.syncs == 1:
                self._notice(
                    f"lattice: sync from {self.remote.alias} failed ({self.last_sync_error}); "
                    "reads on this machine catch up on their own until it recovers"
                )
        if self._on_sync is not None:
            self._on_sync(outcome)
        return outcome

    def _maybe_extend(self) -> None:
        """Extend ``stream_live_until`` if the cache holds everything announced.

        Queued deliveries are counted first, so an announcement that arrived
        a moment ago is never missed.
        """
        self._drain()
        if self.polling or not self.last_sync_ok or self.cache_head is None:
            return
        if self.cache_head < self.announced:
            return
        until = datetime.now(timezone.utc) + timedelta(seconds=self.silence_seconds)
        self._write(until)

    def _clear(self, *, final: bool = False) -> None:
        if self._live_until is None and not final:
            return
        if final:
            current = read_follower(self.hosted_root)
            if current is not None and current.get("pid") != os.getpid():
                return  # another follower owns the file now
            if current is None and self._live_until is None:
                return
        self._write(None)

    def _write(self, until: datetime | None) -> None:
        self._live_until = until
        record = {"pid": os.getpid(), "stream_live_until": _iso(until) if until else None}
        path = follower_path(self.hosted_root)
        data = (json.dumps(record, sort_keys=True, indent=2) + "\n").encode("utf-8")
        try:
            # Through real directories only, never a symlink (cache_paths).
            lattice_fd = cache_paths.open_dir(self.hosted_root, LATTICE_DIR, create=False)
            try:
                cache_fd = cache_paths.open_child(lattice_fd, "cache", path.parent)
            finally:
                os.close(lattice_fd)
            try:
                cache_paths.write_file(cache_fd, FOLLOWER_JSON, data)
            finally:
                os.close(cache_fd)
        except OSError as exc:
            self._notice(f"lattice: cannot write {path}: {exc}")

    @property
    def stream_live_until(self) -> datetime | None:
        return self._live_until

    # -- reader thread ----------------------------------------------------------

    def _notice(self, line: str) -> None:
        if self._on_notice is not None and line != self._last_notice:
            self._last_notice = line
            self._on_notice(line)

    def _read_stream(self) -> None:
        last_event_id: str | None = None
        initial = min(1.0, self.heartbeat_seconds)
        backoff = initial
        while not self._stop.is_set():
            delivered = False
            try:
                conn = self._open_stream(
                    self.remote,
                    self.project,
                    last_event_id=last_event_id,
                    timeout=self.silence_seconds + 1.0,
                )
                with self._conn_lock:
                    self._conn = conn
                if self._stop.is_set():
                    conn.close()
                    return
                self.stream_connects += 1
                for event in conn.events():
                    message = parse_stream_event(event)
                    if message is None:
                        if event.event == "journal":
                            self.ignored_entries += 1
                            self._notice(
                                f"lattice: ignored a stream entry from {self.remote.alias} "
                                f"without a valid id ({event.id!r}); syncing covers it"
                            )
                        continue
                    if not delivered:
                        delivered = True
                        backoff = initial
                        self.last_stream_error = None
                        self._notice(f"lattice: following {self.remote.alias}")
                    self.deliveries[event.event] += 1
                    if isinstance(message, _Reset):
                        last_event_id = None
                    elif isinstance(message, _Entry) and event.id:
                        last_event_id = event.id
                    self._queue.put(message)
                    if self._stop.is_set():
                        return
                error = OpError("SERVER_UNREACHABLE", "The stream closed.")
            except OpError as exc:
                error = exc
            except Exception as exc:  # noqa: BLE001 - the reader must never die silently
                error = OpError("SERVER_UNREACHABLE", f"{exc.__class__.__name__}: {exc}")
            finally:
                with self._conn_lock:
                    self._conn = None
            if self._stop.is_set():
                return
            self.last_stream_error = error
            self._queue.put(_Down(error))
            self._notice(
                f"lattice: stream from {self.remote.alias} unavailable ({error.code}); polling"
            )
            if self._stop.wait(backoff):
                return
            backoff = min(backoff * 2, self.max_backoff)
