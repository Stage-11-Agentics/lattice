"""AC-7 (H-10b row), SPEC §8.8 supported size: a 2,000-task board whose 4 MiB
task log is appended every 200 ms, and a client behind a proxy throttled to
1 MiB/s syncing continuously. After the first sync every sync of the hot log
carries only its appended bytes, and the client matches the server within 5 s
of the writer stopping. Real server (``serve_board``), real board."""

from __future__ import annotations

import threading
import time
from pathlib import Path

import pytest

from lattice.remote import cache
from lattice.server.testing import serve_board
from tests.test_remote.conftest import bind, record_requests, tree_hashes
from tests.test_remote.proxies import tcp_proxy
from tests.torture.envelope import MIB, build_envelope

pytestmark = [pytest.mark.torture, pytest.mark.envelope, pytest.mark.timeout(300)]

WRITE_SECONDS = 8.0
APPEND_EVERY = 0.2


def test_continuous_sync_of_a_hot_log_carries_only_appended_bytes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = tmp_path / "source"
    hot = build_envelope(source, tasks=2000, hot_log_bytes=4 * MIB)
    hot_rel = f"events/{hot}.jsonl"
    with serve_board(tmp_path / "server", source=source) as server:
        with tcp_proxy(server.handle.port, bytes_per_second=MIB) as proxy:
            client = bind(
                tmp_path / "client", proxy.url, server.token, monkeypatch, project=server.slug
            )
            assert cache.catch_up(client, bulk=True).kind == "applied"
            assert tree_hashes(client / ".lattice") == tree_hashes(server.board)
            calls = record_requests(monkeypatch)

            appended = {"count": 0}
            stop = threading.Event()

            def writer() -> None:
                n = 0
                while not stop.is_set():
                    server.op("task.comment", {"task": hot, "text": f"live comment {n}"})
                    appended["count"] += 1
                    n += 1
                    time.sleep(APPEND_EVERY)

            thread = threading.Thread(target=writer, daemon=True)
            thread.start()
            received: list[int] = []
            started = time.monotonic()
            while time.monotonic() - started < WRITE_SECONDS:
                proxy.reset_counts()
                outcome = cache.catch_up(client)
                assert outcome.kind in ("applied", "unchanged"), outcome
                received.append(proxy.downstream_bytes)
            stop.set()
            thread.join(5)
            stopped = time.monotonic()
            while tree_hashes(client / ".lattice") != tree_hashes(server.board):
                assert time.monotonic() - stopped < 5.0, (
                    "no match within 5 s of the writer stopping"
                )
                cache.catch_up(client)
            converged = time.monotonic() - stopped

    bodies = [response.data() for path, response in calls if "/sync?" in path and response]
    hot_entries = [body["files"][hot_rel] for body in bodies if hot_rel in body["files"]]
    assert appended["count"] >= 10 and len(hot_entries) >= 10
    assert all("append_from" in entry for entry in hot_entries)  # never the whole 4 MiB log
    assert not [path for path, _ in calls if "/files/" in path]  # nothing fetched whole
    # Appends arrive at about 1 KiB/s while the hot log is 4 MiB: a sync that
    # re-sent the log, or anything beyond its new bytes, would dwarf this bound.
    assert all(size < 64 * 1024 for size in received), received
    print(f"syncs={len(received)} appends={appended['count']} converged_in={converged:.2f}s")
