"""AC-23 (H-10b row): a server restored from a backup no longer holds the
history a client saw. The client's next sync sends its line hash, gets a
reset, and ends byte-identical with the server."""

from __future__ import annotations

from pathlib import Path

import pytest

from lattice.remote import cache
from tests.test_remote.conftest import assert_mirror, create_task
from tests.test_remote.stub_sync_server import StubServer


def test_history_mismatch_after_a_restore_resets(
    tmp_path: Path, client_root: Path, stub: StubServer, capsys: pytest.CaptureFixture[str]
) -> None:
    create_task(stub, "before the backup")
    backup = stub.snapshot(tmp_path / "backup")  # clean shutdown, mtimes preserved
    create_task(stub, "lost in the restore")
    create_task(stub, "also lost")
    cache.catch_up(client_root)
    seen_head, seen_hash = stub.head, stub.head_hash()

    stub.restore(backup)  # same root, same epoch, a shorter history
    for n in range(3):
        create_task(stub, f"after the restore {n}")
    assert stub.head > seen_head  # written past the client's seq
    captured: list[dict] = []
    stub.fault.mutate_sync = captured.append

    outcome = cache.catch_up(client_root)

    query = stub.arrivals[-1][1]
    assert (query["since"], query["epoch"], query["hash"]) == (
        str(seen_head),
        stub.epoch,
        seen_hash,
    )
    assert captured[-1]["reset"] is True
    assert outcome.kind == "applied" and outcome.head_seq == stub.head
    assert_mirror(client_root, stub)
    # The files the restore lost were the server's, not local edits, but a
    # reset never discards a file silently: they are kept aside and reported.
    assert "moved to" in capsys.readouterr().err


def test_a_matching_history_is_a_plain_delta(client_root: Path, stub: StubServer) -> None:
    create_task(stub)
    cache.catch_up(client_root)
    create_task(stub, "next")
    captured: list[dict] = []
    stub.fault.mutate_sync = captured.append
    assert cache.catch_up(client_root).kind == "applied"
    assert captured[-1]["reset"] is False
    assert_mirror(client_root, stub)
