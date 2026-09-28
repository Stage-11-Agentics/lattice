"""AC-23 (the follower's part): a ``reset`` makes the follower resync in full."""

from __future__ import annotations

import hashlib
import json
import threading
from pathlib import Path

from lattice.remote import cache
from lattice.remote.follower import Follower, live_follower
from lattice.remote.http import Remote
from lattice.server.testing import serve_board
from tests.test_remote.conftest import bind
from tests.test_remote.follower_support import following
from tests.test_remote.stream_stub import StubSyncer, cache_files, wait_for


def _hashes(lattice_dir: Path) -> dict[str, str]:
    return {
        rel: hashlib.sha256((lattice_dir / rel).read_bytes()).hexdigest()
        for rel in cache.synced_files(lattice_dir)
    }


def _epoch(root: Path) -> str | None:
    try:
        return json.loads((root / ".lattice" / "cache" / "state.json").read_text())["epoch"]
    except (OSError, ValueError, KeyError):
        return None


def test_reset_triggers_a_full_resync_and_ends_byte_identical(tmp_path, monkeypatch) -> None:
    """H-10a's real ``rotate-epoch`` broadcasts ``reset``; the follower resyncs in
    full with H-10b's real ``catch_up`` and ends byte-identical with the server."""
    with serve_board(tmp_path / "server", audit=False, heartbeat_seconds=0.2) as srv:
        root = bind(tmp_path / "b", srv.url, srv.token, monkeypatch)
        remote = Remote(alias="team", url=srv.url, token=srv.token)
        follower = Follower(root, remote, srv.slug, catch_up=cache.catch_up)
        thread = threading.Thread(target=follower.run, daemon=True)
        thread.start()
        try:
            assert wait_for(lambda: live_follower(root), 5)
            srv.op("task.create", {"title": "before rotation"})
            assert wait_for(lambda: _hashes(root / ".lattice") == _hashes(srv.board), 5)
            old_epoch = _epoch(root)

            srv.rotate_epoch()
            srv.op("task.create", {"title": "after rotation"})

            def converged() -> bool:
                return (
                    _epoch(root) not in (None, old_epoch)
                    and follower.cache_head == srv.sync()["head_seq"]
                    and _hashes(root / ".lattice") == _hashes(srv.board)
                )

            assert wait_for(converged, 5)
            assert follower.deliveries["reset"] >= 1
            assert wait_for(lambda: live_follower(root), 5)
        finally:
            follower.stop()
            thread.join(5)


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
