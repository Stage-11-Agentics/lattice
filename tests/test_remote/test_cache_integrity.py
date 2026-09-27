"""AC-47 (H-10b rows): after a sync every board file matches the server byte for
byte; a local edit is moved aside and reported, never silently discarded; the
cache refuses edits; bad deltas are rejected whole; interrupted applies are
detected and repaired; ``cache clear`` never deletes a local board.

Correct answers come from the real server (``server``); malformed or forced
answers from the stub (``stub``)."""

from __future__ import annotations

import json
import os
import signal
import stat
import subprocess
import sys
from pathlib import Path
from unittest.mock import patch

import pytest
from click.testing import CliRunner

from lattice.cli.main import cli
from lattice.core.errors import OpError
from lattice.remote import cache
from lattice.server.testing import BoardServer, ServerHandle
from tests.test_remote.conftest import (
    assert_mirror,
    bind,
    create_task,
    new_project,
    tree_hashes,
)
from tests.test_remote.stub_sync_server import StubServer

RESCUE_LINE = "locally edited board file(s) moved to"


def _lattice(client: Path) -> Path:
    return client / ".lattice"


def _rescued(client: Path) -> dict[str, bytes]:
    base = _lattice(client) / "cache" / "rescued"
    found: dict[str, bytes] = {}
    if base.is_dir():
        for path in sorted(base.rglob("*")):
            if path.is_file() and not path.name.startswith(".rescue-"):
                found[path.relative_to(base).as_posix()] = path.read_bytes()
    return found


def _edit(path: Path, data: bytes) -> None:
    """A deliberate local edit: open the modes first, as a determined user would."""
    os.chmod(path.parent, 0o700)
    os.chmod(path, 0o600)
    path.write_bytes(data)
    os.chmod(path, 0o400)
    os.chmod(path.parent, 0o500)


def _synced(client: Path, server: BoardServer | StubServer) -> str:
    task = create_task(server)
    assert cache.catch_up(client).kind == "applied"
    return task


# ---------------------------------------------------------------------------
# Tamper detection and rescue
# ---------------------------------------------------------------------------


def test_a_local_edit_is_rescued_and_reset(
    client: Path, server: BoardServer, capsys: pytest.CaptureFixture[str]
) -> None:
    task = _synced(client, server)
    target = _lattice(client) / "tasks" / f"{task}.json"
    original = target.read_bytes()
    _edit(target, original + b" ")
    assert cache.catch_up(client).kind == "applied"
    err = capsys.readouterr().err
    assert err.count(RESCUE_LINE) == 1
    assert "1 locally edited board file(s)" in err
    assert "lattice plan write <task> --file <path>" in err
    assert target.read_bytes() == original
    rescued = _rescued(client)
    assert list(rescued.values()) == [original + b" "]
    assert next(iter(rescued)).endswith(f"tasks/{task}.json")
    assert_mirror(client, server)


def test_an_orchestration_file_is_synced_protected_and_rescued(
    client: Path, server: BoardServer, capsys: pytest.CaptureFixture[str]
) -> None:
    server.op("board.file_write", {"path": "orchestration/run-state.md", "file": "# run\n"})
    cache.catch_up(client)
    target = _lattice(client) / "orchestration" / "run-state.md"
    assert stat.S_IMODE(target.stat().st_mode) == 0o400
    assert stat.S_IMODE(target.parent.stat().st_mode) == 0o500
    with pytest.raises(PermissionError):
        target.write_text("edited")
    _edit(target, b"# edited locally\n")
    assert cache.catch_up(client).kind == "applied"
    assert RESCUE_LINE in capsys.readouterr().err
    assert target.read_bytes() == b"# run\n"
    assert list(_rescued(client).values()) == [b"# edited locally\n"]


def test_the_cache_refuses_edits(client: Path, server: BoardServer) -> None:
    task = _synced(client, server)
    tasks = _lattice(client) / "tasks"
    target = tasks / f"{task}.json"
    with pytest.raises(PermissionError):
        target.write_text("direct write")
    with pytest.raises(PermissionError):
        (tasks / "tmp.save").write_text("rename-based save")
    with pytest.raises(PermissionError):
        (tasks / "new.json").write_text("new file")
    assert cache.catch_up(client).kind == "unchanged"


