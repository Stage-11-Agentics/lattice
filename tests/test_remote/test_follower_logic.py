"""The follower's rules, driven by a scripted stream (no network)."""

from __future__ import annotations

import json
import os
import threading
import time
from datetime import datetime, timedelta, timezone

import pytest

from lattice.core.errors import OpError
from lattice.remote.cache import SyncOutcome
from lattice.remote.follower import (
    MAX_BACKOFF_SECONDS,
    Follower,
    _Entry,
    _Heartbeat,
    _Reset,
    follower_path,
    live_follower,
    parse_stream_event,
    read_follower,
)
from lattice.remote.sse import SSEEvent, SSEParser
from tests.test_remote.stream_stub import SLUG, stub_remote, wait_for


def _feed(parser: SSEParser, text: str) -> list[SSEEvent]:
    out = []
    for line in text.split("\n"):
        event = parser.feed(line)
        if event is not None:
            out.append(event)
    return out


def test_sse_parser_reads_the_spec_shapes() -> None:
    text = (
        ": comment\n"
        'id: ep_1:3:abc\nevent: journal\ndata: {"seq": 3}\n\n'
        'event: heartbeat\ndata: {"epoch": "ep_1", "head_seq": 3}\n\n'
        'event: reset\ndata:{"epoch":"ep_2"}\n\n'
        "data: a\ndata: b\n\n"
        "event: empty\n\n"
    )
    events = _feed(SSEParser(), text)
    assert [(e.event, e.id) for e in events] == [
        ("journal", "ep_1:3:abc"),
        ("heartbeat", None),
        ("reset", None),
        ("message", None),
    ]
    assert events[3].data == "a\nb"


def test_parse_stream_event() -> None:
    good = "ep_1:7:0123456789abcdef0123456789abcdef"
    assert parse_stream_event(SSEEvent("journal", '{"seq": 9}', good)) == _Entry("ep_1", 7)
    # SPEC §8.9: every journal entry carries a valid id; nothing falls back to data.seq.
    for bad in (
        None,
        "",
        "ep_1:7",
        "ep_1:7:abc",
        "ep_1:x:0123456789abcdef0123456789abcdef",
        "ep_1:0:0123456789abcdef0123456789abcdef",
        ":7:0123456789abcdef0123456789abcdef",
        "ep_1:7:0123456789ABCDEF0123456789ABCDEF",
        "ep_1:7:0123456789abcdef0123456789abcdef:extra",
        "ep_1:\u00b2:0123456789abcdef0123456789abcdef",
    ):
        assert parse_stream_event(SSEEvent("journal", '{"seq": 9}', bad)) is None, bad
    assert parse_stream_event(
        SSEEvent("heartbeat", '{"epoch": "ep_1", "head_seq": 4}', None)
    ) == _Heartbeat("ep_1", 4)
    assert parse_stream_event(SSEEvent("reset", '{"epoch": "ep_2"}', None)) == _Reset("ep_2")
    assert parse_stream_event(SSEEvent("heartbeat", "{}", None)) is None
    assert parse_stream_event(SSEEvent("heartbeat", "not json", None)) is None
    assert parse_stream_event(SSEEvent("other", "{}", None)) is None


def _write_record(root, record) -> None:
    path = follower_path(root)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(record))


