"""AC-7 and AC-45: freshness is applied syncs, whatever the stream does.

H-11's part (at the end): with no follower, a checkout's very next command
reflects another client's write; a killed follower's leftover
``cache/follower.json`` is ignored (SPEC §9.5).

A writes to H-10a's real server while B follows it, syncing with H-10b's real
``catch_up``; the stream goes direct or through a test proxy that blocks,
drops, refuses, or buffers it. Only the every-sync-fails case uses the stream
stub, since a real server cannot be made to fail each sync. Every server
listens on ``127.0.0.1:0`` with 0.2 s heartbeats, so the 2 s and 5 s bounds
have room.
"""

from __future__ import annotations

import contextlib
import json
import os
import signal
import subprocess
import sys
import threading
import time
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from lattice.remote import cache
from lattice.remote.follower import Follower, live_follower, read_follower
from lattice.remote.http import Remote
from lattice.server.testing import BoardServer, serve_board
from tests.test_remote.conftest import bind
from tests.test_remote.follower_support import following
from tests.test_remote.hosted import HostedEnv, make_repo, run_cli
from tests.test_remote.stream_stub import StubSyncer, TestProxy, wait_for


@pytest.fixture
def server(tmp_path) -> Iterator[BoardServer]:
    with serve_board(tmp_path / "server", heartbeat_seconds=0.2) as srv:
        yield srv


@contextlib.contextmanager
def follow(root: Path, url: str, srv: BoardServer, monkeypatch, **kwargs) -> Iterator[Follower]:
    """B bound to *url* (the server, or a proxy in front of it), followed with the
    real ``catch_up``, in a thread; stopped and joined on exit."""
    bind(root, url, srv.token, monkeypatch)
    remote = Remote(alias="team", url=url, token=srv.token)
    follower = Follower(root, remote, srv.slug, catch_up=cache.catch_up, max_backoff=1.0, **kwargs)
    thread = threading.Thread(target=follower.run, daemon=True)
    thread.start()
    try:
        yield follower
    finally:
        follower.stop()
        thread.join(timeout=5)
        assert not thread.is_alive(), "follower did not stop"


def _write(srv: BoardServer, n: int) -> Path:
    """A creates a task on the server; returns its snapshot's path under the board."""
    task = srv.op("task.create", {"title": f"write {n}"})["task"]
    return Path("tasks") / f"{task['id']}.json"


def _reflects(root: Path, srv: BoardServer, rel: Path):
    def check() -> bool:
        try:
            return (root / ".lattice" / rel).read_bytes() == (srv.board / rel).read_bytes()
        except FileNotFoundError:
            return False

    return check


def _live(root) -> bool:
    return live_follower(root)


def test_following_client_sees_a_write_within_2s(tmp_path, server, monkeypatch) -> None:
    root = tmp_path / "b"
    with follow(root, server.url, server, monkeypatch) as follower:
        assert wait_for(lambda: _live(root), 2), "follower never went live"
        for n in range(3):
            rel = _write(server, n)
            elapsed = wait_for(_reflects(root, server, rel), 2)
            assert elapsed is not None and elapsed < 2
        assert wait_for(lambda: _live(root), 2)
        assert follower.deliveries["journal"] >= 3
        assert not follower.polling
    # Stopped: stream_live_until is cleared.
    assert read_follower(root)["stream_live_until"] is None
    assert not live_follower(root)


def test_blocked_stream_converges_within_5s_by_polling(tmp_path, server, monkeypatch) -> None:
    root = tmp_path / "b"
    with TestProxy(server.url, "block").running() as proxy:
        with follow(root, proxy.url, server, monkeypatch) as follower:
            rel = _write(server, 1)
            elapsed = wait_for(_reflects(root, server, rel), 5)
            assert elapsed is not None and elapsed < 5
            assert wait_for(lambda: follower.polling, 2)
            assert follower.stream_live_until is None
            assert not _live(root)


def test_proxy_dropping_entries_still_converges_on_heartbeats(
    tmp_path, server, monkeypatch
) -> None:
    root = tmp_path / "b"
    with TestProxy(server.url, "drop_entries").running() as proxy:
        with follow(root, proxy.url, server, monkeypatch) as follower:
            assert wait_for(lambda: _live(root), 2)
            for n in range(3):
                rel = _write(server, n)
                elapsed = wait_for(_reflects(root, server, rel), 5)
                assert elapsed is not None and elapsed < 5
            # Heartbeats keep arriving, so this is the stream, not the poller.
            assert not follower.polling
            assert follower.deliveries["journal"] == 0
            assert follower.deliveries["heartbeat"] > 0
            assert wait_for(lambda: _live(root), 2)