def test_a_rename_save_of_config_is_detected_and_rescued(
    client: Path, server: BoardServer, capsys: pytest.CaptureFixture[str]
) -> None:
    _synced(client, server)
    config = _lattice(client) / "config.json"
    original = config.read_bytes()
    tmp = config.with_name("config.json.swp")
    tmp.write_bytes(b'{"edited": true}\n')
    os.replace(tmp, config)  # the directory is writable: an editor's save succeeds
    assert cache.catch_up(client).kind == "applied"
    assert RESCUE_LINE in capsys.readouterr().err
    assert config.read_bytes() == original
    assert list(_rescued(client).values()) == [b'{"edited": true}\n']
    assert_mirror(client, server)


def test_a_file_the_reset_lacks_is_rescued(client: Path, server: BoardServer) -> None:
    _synced(client, server)
    notes = _lattice(client) / "notes"
    os.chmod(notes, 0o700)
    (notes / "mine.md").write_text("my notes")
    os.chmod(notes, 0o500)
    cache.catch_up(client)
    assert not (notes / "mine.md").exists()
    assert list(_rescued(client).values()) == [b"my notes"]


def test_no_rescue_when_nothing_was_edited(
    client: Path, server: BoardServer, capsys: pytest.CaptureFixture[str]
) -> None:
    _synced(client, server)
    create_task(server, "second")
    assert cache.catch_up(client).kind == "applied"
    assert RESCUE_LINE not in capsys.readouterr().err
    assert _rescued(client) == {}


# ---------------------------------------------------------------------------
# Deltas rejected whole
# ---------------------------------------------------------------------------


def _inject(stub: StubServer, rel: str, **spec: object) -> None:
    def mutate(body: dict) -> None:
        body["files"][rel] = {"sha256": "0" * 64, "size": 1, "content_b64": "eA==", **spec}

    stub.fault.mutate_sync = mutate


@pytest.mark.parametrize(
    "rel",
    [
        "../escape.json",
        "tasks/../../escape.json",
        "/etc/lattice-escape",
        "locks/evil.lock",
        "review_state/x.json",
        "cache/state.json",
        "hosted/journal.jsonl",
        "runner.log",
        "reviews/x.md",
    ],
)
def test_unsafe_paths_reject_the_whole_delta(
    client_root: Path, stub: StubServer, rel: str
) -> None:
    _synced(client_root, stub)
    before = tree_hashes(_lattice(client_root))
    state_before = (_lattice(client_root) / "cache" / "state.json").read_bytes()
    create_task(stub, "not applied")
    _inject(stub, rel)
    with pytest.raises(OpError) as err:
        cache.catch_up(client_root)
    assert err.value.code == "INTEGRITY_ERROR"
    assert err.value.details["reason"] == "UNSAFE_PATH"
    assert tree_hashes(_lattice(client_root)) == before
    assert (_lattice(client_root) / "cache" / "state.json").read_bytes() == state_before
    assert not (client_root / "escape.json").exists()


def test_an_unsafe_removed_path_rejects_the_delta(client_root: Path, stub: StubServer) -> None:
    _synced(client_root, stub)
    create_task(stub, "x")
    stub.fault.mutate_sync = lambda body: body["removed"].append("../../victim")
    with pytest.raises(OpError) as err:
        cache.catch_up(client_root)
    assert err.value.details["reason"] == "UNSAFE_PATH"


@pytest.mark.parametrize(
    "href",
    ["http://evil.example/v1/x", "//evil.example/x", "https://127.0.0.1:1/v1/x", "v1/x"],
)
def test_a_cross_origin_href_is_rejected_and_never_fetched(
    client_root: Path, stub: StubServer, href: str
) -> None:
    _synced(client_root, stub)
    before = tree_hashes(_lattice(client_root))
    create_task(stub, "x")
    stub.fault.mutate_sync = lambda body: body["files"].update(
        {"tasks/other.json": {"sha256": "0" * 64, "size": 3, "href": href}}
    )
    requests = len(stub.requests)
    with pytest.raises(OpError) as err:
        cache.catch_up(client_root)
    assert err.value.details["reason"] == "CROSS_ORIGIN_HREF"
    assert len(stub.requests) == requests + 1  # the sync itself, nothing more
    assert tree_hashes(_lattice(client_root)) == before


