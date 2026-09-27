"""AC-7 (H-10b row), SPEC §8.8 supported size: a 2,000-task board whose 4 MiB
task log is appended every 200 ms, and a client behind a proxy throttled to
1 MiB/s syncing continuously. After the first sync every sync of the hot log
carries only its appended bytes, and the client matches the server within 5 s
of the writer stopping."""

from __future__ import annotations

import threading
import time
from pathlib import Path

import pytest

from lattice.remote import cache
from tests.test_remote.conftest import bind, tree_hashes
from tests.test_remote.proxies import tcp_proxy
from tests.test_remote.stub_sync_server import running_stub
from tests.torture.envelope import MIB, build_envelope, event_line

pytestmark = [pytest.mark.torture, pytest.mark.timeout(180)]

WRITE_SECONDS = 8.0
APPEND_EVERY = 0.2


def test_continuous_sync_of_a_hot_log_carries_only_appended_bytes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    server_root = tmp_path / "server"
    hot = build_envelope(server_root / ".lattice", tasks=2000, hot_log_bytes=4 * MIB)
    hot_rel = f"events/{hot}.jsonl"
    with running_stub(server_root, slug="demo") as stub:
        port = int(stub.url.rsplit(":", 1)[1])
        with tcp_proxy(port, bytes_per_second=MIB) as proxy:
            client = bind(tmp_path / "client", proxy.url, stub.token, monkeypatch)
            assert cache.catch_up(client, bulk=True).kind == "applied"
            assert tree_hashes(client / ".lattice") == tree_hashes(stub.board)
            first_sync_arrivals = len(stub.arrivals)

            appended = {"bytes": 0}
            lock = threading.Lock()
            stop = threading.Event()

            def writer() -> None:
                n = 0
                while not stop.is_set():
                    line = event_line(hot, 10_000 + n, pad=150)
                    stub.commit(append={hot_rel: line})
                    with lock:
                        appended["bytes"] += len(line)
                    n += 1
                    time.sleep(APPEND_EVERY)

            bodies: list[dict] = []
            stub.fault.mutate_sync = bodies.append
            thread = threading.Thread(target=writer, daemon=True)
            thread.start()
            per_sync: list[
                tuple[int, int]
            ] = []  # (bytes appended since last sync, bytes received)
            started = time.monotonic()
            while time.monotonic() - started < WRITE_SECONDS:
                with lock:
                    appended["bytes"], since_last = 0, appended["bytes"]
                proxy.reset_counts()
                outcome = cache.catch_up(client)
                assert outcome.kind in ("applied", "unchanged"), outcome
                per_sync.append((since_last, proxy.downstream_bytes))
            stop.set()
            thread.join(5)
            stopped = time.monotonic()
            while tree_hashes(client / ".lattice") != tree_hashes(stub.board):
                assert time.monotonic() - stopped < 5.0, (
                    "no match within 5 s of the writer stopping"
                )
                cache.catch_up(client)
            converged = time.monotonic() - stopped

    hot_entries = [body["files"][hot_rel] for body in bodies if hot_rel in body["files"]]
    assert len(hot_entries) >= 10
    assert all("append_from" in entry for entry in hot_entries)  # never the whole 4 MiB file
    later = stub.arrivals[first_sync_arrivals:]
    assert not [a for a in later if a[0] == "files" and a[1]["path"] == hot_rel]
    # Appends arrive at about 1 KiB/s while the hot log is 4 MiB: a sync that
    # re-sent the log, or anything beyond its new bytes, would dwarf this bound.
    assert all(received < 64 * 1024 for _appended, received in per_sync), per_sync
    assert sum(appended for appended, _ in per_sync) > 0
    print(f"syncs={len(per_sync)} converged_in={converged:.2f}s")
