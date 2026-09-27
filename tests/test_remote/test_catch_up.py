"""catch_up against the stub: first sync, deltas, appends, relocation, outcomes."""

from __future__ import annotations

import json
import os
import stat
import threading
from pathlib import Path

import pytest

from lattice.core.errors import OpError
from lattice.remote import cache
from lattice.storage.ownership import board_state
from tests.test_remote.conftest import assert_mirror, create_task
from tests.test_remote.stub_sync_server import StubServer


def _state(client: Path) -> dict:
    return json.loads((client / ".lattice" / "cache" / "state.json").read_text())


def test_first_sync_mirrors_the_board(client_root: Path, stub: StubServer) -> None:
    create_task(stub)
    outcome = cache.catch_up(client_root)
    assert outcome.kind == "applied"
    assert outcome.head_seq == stub.head == 1
    assert outcome.synced_at
    assert_mirror(client_root, stub)
    state = _state(client_root)
    assert state["remote"] == "team" and state["project"] == "demo"
    assert state["epoch"] == stub.epoch and state["head_hash"] == stub.head_hash()
    assert state["server_version"] == "2.0.0.dev0+stub"
    assert state["fingerprint"] == cache.fingerprint(client_root / ".lattice")
    assert not (client_root / ".lattice" / "cache" / "applying").exists()
    assert board_state(client_root / ".lattice") == "cache"
    for name in cache.RUNTIME_DIRS:
        assert (client_root / ".lattice" / name).is_dir()
    for name in cache.STANDARD_DIRS:
        assert (client_root / ".lattice" / name).is_dir()


def test_nothing_new_is_unchanged(client_root: Path, stub: StubServer) -> None:
    cache.catch_up(client_root)
    first = _state(client_root)
    outcome = cache.catch_up(client_root)
    assert outcome.kind == "unchanged"
    assert outcome.head_seq == first["head_seq"]
    assert stub.arrivals[-1][0] == "sync"
    assert stub.arrivals[-1][1]["since"] == str(first["head_seq"])


def test_delta_with_an_append_sends_only_new_bytes(client_root: Path, stub: StubServer) -> None:
    task = create_task(stub)
    cache.catch_up(client_root)
    captured: list[dict] = []
    stub.fault.mutate_sync = captured.append
    stub.op("task.comment", {"task": task, "text": "hello there"})
    assert cache.catch_up(client_root).kind == "applied"
    log = f"events/{task}.jsonl"
    entry = captured[-1]["files"][log]
    assert entry["append_from"] > 0
    assert_mirror(client_root, stub)


def test_archive_relocation_and_unarchive(client_root: Path, stub: StubServer) -> None:
    task = create_task(stub)
    cache.catch_up(client_root)
    stub.op("task.archive", {"task": task})
    cache.catch_up(client_root)
    assert_mirror(client_root, stub)
    assert not (client_root / ".lattice" / "tasks" / f"{task}.json").exists()
    assert (client_root / ".lattice" / "archive" / "tasks" / f"{task}.json").exists()
    stub.op("task.unarchive", {"task": task})
    cache.catch_up(client_root)
    assert_mirror(client_root, stub)


@pytest.mark.parametrize("umask", [0o022, 0o077, 0o000])
def test_modes_are_explicit_whatever_the_umask(
    client_root: Path, stub: StubServer, umask: int
) -> None:
    task = create_task(stub)
    old = os.umask(umask)
    try:
        cache.catch_up(client_root)
        stub.op("task.comment", {"task": task, "text": "x"})
        stub.commit(write={"resources/r1/meta.json": b"{}"})
        cache.catch_up(client_root)
    finally:
        os.umask(old)
    lattice = client_root / ".lattice"

    def mode(p: Path) -> int:
        return stat.S_IMODE(os.lstat(p).st_mode)

    files, dirs = cache._walk_synced(lattice)
    assert files and dirs
    assert {mode(lattice / rel) for rel, _ in files} == {0o400}
    assert {mode(lattice / rel) for rel in dirs} == {0o500}
    for name in (".", "cache", *cache.RUNTIME_DIRS):
        assert mode(lattice / name) == 0o700, name


def test_unreachable_leaves_the_cache_alone(
    client_root: Path, stub: StubServer, monkeypatch
) -> None:
    cache.catch_up(client_root)
    before = cache.fingerprint(client_root / ".lattice")
    monkeypatch.setenv("LATTICE_REMOTE_TEAM_URL", "http://127.0.0.1:9")
    outcome = cache.catch_up(client_root)
    assert outcome.kind == "unreachable"
    assert outcome.head_seq == 1 - 1 or outcome.head_seq == _state(client_root)["head_seq"]
    assert "cannot reach team" in outcome.detail
    assert cache.fingerprint(client_root / ".lattice") == before


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


def test_a_client_error_raises(client_root: Path, stub: StubServer, monkeypatch) -> None:
    monkeypatch.setenv("LATTICE_REMOTE_TEAM_TOKEN", "wrong")
    with pytest.raises(OpError) as err:
        cache.catch_up(client_root)
    assert err.value.code == "UNAUTHENTICATED"


def test_the_probe_returns_busy_while_another_sync_holds_the_lock(
    client_root: Path, stub: StubServer, monkeypatch
) -> None:
    cache.catch_up(client_root)
    monkeypatch.setattr(cache, "PROBE_SECONDS", 0.3)
    fd = cache._lock(client_root / ".lattice" / "locks" / "cache_sync.lock", True, None)
    try:
        outcome = cache.catch_up(client_root)
    finally:
        os.close(fd)
    assert outcome.kind == "busy"


def test_bulk_waits_for_the_lock(client_root: Path, stub: StubServer) -> None:
    cache.catch_up(client_root)
    create_task(stub)
    fd = cache._lock(client_root / ".lattice" / "locks" / "cache_sync.lock", True, None)
    result: list = []
    thread = threading.Thread(target=lambda: result.append(cache.catch_up(client_root, bulk=True)))
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
    client_root: Path, stub: StubServer
) -> None:
    from lattice.storage.board_init import create_board

    create_board(client_root, project_code="LOC")
    with pytest.raises(OpError) as err:
        cache.catch_up(client_root)
    assert err.value.code == "BINDING_CONFLICT"
    assert not (client_root / ".lattice" / "cache").exists()


def test_remote_not_configured(client_root: Path, stub: StubServer, monkeypatch) -> None:
    monkeypatch.delenv("LATTICE_REMOTE_TEAM_URL")
    with pytest.raises(OpError) as err:
        cache.catch_up(client_root)
    assert err.value.code == "REMOTE_NOT_CONFIGURED"
    assert "lattice remote add team" in err.value.message
    assert not (client_root / ".lattice").exists()