def test_a_persistent_hash_mismatch_fails_after_bounded_resyncs(
    client_root: Path, stub: StubServer
) -> None:
    _synced(client_root, stub)
    before = tree_hashes(_lattice(client_root))
    task = create_task(stub, "x")

    def corrupt(body: dict) -> None:
        body["files"][f"tasks/{task}.json"]["sha256"] = "f" * 64

    stub.fault.mutate_sync = corrupt
    syncs = sum(1 for kind, _ in stub.arrivals if kind == "sync")
    with pytest.raises(OpError) as err:
        cache.catch_up(client_root)
    assert err.value.details["reason"] == "HASH_MISMATCH"
    assert sum(1 for kind, _ in stub.arrivals if kind == "sync") - syncs == cache.MAX_CYCLES
    assert tree_hashes(_lattice(client_root)) == before


def test_a_one_off_hash_mismatch_resyncs(client_root: Path, stub: StubServer) -> None:
    _synced(client_root, stub)
    task = create_task(stub, "x")
    shots = {"left": 1}

    def corrupt_once(body: dict) -> None:
        if shots["left"]:
            shots["left"] -= 1
            body["files"][f"tasks/{task}.json"]["sha256"] = "f" * 64

    stub.fault.mutate_sync = corrupt_once
    assert cache.catch_up(client_root).kind == "applied"
    assert_mirror(client_root, stub)


def test_a_fetched_file_is_verified(client_root: Path, stub: StubServer) -> None:
    stub.inline_file_bytes = 10  # every file travels by href
    _synced(client_root, stub)
    create_task(stub, "x")
    stub.fault.mutate_file = lambda rel, data: data + b"tampered"
    with pytest.raises(OpError) as err:
        cache.catch_up(client_root)
    assert err.value.details["reason"] == "HASH_MISMATCH"
    stub.fault.mutate_file = None
    assert cache.catch_up(client_root).kind == "applied"
    assert_mirror(client_root, stub)


def test_stale_version_resyncs(client_root: Path, stub: StubServer) -> None:
    stub.inline_file_bytes = 10  # every file travels by href
    _synced(client_root, stub)
    task = create_task(stub, "x")
    shots = {"left": 1}

    def change_after_assembly(body: dict) -> None:
        if shots["left"]:
            shots["left"] -= 1
            stub.op("task.comment", {"task": task, "text": "raced"})

    stub.fault.mutate_sync = change_after_assembly
    assert cache.catch_up(client_root).kind == "applied"
    assert any(status == 412 for _rel, status in stub.file_statuses)
    assert_mirror(client_root, stub)


def test_an_append_against_a_wrong_length_copy_fetches_the_whole_file(
    client_root: Path, stub: StubServer
) -> None:
    task = _synced(client_root, stub)
    log = f"events/{task}.jsonl"
    stub.op("task.comment", {"task": task, "text": "more"})

    def shift(body: dict) -> None:
        entry = body["files"][log]
        entry["append_from"] -= 1  # the local copy is not append_from bytes long

    stub.fault.mutate_sync = shift
    assert cache.catch_up(client_root).kind == "applied"
    assert ("files", {"path": log, "sha256": body_sha(stub, log)}) in stub.arrivals
    assert_mirror(client_root, stub)


def body_sha(stub: StubServer, rel: str) -> str:
    import hashlib

    return hashlib.sha256((stub.board / rel).read_bytes()).hexdigest()


# ---------------------------------------------------------------------------
# Modes and runtime directories on a fresh cache
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("umask", [0o022, 0o077])
def test_effective_modes(client: Path, server: BoardServer, umask: int) -> None:
    task = create_task(server)
    old = os.umask(umask)
    try:
        cache.catch_up(client)
        server.op("task.comment", {"task": task, "text": "x"})
        server.op("resource.create", {"name": "r1"})  # a new nested directory
        cache.catch_up(client)
    finally:
        os.umask(old)
    lattice = _lattice(client)

    def mode(p: Path) -> int:
        return stat.S_IMODE(os.lstat(p).st_mode)

    files, dirs = cache._walk_synced(lattice)
    assert {mode(lattice / rel) for rel, _ in files} == {0o400}
    assert {mode(lattice / rel) for rel in dirs} == {0o500}
    for name in (".", "cache", *cache.RUNTIME_DIRS):
        assert mode(lattice / name) == 0o700, name


