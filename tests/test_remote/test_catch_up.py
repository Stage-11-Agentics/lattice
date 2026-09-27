"""catch_up: first sync, deltas, appends, relocation, outcomes.

Correct answers come from the real server (``server``); forced answers from
the stub (``stub``)."""

from __future__ import annotations

import json
import os
import stat
import threading
from pathlib import Path

import pytest

import lattice
from lattice.core.errors import OpError
from lattice.remote import cache
from lattice.storage.ownership import board_state
from lattice.server.testing import BoardServer
from tests.test_remote.conftest import assert_mirror, create_task, record_requests
from tests.test_remote.stub_sync_server import StubServer


def _state(client: Path) -> dict:
    return json.loads((client / ".lattice" / "cache" / "state.json").read_text())


def test_first_sync_mirrors_the_board(client: Path, server: BoardServer) -> None:
    create_task(server)
    outcome = cache.catch_up(client)
    assert outcome.kind == "applied"
    head = server.sync()
    assert outcome.head_seq == head["head_seq"] == 1
    assert outcome.synced_at
    assert_mirror(client, server)
    state = _state(client)
    assert state["remote"] == "team" and state["project"] == server.slug
    assert state["epoch"] == head["epoch"] and state["head_hash"] == head["head_hash"]
    assert state["server_version"] == lattice.__version__
    assert state["fingerprint"] == cache.fingerprint(client / ".lattice")
    assert not (client / ".lattice" / "cache" / "applying").exists()
    assert board_state(client / ".lattice") == "cache"
    for name in cache.RUNTIME_DIRS:
        assert (client / ".lattice" / name).is_dir()
    for name in cache.STANDARD_DIRS:
        assert (client / ".lattice" / name).is_dir()


def test_nothing_new_is_unchanged(
    client: Path, server: BoardServer, monkeypatch: pytest.MonkeyPatch
) -> None:
    create_task(server)
    cache.catch_up(client)
    first = _state(client)
    calls = record_requests(monkeypatch)
    outcome = cache.catch_up(client)
    assert outcome.kind == "unchanged"
    assert outcome.head_seq == first["head_seq"]
    assert [path for path, _ in calls] == [
        f"/v1/projects/{server.slug}/sync?since={first['head_seq']}&epoch={first['epoch']}"
        f"&hash={first['head_hash']}"
    ]


def test_delta_with_an_append_sends_only_new_bytes(
    client: Path, server: BoardServer, monkeypatch: pytest.MonkeyPatch
) -> None:
    task = create_task(server)
    cache.catch_up(client)
    log = f"events/{task}.jsonl"
    before = (client / ".lattice" / log).stat().st_size
    server.op("task.comment", {"task": task, "text": "hello there"})
    calls = record_requests(monkeypatch)
    assert cache.catch_up(client).kind == "applied"
    entry = calls[0][1].data()["files"][log]
    assert entry["append_from"] == before  # only the new bytes travelled
    assert_mirror(client, server)


def test_archive_relocation_and_unarchive(client: Path, server: BoardServer) -> None:
    task = create_task(server)
    cache.catch_up(client)
    server.op("task.archive", {"task": task})
    cache.catch_up(client)
    assert_mirror(client, server)
    assert not (client / ".lattice" / "tasks" / f"{task}.json").exists()
    assert (client / ".lattice" / "archive" / "tasks" / f"{task}.json").exists()
    server.op("task.unarchive", {"task": task})
    cache.catch_up(client)
    assert_mirror(client, server)


@pytest.mark.parametrize("umask", [0o022, 0o077, 0o000])
def test_modes_are_explicit_whatever_the_umask(
    client: Path, server: BoardServer, umask: int
) -> None:
    task = create_task(server)
    old = os.umask(umask)
    try:
        cache.catch_up(client)
        server.op("task.comment", {"task": task, "text": "x"})
        server.op("resource.create", {"name": "r1"})  # a new nested directory
        cache.catch_up(client)
    finally:
        os.umask(old)
    lattice = client / ".lattice"

    def mode(p: Path) -> int:
        return stat.S_IMODE(os.lstat(p).st_mode)

    files, dirs = cache._walk_synced(lattice)
    assert files and dirs
    assert {mode(lattice / rel) for rel, _ in files} == {0o400}
    assert {mode(lattice / rel) for rel in dirs} == {0o500}
    for name in (".", "cache", *cache.RUNTIME_DIRS):
        assert mode(lattice / name) == 0o700, name


