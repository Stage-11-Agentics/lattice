"""AC-23 (H-10b row): a server restored from a backup no longer holds the
history a client saw. The client's next sync sends its line hash, gets a
reset, and ends byte-identical with the server. A stream resume with a stale
line hash gets ``reset`` too.

The restore runs twice: on the real server (a backup copied at a clean
shutdown and restored under the same root), and on the stub, whose
restore manipulation isolates the client's side."""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

from lattice.remote import cache
from lattice.server import admin, tokens
from lattice.server.testing import BoardServer, ServerHandle, make_root, running_server
from tests.test_remote.conftest import assert_mirror, bind, create_task, record_requests
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


def test_a_matching_history_is_a_plain_delta(
    client: Path, server: BoardServer, monkeypatch: pytest.MonkeyPatch
) -> None:
    create_task(server)
    cache.catch_up(client)
    create_task(server, "next")
    calls = record_requests(monkeypatch)
    assert cache.catch_up(client).kind == "applied"
    assert calls[0][1].data()["reset"] is False
    assert_mirror(client, server)


# ---------------------------------------------------------------------------
# The real server: a backup restored under the same root, and a stale stream
# ---------------------------------------------------------------------------


def test_a_restored_backup_on_the_real_server_resets_the_client(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """AC-23: a backup taken at a clean shutdown with mtimes preserved, restored
    under the same root; the server writes past the client's seq; the client's
    next sync sends its line hash, gets ``reset``, and ends byte-identical."""
    root = make_root(tmp_path / "base")
    admin.create_project(root, "demo", code="DEM")
    minted = tokens.create_token(root, user="human:alice", machine="t", projects=["demo"])

    def board(handle: ServerHandle) -> BoardServer:
        return BoardServer(handle, "demo", minted["token"], minted["record"]["id"], "human:alice")

    project = root / "projects" / "demo"
    backup = tmp_path / "backup"
    with running_server(root) as handle:
        create_task(board(handle), "before the backup")
    shutil.copytree(project, backup, copy_function=shutil.copy2)  # clean shutdown, mtimes kept

    client = bind(tmp_path / "client", "http://unset", minted["token"], monkeypatch)
    with running_server(root) as handle:
        live = board(handle)
        monkeypatch.setenv("LATTICE_REMOTE_TEAM_URL", live.url)
        create_task(live, "lost in the restore")
        create_task(live, "also lost")
        cache.catch_up(client)
        seen = json.loads((client / ".lattice" / "cache" / "state.json").read_text())

    shutil.rmtree(project)
    shutil.copytree(backup, project, copy_function=shutil.copy2)
    with running_server(root) as handle:
        live = board(handle)
        monkeypatch.setenv("LATTICE_REMOTE_TEAM_URL", live.url)
        for n in range(3):
            create_task(live, f"after the restore {n}")
        head = live.sync()
        assert head["epoch"] == seen["epoch"]  # no rotation: the history check must catch it
        assert head["head_seq"] > seen["head_seq"]  # written past the client's seq
        calls = record_requests(monkeypatch)
        outcome = cache.catch_up(client)
        path, response = calls[0]
        assert f"since={seen['head_seq']}" in path and f"hash={seen['head_hash']}" in path
        assert response.data()["reset"] is True
        assert outcome.kind == "applied" and outcome.head_seq == head["head_seq"]
        assert_mirror(client, live)
    assert "moved to" in capsys.readouterr().err  # the lost files are kept aside


def test_a_stream_resume_with_a_stale_line_hash_gets_reset(
    client: Path, server: BoardServer
) -> None:
    create_task(server)
    cache.catch_up(client)
    state = json.loads((client / ".lattice" / "cache" / "state.json").read_text())
    stale = f"{state['epoch']}:{state['head_seq']}:{'0' * 32}"
    with server.stream(last_event_id=stale) as stream:
        events = [stream.next(timeout=5) for _ in range(2)]
    assert "reset" in [event.event for event in events if event is not None]