def test_review_state_and_an_auto_review_spawn_work_on_a_fresh_cache(
    client: Path, server: BoardServer
) -> None:
    from lattice.cli import auto_review
    from lattice.core.review import read_review_state, write_review_state

    task = _synced(client, server)
    lattice = _lattice(client)
    write_review_state(lattice, {"task_id": task, "status": "running"})
    assert read_review_state(lattice, task)["status"] == "running"
    other = create_task(server, "second")
    cache.catch_up(client)
    with patch.object(auto_review, "find_lattice_executable", return_value="/usr/bin/true"):
        result = auto_review.auto_fire_review(
            lattice,
            other,
            "planned",
            status_event_id="ev_x",
            config={"plan_review_mode": "single"},
            no_auto_review_flag=False,
        )
    assert result["fired"] is True, result
    assert Path(result["log_path"]).is_file()
    assert read_review_state(lattice, other) is not None


# ---------------------------------------------------------------------------
# lattice cache clear
# ---------------------------------------------------------------------------


def _clear(client: Path, *args: str) -> object:
    return CliRunner().invoke(cli, ["cache", "clear", *args], env={"LATTICE_ROOT": str(client)})


def test_cache_clear_keeps_rescued_and_the_marker(client: Path, server: BoardServer) -> None:
    task = _synced(client, server)
    _edit(_lattice(client) / "tasks" / f"{task}.json", b"edited")
    cache.catch_up(client)
    (client / cache.BINDING_FILE).unlink()  # a branch without the binding
    result = _clear(client)
    assert result.exit_code == 0, result.output
    assert "kept rescued board files" in result.stderr
    lattice = _lattice(client)
    assert sorted(p.name for p in lattice.iterdir()) == ["cache", "locks"]  # runtime locks stay
    assert sorted(p.name for p in (lattice / "cache").iterdir()) == ["rescued", "state.json"]
    assert json.loads((lattice / "cache" / "state.json").read_text()) == {
        "project": server.slug,
        "remote": "team",
    }
    assert list(_rescued(client).values()) == [b"edited"]
    # Routing survives through the marker; the next sync resets from scratch.
    outcome = cache.catch_up(client)
    assert outcome.kind == "applied"
    assert_mirror(client, server)


def test_cache_clear_json(client: Path, server: BoardServer) -> None:
    _synced(client, server)
    result = _clear(client, "--json")
    assert result.exit_code == 0
    data = json.loads(result.stdout)["data"]
    assert data == {
        "forgot": False,
        "kept": [],
        "project": server.slug,
        "remote": "team",
        "root": str(client),
    }


def test_cache_clear_forget_then_another_project(
    client: Path, server: BoardServer, live_server: ServerHandle, monkeypatch: pytest.MonkeyPatch
) -> None:
    _synced(client, server)
    result = _clear(client, "--forget")
    assert result.exit_code == 0, result.output
    assert sorted(p.name for p in _lattice(client).iterdir()) == ["locks"]
    assert cache.cache_identity(client) is not None  # the committed binding alone
    other = new_project(live_server, code="OTH")
    create_task(other, "from the other project")
    bind(client, other.url, other.token, monkeypatch, project=other.slug)
    assert cache.catch_up(client).kind == "applied"
    assert_mirror(client, other)


@pytest.mark.parametrize("args", [(), ("--forget",), ("--json",)])
def test_cache_clear_refuses_a_local_board(tmp_path: Path, args: tuple[str, ...]) -> None:
    from lattice.storage.board_init import create_board

    create_board(tmp_path, project_code="LOC")
    before = sorted(p.relative_to(tmp_path) for p in (tmp_path / ".lattice").rglob("*"))
    result = _clear(tmp_path, *args)
    assert result.exit_code == 1
    output = result.stdout if "--json" in args else result.stderr
    assert "NOT_HOSTED" in output or "not a hosted checkout" in output
    assert sorted(p.relative_to(tmp_path) for p in (tmp_path / ".lattice").rglob("*")) == before