def test_live_follower_reader(tmp_path) -> None:
    future = (datetime.now(timezone.utc) + timedelta(seconds=30)).isoformat()
    past = (datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat()
    assert not live_follower(tmp_path)  # no file
    _write_record(tmp_path, {"pid": os.getpid(), "stream_live_until": future})
    assert live_follower(tmp_path)
    _write_record(tmp_path, {"pid": os.getpid(), "stream_live_until": past})
    assert not live_follower(tmp_path)
    _write_record(tmp_path, {"pid": os.getpid(), "stream_live_until": None})
    assert not live_follower(tmp_path)
    _write_record(tmp_path, {"pid": 2**22 + 12345, "stream_live_until": future})
    assert not live_follower(tmp_path)  # dead pid
    _write_record(tmp_path, {"pid": True, "stream_live_until": future})
    assert not live_follower(tmp_path)
    follower_path(tmp_path).write_text("{torn")
    assert not live_follower(tmp_path)


class ScriptedStream:
    """A stream connection yielding scripted events, then blocking until closed."""

    def __init__(self, events: list[SSEEvent]) -> None:
        self._events = list(events)
        self._closed = threading.Event()
        self.more: list[SSEEvent] = []
        self.cond = threading.Condition()

    def push(self, event: SSEEvent) -> None:
        with self.cond:
            self.more.append(event)
            self.cond.notify_all()

    def events(self):
        yield from self._events
        while not self._closed.is_set():
            with self.cond:
                self.cond.wait(0.05)
                pending, self.more = self.more, []
            yield from pending

    def close(self) -> None:
        self._closed.set()


def _hb(head: int, epoch: str = "ep_1") -> SSEEvent:
    return SSEEvent("heartbeat", json.dumps({"epoch": epoch, "head_seq": head}), None)


def _entry(seq: int, epoch: str = "ep_1") -> SSEEvent:
    return SSEEvent("journal", json.dumps({"seq": seq}), f"{epoch}:{seq}:{seq:032x}")


class FakeCache:
    def __init__(self, head: int = 0, delay: float = 0.0) -> None:
        self.server_head = head
        self.head = head
        self.calls = 0
        self.delay = delay
        self.fail = False

    def __call__(self, root, *, bulk=False) -> SyncOutcome:
        self.calls += 1
        time.sleep(self.delay)
        if self.fail:
            return SyncOutcome("unreachable", self.head, None)
        changed = self.head != self.server_head
        self.head = self.server_head
        return SyncOutcome("applied" if changed else "unchanged", self.head, "t")


def _run(follower: Follower):
    thread = threading.Thread(target=follower.run, daemon=True)
    thread.start()
    return thread


def _follower(tmp_path, cache, stream, **kwargs) -> Follower:
    (tmp_path / ".lattice").mkdir(exist_ok=True)
    return Follower(
        tmp_path,
        stub_remote("http://127.0.0.1:1"),
        SLUG,
        catch_up=cache,
        heartbeat_seconds=kwargs.pop("heartbeat_seconds", 0.5),
        stream_opener=lambda *a, **k: stream,
        **kwargs,
    )


def test_heartbeat_at_the_cache_head_extends_without_a_sync(tmp_path) -> None:
    cache = FakeCache(head=5)
    stream = ScriptedStream([_hb(5)])
    follower = _follower(tmp_path, cache, stream)
    thread = _run(follower)
    try:
        assert wait_for(lambda: live_follower(tmp_path), 1)
        calls = cache.calls  # the initial catch-up only
        assert calls == 1
        first = read_follower(tmp_path)["stream_live_until"]
        time.sleep(0.02)
        stream.push(_hb(5))
        assert wait_for(lambda: read_follower(tmp_path)["stream_live_until"] != first, 1)
        assert cache.calls == calls
    finally:
        follower.stop()
        thread.join(2)
    assert read_follower(tmp_path) == {"pid": os.getpid(), "stream_live_until": None}


def test_entry_ahead_syncs_before_extending(tmp_path) -> None:
    cache = FakeCache(head=1)
    stream = ScriptedStream([_hb(1)])
    follower = _follower(tmp_path, cache, stream)
    thread = _run(follower)
    try:
        assert wait_for(lambda: live_follower(tmp_path), 1)
        cache.server_head = 2
        stream.push(_entry(2))
        assert wait_for(lambda: cache.head == 2, 1)
        assert follower.announced == 2 and follower.cache_head == 2
        assert wait_for(lambda: live_follower(tmp_path), 1)
    finally:
        follower.stop()
        thread.join(2)


def test_entries_during_a_sync_coalesce_into_one_more_sync(tmp_path) -> None:
    cache = FakeCache(head=0, delay=0.3)
    stream = ScriptedStream([_hb(0)])
    follower = _follower(tmp_path, cache, stream, heartbeat_seconds=2.0)
    thread = _run(follower)
    try:
        assert wait_for(lambda: live_follower(tmp_path), 2)
        base = cache.calls
        cache.server_head = 1
        stream.push(_entry(1))
        assert wait_for(lambda: cache.calls == base + 1, 1)  # sync in flight
        for seq in range(2, 12):
            cache.server_head = seq
            stream.push(_entry(seq))
        assert wait_for(lambda: cache.head == 11 and follower.cache_head == 11, 3)
        assert cache.calls <= base + 3, cache.calls  # not one per entry
        assert wait_for(lambda: live_follower(tmp_path), 1)
    finally:
        follower.stop()
        thread.join(3)


def test_heartbeat_ahead_of_cache_waits_for_the_sync(tmp_path) -> None:
    cache = FakeCache(head=3)
    cache.fail = True
    stream = ScriptedStream([_hb(3)])
    follower = _follower(tmp_path, cache, stream)
    thread = _run(follower)
    try:
        time.sleep(0.2)
        assert not live_follower(tmp_path)  # last sync failed: no extension
        cache.fail = False
        cache.server_head = 4
        stream.push(_hb(4))
        assert wait_for(lambda: live_follower(tmp_path), 1)
        assert cache.head == 4
    finally:
        follower.stop()
        thread.join(2)


def test_heartbeat_on_a_new_epoch_resyncs(tmp_path) -> None:
    cache = FakeCache(head=7)
    stream = ScriptedStream([_hb(7)])
    follower = _follower(tmp_path, cache, stream)
    thread = _run(follower)
    try:
        assert wait_for(lambda: live_follower(tmp_path), 1)
        calls = cache.calls
        cache.server_head = 1
        stream.push(_hb(1, epoch="ep_2"))
        assert wait_for(lambda: cache.calls > calls and cache.head == 1, 1)
        assert follower.epoch == "ep_2" and follower.announced == 1
        assert wait_for(lambda: live_follower(tmp_path), 1)
    finally:
        follower.stop()
        thread.join(2)


def test_reconnect_backoff_doubles_and_caps_at_60s(tmp_path) -> None:
    assert MAX_BACKOFF_SECONDS == 60.0
    waits: list[float] = []

    def refuse(*args, **kwargs):
        raise OpError("SERVER_UNREACHABLE", "refused")

    follower = Follower(
        tmp_path,
        stub_remote("http://127.0.0.1:1"),
        SLUG,
        catch_up=FakeCache(),
        heartbeat_seconds=2.0,
        stream_opener=refuse,
    )
    real_wait = follower._stop.wait

    def fake_wait(timeout=None):
        waits.append(timeout)
        if len(waits) >= 10:
            follower._stop.set()
        return real_wait(0)

    follower._stop.wait = fake_wait  # type: ignore[method-assign]
    follower._read_stream()
    assert waits == [1.0, 2.0, 4.0, 8.0, 16.0, 32.0, 60.0, 60.0, 60.0, 60.0]


def test_info_sets_heartbeat_seconds_and_rejection_is_fatal(tmp_path) -> None:
    follower = Follower(
        tmp_path,
        stub_remote("http://127.0.0.1:1"),
        SLUG,
        catch_up=FakeCache(),
        info_getter=lambda ep: {"stream_heartbeat_seconds": 3},
    )
    follower._read_heartbeat_seconds()
    assert follower.heartbeat_seconds == 3.0 and follower.silence_seconds == 6.0

    def rejected(ep):
        raise OpError("PROXY_REJECTED", "login page")

    follower = Follower(
        tmp_path,
        stub_remote("http://127.0.0.1:1"),
        SLUG,
        catch_up=FakeCache(),
        info_getter=rejected,
    )
    try:
        follower.run()
    except OpError as exc:
        assert exc.code == "PROXY_REJECTED"
    else:
        raise AssertionError("expected PROXY_REJECTED")


def test_stop_does_not_clear_another_followers_record(tmp_path) -> None:
    future = (datetime.now(timezone.utc) + timedelta(seconds=30)).isoformat()
    follower = _follower(tmp_path, FakeCache(), ScriptedStream([]))
    _write_record(tmp_path, {"pid": 1, "stream_live_until": future})
    follower._clear(final=True)
    assert read_follower(tmp_path) == {"pid": 1, "stream_live_until": future}


class LatchedCache(FakeCache):
    """A cache whose next sync snapshots the server head, then blocks on a latch."""

    def __init__(self, root, head: int = 0) -> None:
        super().__init__(head=head)
        self.root = root
        self.latch = threading.Event()
        self.entered = threading.Event()
        self.latched = False
        self.seen_at_entry: list[object] = []

    def __call__(self, root, *, bulk=False) -> SyncOutcome:
        self.calls += 1
        self.seen_at_entry.append((read_follower(self.root) or {}).get("stream_live_until"))
        snapshot = self.server_head
        if self.latched:
            self.latched = False
            self.entered.set()
            self.latch.wait(2)
        self.head = snapshot
        return SyncOutcome("applied", snapshot, "t")


def test_announcement_during_a_sync_is_counted_before_extending(tmp_path) -> None:
    """Sync N snapshots head 1; entry 2 arrives while it applies. The follower
    must not write a fresh stream_live_until for head 1 (review finding 1)."""
    cache = LatchedCache(tmp_path, head=0)
    stream = ScriptedStream([_hb(0)])
    follower = _follower(tmp_path, cache, stream, heartbeat_seconds=2.0)
    thread = _run(follower)
    try:
        assert wait_for(lambda: live_follower(tmp_path), 1)
        cache.server_head = 1
        cache.latched = True
        stream.push(_entry(1))
        assert cache.entered.wait(1)  # sync N is in flight, snapshotted head 1
        assert read_follower(tmp_path)["stream_live_until"] is None  # entry 1 withdrew it
        cache.server_head = 2
        stream.push(_entry(2))
        time.sleep(0.15)  # entry 2 is queued while sync N applies
        written: list[object] = []
        cache.latch.set()
        # The next sync starts with freshness still withheld: head 1 < announced 2.
        assert wait_for(lambda: len(cache.seen_at_entry) >= 3, 1)
        written.append(cache.seen_at_entry[2])
        assert written == [None], cache.seen_at_entry
        assert wait_for(lambda: follower.cache_head == 2 and live_follower(tmp_path), 1)
    finally:
        cache.latch.set()
        follower.stop()
        thread.join(2)


class RaisingCache(FakeCache):
    def __init__(self, error: OpError, head: int = 0) -> None:
        super().__init__(head=head)
        self.error = error
        self.raising = False
        self.times: list[float] = []

    def __call__(self, root, *, bulk=False) -> SyncOutcome:
        if self.raising:
            self.calls += 1
            self.times.append(time.monotonic())
            raise self.error
        return super().__call__(root)


@pytest.mark.parametrize("code", ["PROXY_REJECTED", "UNAUTHENTICATED"])
def test_fatal_sync_error_ends_the_follower_with_it(tmp_path, code) -> None:
    cache = RaisingCache(OpError(code, "no"), head=0)
    stream = ScriptedStream([_hb(0)])
    follower = _follower(tmp_path, cache, stream, heartbeat_seconds=0.2)
    failure: list[BaseException] = []

    def run() -> None:
        try:
            follower.run()
        except BaseException as exc:  # noqa: BLE001
            failure.append(exc)

    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    try:
        assert wait_for(lambda: live_follower(tmp_path), 1)
        cache.raising = True
        stream.push(_entry(1))
        thread.join(2)
        assert not thread.is_alive()
        assert isinstance(failure[0], OpError) and failure[0].code == code
        assert read_follower(tmp_path)["stream_live_until"] is None
    finally:
        follower.stop()
        thread.join(2)


def test_nonfatal_hard_error_clears_and_backs_off(tmp_path) -> None:
    error = OpError("INTEGRITY_ERROR", "delta rejected", {"reason": "HASH_MISMATCH"})
    cache = RaisingCache(error, head=0)
    stream = ScriptedStream([_hb(0)])
    follower = _follower(tmp_path, cache, stream, heartbeat_seconds=0.1, max_backoff=0.4)
    thread = _run(follower)
    try:
        assert wait_for(lambda: live_follower(tmp_path), 1)
        cache.raising = True
        for seq in range(1, 30):  # a steady stream of triggers
            stream.push(_entry(seq))
            time.sleep(0.05)
            assert not live_follower(tmp_path)
        assert thread.is_alive()
        gaps = [b - a for a, b in zip(cache.times, cache.times[1:])]
        assert len(cache.times) >= 3
        assert gaps[-1] >= 0.35, gaps  # backed off to the cap, not once per entry
        assert follower.last_sync_error.startswith("INTEGRITY_ERROR")
        cache.raising = False
        cache.server_head = 29
        stream.push(_hb(29))
        assert wait_for(lambda: live_follower(tmp_path), 5)
    finally:
        follower.stop()
        thread.join(2)


class FiniteStream:
    def __init__(self, events: list[SSEEvent]) -> None:
        self._events = events

    def events(self):
        yield from self._events

    def close(self) -> None:
        pass


def test_journal_entries_without_a_valid_id_are_ignored_and_logged(tmp_path) -> None:
    """An id-less or malformed entry is neither a delivery nor a resume point."""
    valid = _entry(3)
    first = FiniteStream(
        [
            SSEEvent("journal", '{"seq": 4}', None),
            valid,
            SSEEvent("journal", '{"seq": 5}', "ep_1:5:nothex"),
        ]
    )
    resumes: list[object] = []
    streams = [first]

    def opener(remote, project, *, last_event_id, timeout):
        resumes.append(last_event_id)
        if streams:
            return streams.pop(0)
        raise OpError("SERVER_UNREACHABLE", "gone")

    notices: list[str] = []
    (tmp_path / ".lattice").mkdir()
    follower = Follower(
        tmp_path,
        stub_remote("http://127.0.0.1:1"),
        SLUG,
        catch_up=FakeCache(),
        heartbeat_seconds=0.05,
        stream_opener=opener,
        on_notice=notices.append,
    )
    real_wait = follower._stop.wait

    def fake_wait(timeout=None):
        if len(resumes) >= 2:
            follower._stop.set()
        return real_wait(0)

    follower._stop.wait = fake_wait  # type: ignore[method-assign]
    follower._read_stream()
    assert follower.deliveries["journal"] == 1
    assert follower.ignored_entries == 2
    # The reconnect resumes from the one valid id, never from an unpinned one.
    assert resumes == [None, valid.id]
    assert any("without a valid id" in line for line in notices)
    queued = []
    while not follower._queue.empty():
        queued.append(follower._queue.get_nowait())
    assert [m for m in queued if isinstance(m, _Entry)] == [_Entry("ep_1", 3)]