def test_every_sync_failing_clears_stream_live_until_at_the_first_failure(
    tmp_path, stream_stub
) -> None:
    """Forced failures need the stream stub: the real server cannot fail each sync."""
    syncer = StubSyncer(stream_stub.url)
    seen: list[tuple[str, object]] = []

    def on_sync(outcome) -> None:
        record = read_follower(tmp_path) or {}
        seen.append((outcome.kind, record.get("stream_live_until")))

    with following(
        tmp_path, stream_stub.url, syncer, on_sync=on_sync, max_backoff=0.4
    ) as follower:
        assert wait_for(lambda: _live(tmp_path), 2)
        stream_stub.fail_sync = True
        content = stream_stub.files["events/T1.jsonl"] + b'{"id":"ev_1"}\n'
        stream_stub.write({"events/T1.jsonl": content}, [{"id": "ev_1"}])
        assert wait_for(
            lambda: any(status != "applied" and status != "unchanged" for status, _ in seen), 2
        )
        first_failure = next(
            i for i, (s, _) in enumerate(seen) if s not in ("applied", "unchanged")
        )
        assert seen[first_failure][1] is None, "not cleared at the first failed sync"
        assert follower.stream_live_until is None
        # It stays cleared while syncs keep failing, heartbeats notwithstanding.
        assert wait_for(lambda: _live(tmp_path), 0.5) is None
        # And recovers once syncs succeed again.
        stream_stub.fail_sync = False
        assert wait_for(lambda: _live(tmp_path), 3)


def _never_advances(tmp_path, srv: BoardServer, monkeypatch, mode: str) -> None:
    root = tmp_path / "b"
    with TestProxy(srv.url, mode).running() as proxy:
        with follow(root, proxy.url, srv, monkeypatch) as follower:
            for n in range(2):
                rel = _write(srv, n)
                elapsed = wait_for(_reflects(root, srv, rel), 5)
                assert elapsed is not None and elapsed < 5
                assert (read_follower(root) or {}).get("stream_live_until") is None
            assert follower.polling
            assert follower.stream_live_until is None
            assert not _live(root)
            assert follower.stream_connects == 0 or mode == "buffer"


def test_ac45_refused_stream_polls_and_never_advances(tmp_path, server, monkeypatch) -> None:
    _never_advances(tmp_path, server, monkeypatch, "refuse")


def test_ac45_buffered_stream_polls_and_never_advances(tmp_path, server, monkeypatch) -> None:
    _never_advances(tmp_path, server, monkeypatch, "buffer")


# ---------------------------------------------------------------------------
# H-11: no follower, and a killed follower
# ---------------------------------------------------------------------------


def _clones(env: HostedEnv, tmp_path: Path) -> tuple[Path, Path]:
    a, b = make_repo(tmp_path / "a"), make_repo(tmp_path / "b")
    for repo in (a, b):
        assert run_cli(repo, "remote", "attach", "team", "demo").exit_code == 0
    return a, b


def _titles(repo: Path) -> list[str]:
    result = run_cli(repo, "list", "--json")
    assert result.exit_code == 0, result.output
    return sorted(t["title"] for t in json.loads(result.stdout)["data"])


def test_the_very_next_command_sees_another_clients_write(
    hosted_env: HostedEnv, tmp_path: Path
) -> None:
    a, b = _clones(hosted_env, tmp_path)
    for n in range(3):
        started = time.monotonic()
        assert _titles(b) == [f"t{i}" for i in range(n)]
        assert run_cli(a, "create", f"t{n}", "--actor", "agent:a").exit_code == 0
        # B's next command, well within 100 ms of its previous one finishing.
        assert f"t{n}" in _titles(b)
        assert time.monotonic() - started < 5


def test_a_killed_followers_file_is_ignored(hosted_env: HostedEnv, tmp_path: Path) -> None:
    a, b = _clones(hosted_env, tmp_path)
    follower = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    try:
        record = {
            "pid": follower.pid,
            "stream_live_until": (datetime.now(UTC) + timedelta(minutes=5)).isoformat(),
        }
        path = b / ".lattice" / "cache" / "follower.json"
        path.write_text(json.dumps(record))
        assert run_cli(a, "create", "While followed", "--actor", "agent:a").exit_code == 0
        # A live follower is trusted: B reads its cache without a catch-up.
        assert _titles(b) == []
        os.kill(follower.pid, signal.SIGKILL)
        follower.wait(timeout=5)
        # The pid is dead: B catches up itself.
        assert _titles(b) == ["While followed"]
    finally:
        if follower.poll() is None:
            follower.kill()
            follower.wait(timeout=5)
