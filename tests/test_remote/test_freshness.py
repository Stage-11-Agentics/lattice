"""AC-7 (follower part) and AC-45: freshness is applied syncs, whatever the stream does.

A writes through the stream_stub server while B follows. Every server listens on
``127.0.0.1:0`` with 0.2 s heartbeats, so the 2 s and 5 s bounds have room.
"""

from __future__ import annotations

import json

from lattice.remote.follower import live_follower, read_follower
from tests.test_remote.follower_support import following
from tests.test_remote.stream_stub import (
    StubSyncer,
    TestProxy,
    cache_files,
    wait_for,
)


def _event(n: int) -> bytes:
    return (json.dumps({"id": f"ev_{n}", "type": "comment_added"}) + "\n").encode()


def _write(stream_stub, n: int) -> bytes:
    """Append event *n* to T1's log on the server; return the log's new content."""
    content = stream_stub.files["events/T1.jsonl"] + _event(n)
    stream_stub.write({"events/T1.jsonl": content}, [{"id": f"ev_{n}"}])
    return content


def _reflects(root, content: bytes):
    return lambda: cache_files(root).get("events/T1.jsonl") == content


def _live(root) -> bool:
    return live_follower(root)


def test_following_client_sees_a_write_within_2s(tmp_path, stream_stub) -> None:
    syncer = StubSyncer(stream_stub.url)
    with following(tmp_path, stream_stub.url, syncer) as follower:
        assert wait_for(lambda: _live(tmp_path), 2), "follower never went live"
        for n in range(3):
            content = _write(stream_stub, n)
            elapsed = wait_for(_reflects(tmp_path, content), 2)
            assert elapsed is not None and elapsed < 2
        assert wait_for(lambda: _live(tmp_path), 2)
        assert follower.deliveries["journal"] == 3
        assert not follower.polling
    # Stopped: stream_live_until is cleared.
    assert read_follower(tmp_path)["stream_live_until"] is None
    assert not live_follower(tmp_path)


def test_blocked_stream_converges_within_5s_by_polling(tmp_path, stream_stub) -> None:
    with TestProxy(stream_stub.url, "block").running() as proxy:
        syncer = StubSyncer(proxy.url)
        with following(tmp_path, proxy.url, syncer) as follower:
            content = _write(stream_stub, 1)
            elapsed = wait_for(_reflects(tmp_path, content), 5)
            assert elapsed is not None and elapsed < 5
            assert wait_for(lambda: follower.polling, 2)
            assert follower.stream_live_until is None
            assert not _live(tmp_path)


def test_proxy_dropping_entries_still_converges_on_heartbeats(tmp_path, stream_stub) -> None:
    with TestProxy(stream_stub.url, "drop_entries").running() as proxy:
        syncer = StubSyncer(proxy.url)
        with following(tmp_path, proxy.url, syncer) as follower:
            assert wait_for(lambda: _live(tmp_path), 2)
            for n in range(3):
                content = _write(stream_stub, n)
                elapsed = wait_for(_reflects(tmp_path, content), 5)
                assert elapsed is not None and elapsed < 5
            # Heartbeats keep arriving, so this is the stream, not the poller.
            assert not follower.polling
            assert follower.deliveries["journal"] == 0
            assert follower.deliveries["heartbeat"] > 0
            assert wait_for(lambda: _live(tmp_path), 2)


def test_every_sync_failing_clears_stream_live_until_at_the_first_failure(
    tmp_path, stream_stub
) -> None:
    syncer = StubSyncer(stream_stub.url)
    seen: list[tuple[str, object]] = []

    def on_sync(outcome) -> None:
        record = read_follower(tmp_path) or {}
        seen.append((outcome.kind, record.get("stream_live_until")))

    with following(tmp_path, stream_stub.url, syncer, on_sync=on_sync) as follower:
        assert wait_for(lambda: _live(tmp_path), 2)
        stream_stub.fail_sync = True
        _write(stream_stub, 1)
        assert wait_for(
            lambda: any(status != "applied" and status != "unchanged" for status, _ in seen), 2
        )
        first_failure = next(
            i for i, (s, _) in enumerate(seen) if s not in ("applied", "unchanged")
        )
        assert seen[first_failure][1] is None, "not cleared at the first failed sync"
        assert follower.stream_live_until is None
        # It stays cleared while syncs keep failing, heartbeats notwithstanding.
        assert wait_for(lambda: _live(tmp_path), 0.8) is None
        # And recovers once syncs succeed again.
        stream_stub.fail_sync = False
        assert wait_for(lambda: _live(tmp_path), 3)


def _never_advances(tmp_path, stream_stub, mode: str) -> None:
    with TestProxy(stream_stub.url, mode).running() as proxy:
        syncer = StubSyncer(proxy.url)
        with following(tmp_path, proxy.url, syncer) as follower:
            for n in range(2):
                content = _write(stream_stub, n)
                elapsed = wait_for(_reflects(tmp_path, content), 5)
                assert elapsed is not None and elapsed < 5
                assert (read_follower(tmp_path) or {}).get("stream_live_until") is None
            assert follower.polling
            assert follower.stream_live_until is None
            assert not _live(tmp_path)
            assert follower.stream_connects == 0 or mode == "buffer"


def test_ac45_refused_stream_polls_and_never_advances(tmp_path, stream_stub) -> None:
    _never_advances(tmp_path, stream_stub, "refuse")


def test_ac45_buffered_stream_polls_and_never_advances(tmp_path, stream_stub) -> None:
    _never_advances(tmp_path, stream_stub, "buffer")