def test_unreachable_leaves_the_cache_alone(
    client: Path, server: BoardServer, monkeypatch
) -> None:
    cache.catch_up(client)
    before = cache.fingerprint(client / ".lattice")
    monkeypatch.setenv("LATTICE_REMOTE_TEAM_URL", "http://127.0.0.1:9")
    outcome = cache.catch_up(client)
    assert outcome.kind == "unreachable"
    assert outcome.head_seq == _state(client)["head_seq"]
    assert "cannot reach team" in outcome.detail
    assert cache.fingerprint(client / ".lattice") == before


@pytest.mark.parametrize("code,kind", [("BOARD_BUSY", "busy"), ("RATE_LIMITED", "busy")])
def test_busy_answers(client_root: Path, stub: StubServer, code: str, kind: str) -> None:
    body = json.dumps({"ok": False, "error": {"code": code, "message": "later"}}).encode()
    stub.fault.raw = (
        503 if code == "BOARD_BUSY" else 429,
        {"Content-Type": "application/json", "Lattice-Protocol": "1"},
        body,
    )
    assert cache.catch_up(client_root).kind == kind


def test_a_server_5xx_envelope_is_unreachable(client_root: Path, stub: StubServer) -> None:
    body = json.dumps({"ok": False, "error": {"code": "BOARD_UNAVAILABLE", "message": "x"}})
    stub.fault.raw = (
        503,
        {"Content-Type": "application/json", "Lattice-Protocol": "1"},
        body.encode(),
    )
    assert cache.catch_up(client_root).kind == "unreachable"


def test_a_client_error_raises(client: Path, server: BoardServer, monkeypatch) -> None:
    monkeypatch.setenv("LATTICE_REMOTE_TEAM_TOKEN", "wrong")
    with pytest.raises(OpError) as err:
        cache.catch_up(client)
    assert err.value.code == "UNAUTHENTICATED"


def test_the_probe_returns_busy_while_another_sync_holds_the_lock(
    client: Path, server: BoardServer, monkeypatch
) -> None:
    cache.catch_up(client)
    monkeypatch.setattr(cache, "PROBE_SECONDS", 0.3)
    fd = cache._lock(client / ".lattice" / "locks" / "cache_sync.lock", True, None)
    try:
        outcome = cache.catch_up(client)
    finally:
        os.close(fd)
    assert outcome.kind == "busy"


def test_bulk_waits_for_the_lock(client: Path, server: BoardServer) -> None:
    cache.catch_up(client)
    create_task(server)
    fd = cache._lock(client / ".lattice" / "locks" / "cache_sync.lock", True, None)
    result: list = []
    thread = threading.Thread(target=lambda: result.append(cache.catch_up(client, bulk=True)))
    thread.start()
    thread.join(0.3)
    assert thread.is_alive()
    os.close(fd)
    thread.join(5)
    assert result[0].kind == "applied"


def test_not_hosted(tmp_path: Path) -> None:
    with pytest.raises(OpError) as err:
        cache.catch_up(tmp_path)
    assert err.value.code == "NOT_HOSTED"


def test_a_local_board_beside_a_binding_is_never_synced_over(
    client: Path, server: BoardServer
) -> None:
    from lattice.storage.board_init import create_board

    create_board(client, project_code="LOC")
    with pytest.raises(OpError) as err:
        cache.catch_up(client)
    assert err.value.code == "BINDING_CONFLICT"
    assert not (client / ".lattice" / "cache").exists()


def test_remote_not_configured(client: Path, server: BoardServer, monkeypatch) -> None:
    monkeypatch.delenv("LATTICE_REMOTE_TEAM_URL")
    with pytest.raises(OpError) as err:
        cache.catch_up(client)
    assert err.value.code == "REMOTE_NOT_CONFIGURED"
    assert "lattice remote add team" in err.value.message
    assert not (client / ".lattice").exists()


@pytest.mark.parametrize(
    "status,code", [(500, "INTEGRITY_ERROR"), (500, "SOMETHING_NEW"), (503, "STORAGE_LOW")]
)
def test_a_hard_server_error_raises_whatever_its_status(
    client_root: Path, stub: StubServer, status: int, code: str
) -> None:
    body = json.dumps({"ok": False, "error": {"code": code, "message": "broken"}}).encode()
    stub.fault.raw = (status, {"Content-Type": "application/json", "Lattice-Protocol": "1"}, body)
    with pytest.raises(OpError) as err:
        cache.catch_up(client_root)
    assert err.value.code == code