def test_cache_clear_outside_any_board(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    result = CliRunner().invoke(cli, ["cache", "clear", "--json"])
    assert result.exit_code == 1
    assert json.loads(result.stdout)["error"]["code"] == "NOT_HOSTED"


# ---------------------------------------------------------------------------
# Killed syncers (SIGKILL in a subprocess at a named step)
# ---------------------------------------------------------------------------

_KILLER = """
import os, signal, sys
from pathlib import Path
from lattice.remote import cache

root, step, nth = Path(sys.argv[1]), sys.argv[2], int(sys.argv[3])
seen = [0]

def seam(name):
    if name == step:
        seen[0] += 1
        if seen[0] >= nth:
            os.kill(os.getpid(), signal.SIGKILL)

cache._seam = seam
cache.catch_up(root, bulk=True)
print("finished without reaching", step)
"""


def kill_sync_at(root: Path, step: str, nth: int = 1) -> None:
    proc = subprocess.run(
        [sys.executable, "-c", _KILLER, str(root), step, str(nth)],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert proc.returncode == -signal.SIGKILL, proc.stdout + proc.stderr


def _assert_offline_read_fails_then_online_repairs(
    client: Path, server: BoardServer | StubServer, monkeypatch: pytest.MonkeyPatch
) -> None:
    assert (_lattice(client) / "cache" / "applying").exists()
    monkeypatch.setenv("LATTICE_REMOTE_TEAM_URL", "http://127.0.0.1:9")
    outcome = cache.catch_up(client)
    assert outcome.kind == "incomplete"
    with pytest.raises(OpError) as err, cache.read_lock(client):
        pass
    assert err.value.code == "CACHE_INCOMPLETE"
    assert "lattice sync" in err.value.message
    monkeypatch.setenv("LATTICE_REMOTE_TEAM_URL", server.url)
    assert cache.catch_up(client).kind == "applied"
    with cache.read_lock(client):
        pass
    assert_mirror(client, server)


def test_killed_during_the_first_sync(
    client: Path, server: BoardServer, monkeypatch: pytest.MonkeyPatch
) -> None:
    for n in range(3):
        create_task(server, f"task {n}")
    kill_sync_at(client, "file_written", 2)
    applying = json.loads((_lattice(client) / "cache" / "applying").read_text())
    assert (applying["remote"], applying["project"]) == ("team", server.slug)
    assert applying["kind"] == "reset"
    assert not (_lattice(client) / "cache" / "state.json").exists()
    # Routed as hosted by the leftover alone, even without the binding.
    (client / cache.BINDING_FILE).unlink()
    assert cache.cache_identity(client) == ("team", server.slug)
    _assert_offline_read_fails_then_online_repairs(client, server, monkeypatch)


def test_killed_during_an_archive_relocation(
    client: Path, server: BoardServer, monkeypatch: pytest.MonkeyPatch
) -> None:
    task = _synced(client, server)
    server.op("task.archive", {"task": task})
    kill_sync_at(client, "file_written", 1)
    _assert_offline_read_fails_then_online_repairs(client, server, monkeypatch)
    assert (_lattice(client) / "archive" / "tasks" / f"{task}.json").exists()


def test_killed_during_a_reset(
    client: Path, server: BoardServer, monkeypatch: pytest.MonkeyPatch
) -> None:
    _synced(client, server)
    create_task(server, "second")
    server.rotate_epoch()  # the next sync is a reset
    kill_sync_at(client, "file_written", 2)
    _assert_offline_read_fails_then_online_repairs(client, server, monkeypatch)


@pytest.mark.parametrize(
    "step", ["rescue_copied", "rescue_renamed", "rescue_dir_synced", "rescue_unlinked"]
)
def test_a_rescue_survives_a_kill_at_every_step(
    client: Path, server: BoardServer, monkeypatch: pytest.MonkeyPatch, step: str
) -> None:
    task = _synced(client, server)
    target = _lattice(client) / "tasks" / f"{task}.json"
    _edit(target, b"precious local edit")
    kill_sync_at(client, step)
    # Never lost: the edit is at its source, in the rescue directory, or both.
    at_source = target.exists() and target.read_bytes() == b"precious local edit"
    assert at_source or b"precious local edit" in _rescued(client).values()
    assert cache.catch_up(client).kind == "applied"
    assert b"precious local edit" in _rescued(client).values()
    assert_mirror(client, server)


def test_a_planted_symlink_and_fifo_are_removed_through_the_primitive(
    tmp_path: Path, client: Path, server: BoardServer, monkeypatch: pytest.MonkeyPatch
) -> None:
    from lattice.storage import fs

    _synced(client, server)
    lattice = _lattice(client)
    outside = tmp_path / "outside-target.txt"
    outside.write_text("not board data")
    os.chmod(lattice / "tasks", 0o700)
    (lattice / "tasks" / "planted.json").symlink_to(outside)
    os.chmod(lattice / "tasks", 0o500)
    os.chmod(lattice / "notes", 0o700)
    os.mkfifo(lattice / "notes" / "pipe")
    os.chmod(lattice / "notes", 0o500)
    removed: list[str] = []
    real = fs.unlink_entry
    monkeypatch.setattr(cache, "unlink_entry", lambda p: (removed.append(Path(p).name), real(p)))
    assert cache.catch_up(client).kind == "applied"  # the tree changed: a reset
    assert sorted(removed) == ["pipe", "planted.json"]
    assert not os.path.lexists(lattice / "tasks" / "planted.json")
    assert not os.path.lexists(lattice / "notes" / "pipe")
    assert outside.read_text() == "not board data"
    rescued = lattice / "cache" / "rescued"
    links = [p for p in rescued.rglob("planted.json")]
    assert len(links) == 1 and links[0].is_symlink() and os.readlink(links[0]) == str(outside)
    assert_mirror(client, server)


@pytest.mark.parametrize(
    "head_hash",
    [None, "", "ABCDEF0123456789ABCDEF0123456789", "0" * 31, "0" * 33, "g" * 32, 7],
    ids=["missing", "empty", "uppercase", "short", "long", "not-hex", "not-a-string"],
)
def test_a_bad_head_hash_at_a_nonzero_head_is_rejected(
    client_root: Path, stub: StubServer, head_hash: object
) -> None:
    create_task(stub)

    def mutate(body: dict) -> None:
        if head_hash is None:
            body.pop("head_hash", None)
        else:
            body["head_hash"] = head_hash

    stub.fault.mutate_sync = mutate
    with pytest.raises(OpError) as err:
        cache.catch_up(client_root)
    assert err.value.details["reason"] == "MALFORMED_SYNC"
    assert not (_lattice(client_root) / "cache" / "state.json").exists()


def test_a_head_hash_at_head_zero_is_rejected(client_root: Path, stub: StubServer) -> None:
    assert stub.head == 0
    stub.fault.mutate_sync = lambda body: body.update(head_hash="0" * 32)
    with pytest.raises(OpError) as err:
        cache.catch_up(client_root)
    assert err.value.details["reason"] == "MALFORMED_SYNC"
    stub.fault.mutate_sync = None
    assert cache.catch_up(client_root).kind == "applied"  # absent at head 0 is fine


def test_an_explicit_null_head_hash_at_head_zero_is_rejected(
    client_root: Path, stub: StubServer
) -> None:
    assert stub.head == 0
    stub.fault.mutate_sync = lambda body: body.update(head_hash=None)
    with pytest.raises(OpError) as err:
        cache.catch_up(client_root)
    assert err.value.details["reason"] == "MALFORMED_SYNC"
    assert not (_lattice(client_root) / "cache" / "state.json").exists()


def test_a_file_name_containing_two_dots_is_a_legal_board_path(
    client_root: Path, stub: StubServer
) -> None:
    stub.inline_file_bytes = 4  # travel through the files endpoint
    stub.commit(write={"notes/a..b.md": b"two dots, one segment\n"})
    assert cache.catch_up(client_root).kind == "applied"
    assert (_lattice(client_root) / "notes" / "a..b.md").read_bytes() == b"two dots, one segment\n"
    assert_mirror(client_root, stub)
