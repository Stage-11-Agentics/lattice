"""AC-23 (the follower's part): a ``reset`` makes the follower resync in full."""

from __future__ import annotations

from lattice.remote.follower import live_follower
from tests.test_remote.follower_support import following
from tests.test_remote.stream_stub import StubSyncer, cache_files, wait_for


def test_reset_triggers_a_full_resync_and_ends_byte_identical(tmp_path, stream_stub) -> None:
    syncer = StubSyncer(stream_stub.url)
    with following(tmp_path, stream_stub.url, syncer) as follower:
        assert wait_for(lambda: live_follower(tmp_path), 5)
        stream_stub.write({"events/T1.jsonl": b'{"id":"ev_1"}\n'})
        assert wait_for(lambda: cache_files(tmp_path) == stream_stub.files, 5)
        resets_before = syncer.resets

        # The server's history is rebuilt: new epoch, different files, seq back to 0.
        stream_stub.rotate_epoch(
            {"config.json": b'{"project_code": "DEM", "v": 2}\n', "events/T2.jsonl": b"x\n"}
        )
        stream_stub.write({"events/T2.jsonl": b"x\ny\n"})

        # state.json is written last, so waiting on it too never sees a half-apply.
        def converged() -> bool:
            state = syncer.state(tmp_path)
            return (
                state.get("epoch") == stream_stub.epoch
                and state.get("head_seq") == stream_stub.head_seq
                and cache_files(tmp_path) == stream_stub.files
            )

        assert wait_for(converged, 5), cache_files(tmp_path)

        assert follower.deliveries["reset"] >= 1
        assert syncer.resets == resets_before + 1
        assert "events/T1.jsonl" not in cache_files(tmp_path)  # a full resync, not a delta
        assert syncer.state(tmp_path)["epoch"] == stream_stub.epoch
        assert wait_for(lambda: live_follower(tmp_path), 5)


def test_stale_resume_point_gets_reset_and_resyncs(tmp_path, stream_stub) -> None:
    """A reconnect whose Last-Event-ID the server no longer holds is answered
    with ``reset``; the follower resyncs from scratch."""
    syncer = StubSyncer(stream_stub.url)
    with following(tmp_path, stream_stub.url, syncer) as follower:
        assert wait_for(lambda: live_follower(tmp_path), 5)
        stream_stub.write({"events/T1.jsonl": b"a\n"})
        assert wait_for(lambda: cache_files(tmp_path) == stream_stub.files, 5)
        assert wait_for(lambda: follower.deliveries["journal"] >= 1, 5)
        # Drop the connection while the server restarts its history.
        with stream_stub.lock:
            subs = list(stream_stub.subscribers)
            stream_stub.subscribers.clear()
        stream_stub.rotate_epoch({"config.json": b"{}\n"})
        for sub in subs:
            sub.put(None)  # the server side closes the old stream
        assert wait_for(lambda: cache_files(tmp_path) == stream_stub.files, 5)
        resumed = [
            {k.lower(): v for k, v in h.items()}.get("last-event-id")
            for h in stream_stub.stream_requests[1:]
        ]
        assert any(r and r.startswith("ep_1:1:") for r in resumed), resumed
        assert follower.deliveries["reset"] >= 1
