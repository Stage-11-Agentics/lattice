"""Board ownership (SPEC §6, H-8): path classes, cache and hosted markers checked
in every storage write primitive, board confinement, the owner and syncer
flags, and offline maintenance. AC-3 and G-1 at the primitive level."""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import threading
from dataclasses import dataclass
from pathlib import Path

import pytest

from lattice.core.errors import BoardIsCache, BoardIsHosted, BoardPathError, OpError
from lattice.ops import Caller, OpResult, execute
from lattice.storage.fs import atomic_write, ensure_dir, jsonl_append, recording, unlink_path
from lattice.storage.operations import read_task_authority
from lattice.storage.ownership import (
    PathClass,
    board_scope,
    board_state,
    check_board_writable,
    classify_path,
    offline_maintenance,
    owning_board,
    release_owner_flock,
    syncing_board,
    try_owner_flock,
)

OP_ID = "op_01J9ZABCDEFGHJKMNPQRSTVWXY"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _durable_tree(lattice_dir: Path) -> dict[str, str]:
    """Hash of every durable and workspace file (the bytes a refusal must not change)."""
    tree = {}
    for path in sorted(lattice_dir.rglob("*")):
        rel = path.relative_to(lattice_dir)
        if path.is_file() and classify_path(rel) in (PathClass.DURABLE, PathClass.WORKSPACE):
            tree[rel.as_posix()] = hashlib.sha256(path.read_bytes()).hexdigest()
        elif path.is_dir() and classify_path(rel) is PathClass.DURABLE:
            tree[rel.as_posix() + "/"] = "dir"
    return tree


def _plant(lattice_dir: Path, marker: str) -> None:
    if marker == "state":
        (lattice_dir / "cache").mkdir(exist_ok=True)
        (lattice_dir / "cache" / "state.json").write_text(
            json.dumps({"remote": "studio", "project": "apollo"})
        )
    elif marker == "applying":
        (lattice_dir / "cache").mkdir(exist_ok=True)
        (lattice_dir / "cache" / "applying").write_text(
            json.dumps({"remote": "studio", "project": "apollo", "kind": "reset"})
        )
    else:
        (lattice_dir / "hosted").mkdir(exist_ok=True)
        (lattice_dir / "hosted" / "owner.json").write_text(
            json.dumps({"server_id": "srv_1", "host": "atlas", "pid": 4242, "started_at": "x"})
        )


MARKERS = {
    "state": (BoardIsCache, "BOARD_IS_CACHE"),
    "applying": (BoardIsCache, "BOARD_IS_CACHE"),
    "hosted": (BoardIsHosted, "BOARD_IS_HOSTED"),
}


@pytest.fixture()
def board(initialized_root: Path, invoke) -> Path:  # noqa: ANN001
    """An initialized board's ``.lattice/`` with one task (plan, events, snapshot, ids)."""
    result = invoke("create", "Seed", "--actor", "human:t", "--json")
    assert result.exit_code == 0, result.output
    return initialized_root / ".lattice"


def _task_id(lattice_dir: Path) -> str:
    return next((lattice_dir / "tasks").glob("task_*.json")).stem


# ---------------------------------------------------------------------------
# Path classes (§6.1)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("rel", "expected"),
    [
        (".", PathClass.DURABLE),
        ("tasks/task_1.json", PathClass.DURABLE),
        ("events/_lifecycle.jsonl", PathClass.DURABLE),
        ("archive/events/task_1.jsonl", PathClass.DURABLE),
        ("plans/task_1.md", PathClass.DURABLE),
        ("plans/review-pack.md", PathClass.DURABLE),
        ("notes/x/y.md", PathClass.DURABLE),
        ("artifacts/payload/art_1.txt", PathClass.DURABLE),
        ("resources/db/resource.json", PathClass.DURABLE),
        ("sessions/archive/a.json", PathClass.DURABLE),
        ("templates/code_review.md", PathClass.DURABLE),
        ("config.json", PathClass.DURABLE),
        ("ids.json", PathClass.DURABLE),
        ("context.md", PathClass.DURABLE),
        (".gitignore", PathClass.DURABLE),
        ("orchestration/run-state.md", PathClass.WORKSPACE),
        ("orchestration/a/b/c.md", PathClass.WORKSPACE),
        ("locks/events_x.lock", PathClass.RUNTIME),
        ("review_state/task_1.json", PathClass.RUNTIME),
        ("tmp-prompts/p.md", PathClass.RUNTIME),
        (".daemon/auto-code-review-x.log", PathClass.RUNTIME),
        ("tasks/.tmp.abc123", PathClass.TEMPORARY),
        (".tmp.xyz", PathClass.TEMPORARY),
        ("hosted/owner.json", PathClass.SERVER_CONTROL),
        ("hosted/undo/t--op.jsonl", PathClass.SERVER_CONTROL),
        ("cache/state.json", PathClass.CACHE_CONTROL),
        ("cache/rescued/20260926/plans/x.md", PathClass.CACHE_CONTROL),
        ("reviews/r.md", PathClass.UNMANAGED),
        ("exports/e.json", PathClass.UNMANAGED),
        ("runner.log", PathClass.UNMANAGED),
        ("config.json.bak", PathClass.UNMANAGED),
        ("tasks.json", PathClass.UNMANAGED),
    ],
)
def test_classify_path(rel: str, expected: PathClass) -> None:
    assert classify_path(rel) is expected


# ---------------------------------------------------------------------------
# Markers (§6.2): every primitive refuses, and nothing durable changes
# ---------------------------------------------------------------------------


def _primitive_writes(lattice_dir: Path, task_id: str) -> dict:
    return {
        "atomic_write create": lambda: atomic_write(lattice_dir / "plans" / "new.md", "x"),
        "atomic_write replace": lambda: atomic_write(lattice_dir / "config.json", "{}"),
        "atomic_write workspace": lambda: atomic_write(
            lattice_dir / "orchestration" / "run-state.md", "x"
        ),
        "jsonl_append": lambda: jsonl_append(
            lattice_dir / "events" / f"{task_id}.jsonl", '{"x":1}\n'
        ),
        "unlink_path": lambda: unlink_path(lattice_dir / "plans" / f"{task_id}.md"),
        "ensure_dir": lambda: ensure_dir(lattice_dir / "resources" / "new-resource"),
    }


@pytest.mark.parametrize("marker", sorted(MARKERS))
@pytest.mark.parametrize(
    "write",
    [
        "atomic_write create",
        "atomic_write replace",
        "atomic_write workspace",
        "jsonl_append",
        "unlink_path",
        "ensure_dir",
    ],
)
def test_primitive_refused_on_marked_board(board: Path, marker: str, write: str) -> None:
    (board / "orchestration").mkdir()
    (board / "orchestration" / "run-state.md").write_text("before")
    _plant(board, marker)
    before = _durable_tree(board)
    error_type, code = MARKERS[marker]
    with pytest.raises(error_type) as exc:
        _primitive_writes(board, _task_id(board))[write]()
    assert exc.value.code == code
    assert _durable_tree(board) == before
    assert not list(board.rglob(".tmp.*"))


def test_cache_message_names_remote_and_project(board: Path) -> None:
    _plant(board, "state")
    with pytest.raises(BoardIsCache) as exc:
        atomic_write(board / "context.md", "x")
    assert exc.value.message == (
        "this is a read-only mirror of studio/apollo; writes go through the server"
    )


def test_hosted_message_names_the_server(board: Path) -> None:
    _plant(board, "hosted")
    with pytest.raises(BoardIsHosted) as exc:
        atomic_write(board / "context.md", "x")
    assert "owned by a Lattice server (srv_1 on atlas pid 4242)" in exc.value.message


def test_board_state(board: Path) -> None:
    assert board_state(board) == "local"
    _plant(board, "hosted")
    assert board_state(board) == "hosted"
    _plant(board, "applying")
    assert board_state(board) == "cache"


@pytest.mark.parametrize("marker", sorted(MARKERS))
def test_runtime_temporary_unmanaged_and_cache_control_stay_writable(
    board: Path, marker: str
) -> None:
    _plant(board, marker)
    atomic_write(board / "locks" / "x.json", "{}")
    ensure_dir(board / "review_state")
    atomic_write(board / "review_state" / "task_1.json", "{}")
    ensure_dir(board / ".daemon")
    jsonl_append(board / ".daemon" / "log.jsonl", "{}\n")
    ensure_dir(board / "tmp-prompts")
    atomic_write(board / "tasks" / ".tmp.manual", "x")
    atomic_write(board / "reviews.md", "unmanaged")
    ensure_dir(board / "exports")
    atomic_write(board / "exports" / "e.json", "{}")
    unlink_path(board / "exports" / "e.json")
    ensure_dir(board / "cache")
    atomic_write(board / "cache" / "follower.json", "{}")


@pytest.mark.parametrize("marker", sorted(MARKERS))
def test_ensure_dir_on_existing_durable_dir_is_allowed(board: Path, marker: str) -> None:
    _plant(board, marker)
    ensure_dir(board / "tasks")
    ensure_dir(board / "sessions" / "archive")


def _live_follower(board: Path) -> None:
    """A follower record this process keeps alive, so reads skip the catch-up."""
    (board / "cache" / "follower.json").write_text(
        json.dumps({"pid": os.getpid(), "stream_live_until": "2999-01-01T00:00:00Z"})
    )


def test_reads_succeed_on_a_cache(board: Path, invoke) -> None:  # noqa: ANN001
    task_id = _task_id(board)
    _plant(board, "state")
    _live_follower(board)
    before = _durable_tree(board)
    assert read_task_authority(board, task_id).snapshot["title"] == "Seed"
    for args in (("list",), ("show", task_id), ("list", "--json")):
        result = invoke(*args)
        assert result.exit_code == 0, (args, result.output)
    # Doctor on a cache also compares it with the server (SPEC §9.6), so with
    # no remote configured it fails with that error; it still writes nothing.
    result = invoke("doctor", "--json")
    assert result.exit_code == 1
    assert json.loads(result.output)["error"]["code"] == "REMOTE_NOT_CONFIGURED"
    assert _durable_tree(board) == before


def test_reads_refuse_an_interrupted_cache(board: Path, invoke) -> None:  # noqa: ANN001
    """``cache/applying`` left behind means a mixed tree (SPEC §9.4)."""
    task_id = _task_id(board)
    _plant(board, "applying")
    _live_follower(board)
    before = _durable_tree(board)
    for args in (("list", "--json"), ("show", task_id, "--json")):
        result = invoke(*args)
        assert result.exit_code == 1, (args, result.output)
        assert json.loads(result.output)["error"]["code"] == "CACHE_INCOMPLETE"
    assert _durable_tree(board) == before


def test_server_control_needs_owner_or_maintenance(board: Path) -> None:
    (board / "hosted").mkdir()
    with pytest.raises(BoardIsHosted):
        atomic_write(board / "hosted" / "journal_meta.json", "{}")
    with owning_board(board):
        atomic_write(board / "hosted" / "journal_meta.json", "{}")
    assert (board / "hosted" / "journal_meta.json").read_text() == "{}"


# ---------------------------------------------------------------------------
# Flags: contextvars, scoped to one board, not inherited by a new thread
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("marker", ["state", "applying"])
def test_syncer_flag_writes_a_cache(board: Path, marker: str) -> None:
    _plant(board, marker)
    with syncing_board(board):
        atomic_write(board / "plans" / "synced.md", "from the server")
        check_board_writable(board)
    assert (board / "plans" / "synced.md").read_text() == "from the server"
    with pytest.raises(BoardIsCache):
        atomic_write(board / "plans" / "synced.md", "after")


def test_owner_flag_writes_a_hosted_board(board: Path) -> None:
    _plant(board, "hosted")
    with owning_board(board):
        atomic_write(board / "context.md", "owned")
    with pytest.raises(BoardIsHosted):
        atomic_write(board / "context.md", "not owned")


def test_owner_flag_does_not_open_a_cache(board: Path) -> None:
    _plant(board, "state")
    with owning_board(board), pytest.raises(BoardIsCache):
        atomic_write(board / "context.md", "x")


def test_flags_apply_to_their_own_board_only(board: Path, tmp_path: Path) -> None:
    other = tmp_path / "other" / ".lattice"
    (other / "plans").mkdir(parents=True)
    _plant(board, "hosted")
    _plant(other, "hosted")
    with owning_board(other), pytest.raises(BoardIsHosted):
        atomic_write(board / "context.md", "x")


def test_flags_do_not_follow_work_into_a_new_thread(board: Path) -> None:
    _plant(board, "hosted")
    errors: list[BaseException] = []

    def write() -> None:
        try:
            atomic_write(board / "context.md", "from a thread")
        except BaseException as exc:  # noqa: BLE001 - collected for the assertion
            errors.append(exc)

    with owning_board(board):
        thread = threading.Thread(target=write)
        thread.start()
        thread.join()
    assert len(errors) == 1 and isinstance(errors[0], BoardIsHosted)


# ---------------------------------------------------------------------------
# execute refuses up front (SPEC §3.2 step 1)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("marker", sorted(MARKERS))
@pytest.mark.parametrize(
    ("op", "params"),
    [
        ("task.create", {"title": "Nope"}),
        ("task.comment", {"task": "TASK", "text": "nope"}),
        ("task.status", {"task": "TASK", "new_status": "in_planning"}),
    ],
)
def test_execute_refuses_a_marked_board(board: Path, marker: str, op: str, params: dict) -> None:
    params = {k: (_task_id(board) if v == "TASK" else v) for k, v in params.items()}
    _plant(board, marker)
    before = _durable_tree(board)
    with pytest.raises(OpError) as exc:
        execute(
            board, op, params, Caller(actor="human:t", origin={"op_id": OP_ID}), run_hooks=True
        )
    assert exc.value.code == MARKERS[marker][1]
    assert _durable_tree(board) == before


def test_execute_runs_for_the_owner(board: Path) -> None:
    _plant(board, "hosted")
    with owning_board(board):
        result = execute(
            board,
            "task.comment",
            {"task": _task_id(board), "text": "hello"},
            Caller(actor="human:t", origin={"op_id": OP_ID}),
            run_hooks=False,
        )
    assert result.events[0]["type"] == "comment_added"


def test_cli_write_on_a_cache_routes_to_its_server(board: Path, invoke) -> None:  # noqa: ANN001
    """A cache marker makes the checkout hosted (SPEC §9.3): a CLI write goes to
    the server, never to the cache; with no remote configured it stops there."""
    _plant(board, "state")
    before = _durable_tree(board)
    result = invoke("create", "Nope", "--actor", "human:t", "--json")
    assert result.exit_code == 1
    error = json.loads(result.output)["error"]
    assert error["code"] == "REMOTE_NOT_CONFIGURED"
    assert "lattice remote add studio <url>" in error["message"]
    assert _durable_tree(board) == before


@pytest.mark.parametrize("as_json", [False, True])
def test_cli_command_not_yet_an_operation_renders_the_refusal(
    board: Path,
    invoke,  # noqa: ANN001
    as_json: bool,
) -> None:
    _plant(board, "hosted")
    before = _durable_tree(board)
    args = ["assign", _task_id(board), "agent:x", "--actor", "human:t"]
    result = invoke(*args, *(["--json"] if as_json else []))
    assert result.exit_code == 1
    assert result.exception is None or isinstance(result.exception, SystemExit)
    if as_json:
        assert json.loads(result.output)["error"]["code"] == "BOARD_IS_HOSTED"
    else:
        assert "Error: this board is owned by a Lattice server" in result.output
    assert _durable_tree(board) == before


# ---------------------------------------------------------------------------
# Board confinement (§6.2)
# ---------------------------------------------------------------------------


def _all_files(root: Path) -> set[str]:
    return {p.relative_to(root).as_posix() for p in root.rglob("*")}


def test_dotdot_escape_refused(board: Path, tmp_path: Path) -> None:
    escapes = [
        lambda: atomic_write(board / "plans" / ".." / ".." / "escape.txt", "x"),
        lambda: jsonl_append(board / "events" / ".." / ".." / "escape.jsonl", "{}\n"),
        lambda: ensure_dir(board / "resources" / ".." / ".." / "escape-dir"),
        lambda: unlink_path(board / ".." / "outside.txt"),
    ]
    (board.parent / "outside.txt").write_text("keep")
    before = _all_files(tmp_path)
    for escape in escapes:
        with pytest.raises(BoardPathError) as exc:
            escape()
        assert exc.value.code == "VALIDATION_ERROR"
    assert _all_files(tmp_path) == before


def test_symlink_out_of_the_board_refused(board: Path, tmp_path: Path) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    (board / "plans" / "link").symlink_to(outside, target_is_directory=True)
    with pytest.raises(BoardPathError):
        atomic_write(board / "plans" / "link" / "x.md", "x")
    assert list(outside.iterdir()) == []


def test_cannot_reach_a_sibling_board(tmp_path: Path) -> None:
    a = tmp_path / "a" / ".lattice"
    b = tmp_path / "b" / ".lattice"
    (a / "plans").mkdir(parents=True)
    (b / "plans").mkdir(parents=True)
    with pytest.raises(BoardPathError):
        atomic_write(a / "plans" / ".." / ".." / ".." / "b" / ".lattice" / "plans" / "x.md", "x")
    assert not (b / "plans" / "x.md").exists()


def test_scope_confines_every_path(tmp_path: Path) -> None:
    a = tmp_path / "a" / ".lattice"
    b = tmp_path / "b" / ".lattice"
    (a / "plans").mkdir(parents=True)
    (b / "plans").mkdir(parents=True)
    with board_scope(a):
        atomic_write(a / "plans" / "ok.md", "x")
        with pytest.raises(BoardPathError):
            atomic_write(b / "plans" / "x.md", "x")
        with pytest.raises(BoardPathError):
            atomic_write(tmp_path / "loose.txt", "x")
    assert not (b / "plans" / "x.md").exists() and not (tmp_path / "loose.txt").exists()
    # Outside any scope, a path that is not under a board is written as always.
    atomic_write(tmp_path / "loose.txt", "x")
    assert (tmp_path / "loose.txt").read_text() == "x"


@dataclass(frozen=True, kw_only=True)
class _EscapeParams:
    target: str


def test_execute_confines_an_operation(
    board: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An operation that builds a path outside its board gets ``VALIDATION_ERROR``."""
    from lattice.ops.base import _REGISTRY, operation

    monkeypatch.setattr("lattice.ops.base._REGISTRY", dict(_REGISTRY))

    @operation("xtest.escape")
    class Escape:
        Params = _EscapeParams
        no_actor = True

        def run(self, ctx, p):  # noqa: ANN001, ANN201
            atomic_write(ctx.lattice_dir / p.target, "escaped")
            return OpResult()

    caller = Caller(origin={"op_id": OP_ID})
    for target in ("/" + str(tmp_path / "abs.txt").lstrip("/"), "../../rel.txt"):
        with pytest.raises(OpError) as exc:
            execute(board, "xtest.escape", {"target": target}, caller, run_hooks=False)
        assert exc.value.code == "VALIDATION_ERROR"
    assert not (tmp_path / "abs.txt").exists()
    assert not (board.parent.parent / "rel.txt").exists()
    execute(board, "xtest.escape", {"target": "reviews.md"}, caller, run_hooks=False)
    assert (board / "reviews.md").read_text() == "escaped"


# ---------------------------------------------------------------------------
# Offline maintenance (§3.5)
# ---------------------------------------------------------------------------

_HOLDER = """
import fcntl, os, sys
fd = os.open(sys.argv[1], os.O_RDWR | os.O_CREAT, 0o600)
fcntl.flock(fd, fcntl.LOCK_EX)
print("held", flush=True)
sys.stdin.read()
"""


@pytest.fixture()
def flock_holder():  # noqa: ANN201
    """Start a process holding a board's owner flock until the test ends."""
    procs: list[subprocess.Popen] = []

    def hold(lattice_dir: Path) -> subprocess.Popen:
        proc = subprocess.Popen(
            [sys.executable, "-c", _HOLDER, str(lattice_dir / "hosted" / "owner.lock")],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            text=True,
        )
        procs.append(proc)
        assert proc.stdout.readline().strip() == "held"
        return proc

    yield hold
    for proc in procs:
        proc.stdin.close()
        proc.wait(timeout=10)


def test_maintenance_needs_a_server_board(board: Path) -> None:
    with pytest.raises(OpError) as exc, offline_maintenance(board, "rebuild"):
        pass
    assert exc.value.code == "VALIDATION_ERROR"
    assert "no hosted/ directory" in exc.value.message


def test_maintenance_refused_while_the_flock_is_held(board: Path, flock_holder) -> None:  # noqa: ANN001
    _plant(board, "hosted")
    flock_holder(board)
    before = _durable_tree(board)
    with pytest.raises(BoardIsHosted) as exc, offline_maintenance(board, "rebuild"):
        pass
    assert "a running Lattice server holds this board" in exc.value.message
    assert not (board / "hosted" / "maintenance.json").exists()
    assert _durable_tree(board) == before


def test_maintenance_holds_the_flock_and_records_itself(board: Path) -> None:
    _plant(board, "hosted")
    with offline_maintenance(board, "doctor --fix"):
        record = json.loads((board / "hosted" / "maintenance.json").read_text())
        assert record["command"] == "doctor --fix" and record["at"].endswith("Z")
        assert try_owner_flock(board) is None  # held by this maintenance
        atomic_write(board / "context.md", "repaired")
        check_board_writable(board)
    assert (board / "context.md").read_text() == "repaired"
    fd = try_owner_flock(board)
    assert fd is not None
    release_owner_flock(fd)
    with pytest.raises(BoardIsHosted):
        atomic_write(board / "context.md", "after")


def test_maintenance_does_not_open_a_cache(board: Path) -> None:
    (board / "hosted").mkdir()
    _plant(board, "state")
    with offline_maintenance(board, "rebuild"), pytest.raises(BoardIsCache):
        atomic_write(board / "context.md", "x")


MAINTENANCE_COMMANDS = [
    ("init",),
    ("demo", "init"),
    ("rebuild",),
    ("doctor",),
    ("backfill-ids",),
    ("migrate", "needs-human"),
]


@pytest.mark.parametrize("command", MAINTENANCE_COMMANDS)
def test_maintenance_commands_take_the_flag(invoke, command: tuple[str, ...]) -> None:  # noqa: ANN001
    result = invoke(*command, "--help")
    assert result.exit_code == 0 and "--offline-maintenance" in result.output


def test_cli_rebuild_offline_maintenance(board: Path, invoke, flock_holder) -> None:  # noqa: ANN001
    task_id = _task_id(board)
    (board / "tasks" / f"{task_id}.json").unlink()  # something for rebuild to repair
    _plant(board, "hosted")

    refused = invoke("rebuild", "--all", "--json")
    assert refused.exit_code == 1
    assert json.loads(refused.output)["error"]["code"] == "BOARD_IS_HOSTED"
    assert not (board / "tasks" / f"{task_id}.json").exists()

    holder = flock_holder(board)
    held = invoke("rebuild", "--all", "--offline-maintenance", "--json")
    assert held.exit_code == 1
    assert json.loads(held.output)["error"]["code"] == "BOARD_IS_HOSTED"
    assert not (board / "hosted" / "maintenance.json").exists()
    holder.stdin.close()
    holder.wait(timeout=10)

    ok = invoke("rebuild", "--all", "--offline-maintenance", "--json")
    assert ok.exit_code == 0, ok.output
    assert (board / "tasks" / f"{task_id}.json").exists()
    assert json.loads((board / "hosted" / "maintenance.json").read_text())["command"] == "rebuild"
    fd = try_owner_flock(board)
    assert fd is not None  # released when the command ended
    release_owner_flock(fd)


# Every maintenance command, gated at entry (SPEC §3.5). The board commands
# run on an initialized server-owned board; init and demo init on a hosted
# scaffold (hosted/ and owner.json, no config.json yet). Each argv is one that
# changes nothing on a fresh board (a dry run, an already-clean doctor, a
# migration with nothing to migrate), so a refusal can only come from the gate.
BOARD_MAINTENANCE = {
    "rebuild": (["rebuild", "--all", "--json"], "rebuild"),
    "doctor --fix": (["doctor", "--fix", "--json"], "doctor --fix"),
    "backfill-ids": (["backfill-ids", "--code", "BF", "--json"], "backfill-ids"),
    "migrate needs-human": (["migrate", "needs-human", "--json"], "migrate needs-human"),
    "migrate needs-human --dry-run": (
        ["migrate", "needs-human", "--dry-run", "--json"],
        "migrate needs-human",
    ),
}


def _error_code(result) -> str | None:  # noqa: ANN001
    try:
        return json.loads(result.output)["error"]["code"]
    except (ValueError, KeyError, TypeError):
        return None


@pytest.mark.parametrize("name", sorted(BOARD_MAINTENANCE))
def test_board_maintenance_command_gated_at_entry(
    board: Path,
    invoke,  # noqa: ANN001
    flock_holder,  # noqa: ANN001
    name: str,
) -> None:
    argv, recorded = BOARD_MAINTENANCE[name]
    _plant(board, "hosted")
    before = _durable_tree(board)

    refused = invoke(*argv)
    assert refused.exit_code == 1 and _error_code(refused) == "BOARD_IS_HOSTED", refused.output
    assert _durable_tree(board) == before
    assert not (board / "hosted" / "maintenance.json").exists()

    holder = flock_holder(board)
    held = invoke(*argv, "--offline-maintenance")
    assert held.exit_code == 1 and _error_code(held) == "BOARD_IS_HOSTED", held.output
    assert "a running Lattice server holds this board" in held.output
    assert _durable_tree(board) == before
    assert not (board / "hosted" / "maintenance.json").exists()
    holder.stdin.close()
    holder.wait(timeout=10)

    ok = invoke(*argv, "--offline-maintenance")
    assert ok.exit_code == 0, ok.output
    assert json.loads(ok.output)["ok"] is True
    record = json.loads((board / "hosted" / "maintenance.json").read_text())
    assert record["command"] == recorded
    fd = try_owner_flock(board)
    assert fd is not None  # released when the command ended
    release_owner_flock(fd)


def test_read_only_doctor_is_not_gated(board: Path, invoke) -> None:  # noqa: ANN001
    _plant(board, "hosted")
    result = invoke("doctor", "--json")
    assert result.exit_code == 0, result.output
    assert not (board / "hosted" / "maintenance.json").exists()


@pytest.mark.parametrize("marker", ["state", "applying"])
@pytest.mark.parametrize("name", sorted(BOARD_MAINTENANCE))
def test_board_maintenance_command_refused_on_a_cache(
    board: Path,
    invoke,  # noqa: ANN001
    marker: str,
    name: str,
) -> None:
    _plant(board, marker)
    before = _durable_tree(board)
    result = invoke(*BOARD_MAINTENANCE[name][0])
    # A cache is a hosted checkout: maintenance belongs on the server (SPEC §3.5).
    assert result.exit_code == 1 and _error_code(result) == "LOCAL_ONLY", result.output
    assert _durable_tree(board) == before


def _hosted_scaffold(root: Path) -> Path:
    lattice_dir = root / ".lattice"
    (lattice_dir / "hosted").mkdir(parents=True)
    _plant(lattice_dir, "hosted")
    return lattice_dir


INIT_COMMANDS = {
    "init": (
        lambda root: ["init", "--path", str(root), "--project-code", "SRV", "--actor", "human:t"],
        "init",
    ),
    "demo init": (lambda root: ["demo", "init", "--path", str(root), "--quiet"], "demo init"),
}


@pytest.mark.parametrize("name", sorted(INIT_COMMANDS))
def test_init_commands_initialize_a_hosted_scaffold_under_maintenance(
    tmp_path: Path,
    cli_runner,  # noqa: ANN001
    flock_holder,  # noqa: ANN001
    monkeypatch: pytest.MonkeyPatch,
    name: str,
) -> None:
    from lattice.cli.main import cli

    monkeypatch.delenv("LATTICE_ROOT", raising=False)
    root = tmp_path / "projects" / "apollo"
    lattice_dir = _hosted_scaffold(root)
    argv = INIT_COMMANDS[name][0](root)

    refused = cli_runner.invoke(cli, argv)
    assert refused.exit_code == 1
    assert "Error: this board is owned by a Lattice server" in refused.output
    assert not (lattice_dir / "config.json").exists()

    holder = flock_holder(lattice_dir)
    held = cli_runner.invoke(cli, [*argv, "--offline-maintenance"])
    assert held.exit_code == 1
    assert "a running Lattice server holds this board" in held.output
    assert not (lattice_dir / "config.json").exists()
    assert not (lattice_dir / "hosted" / "maintenance.json").exists()
    holder.stdin.close()
    holder.wait(timeout=10)

    ok = cli_runner.invoke(cli, [*argv, "--offline-maintenance"])
    assert ok.exit_code == 0, ok.output
    assert "already" not in ok.output
    assert (lattice_dir / "config.json").exists()
    assert list((lattice_dir / "tasks").iterdir()) or name == "init"
    record = json.loads((lattice_dir / "hosted" / "maintenance.json").read_text())
    assert record["command"] == INIT_COMMANDS[name][1]

    # Once initialized, the board is a board: init reports it, demo init refuses.
    again = cli_runner.invoke(cli, [*argv, "--offline-maintenance"])
    if name == "init":
        assert again.exit_code == 0 and "Lattice already initialized" in again.output
    else:
        assert again.exit_code == 1 and "Demo already exists" in again.output


def test_init_on_a_fresh_directory_is_unchanged(
    tmp_path: Path,
    cli_runner,  # noqa: ANN001
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from lattice.cli.main import cli

    monkeypatch.delenv("LATTICE_ROOT", raising=False)
    root = tmp_path / "plain"
    root.mkdir()
    argv = ["init", "--path", str(root), "--project-code", "PLN", "--actor", "human:t"]
    assert cli_runner.invoke(cli, argv).exit_code == 0
    assert (root / ".lattice" / "config.json").exists()
    flagged = cli_runner.invoke(cli, [*argv, "--offline-maintenance"])
    assert flagged.exit_code == 1 and "no hosted/ directory" in flagged.output


def test_cli_offline_maintenance_on_a_local_board(board: Path, invoke) -> None:  # noqa: ANN001
    result = invoke("doctor", "--fix", "--offline-maintenance", "--json")
    assert result.exit_code == 1
    assert json.loads(result.output)["error"]["code"] == "VALIDATION_ERROR"


@pytest.mark.parametrize("marker", ["state", "hosted"])
def test_cli_attach_leaves_no_payload_on_a_marked_board(
    board: Path,
    invoke,  # noqa: ANN001
    tmp_path: Path,
    marker: str,
) -> None:
    source = tmp_path / "evidence.txt"
    source.write_text("evidence")
    _plant(board, marker)
    before = _durable_tree(board)
    result = invoke("attach", _task_id(board), str(source), "--actor", "human:t", "--json")
    assert result.exit_code == 1
    # A cache routes the write to its (here unconfigured) server (SPEC §9.3).
    expected = "REMOTE_NOT_CONFIGURED" if marker == "state" else MARKERS[marker][1]
    assert json.loads(result.output)["error"]["code"] == expected
    assert _durable_tree(board) == before


@pytest.mark.parametrize("marker", sorted(MARKERS))
def test_board_under_an_ancestor_named_lattice_is_still_checked(
    tmp_path: Path, marker: str
) -> None:
    """A board kept below a directory that is itself named ``.lattice`` is
    classified against its own ``.lattice/`` too, so its markers still refuse."""
    board = tmp_path / ".lattice" / "projects" / "apollo" / ".lattice"
    (board / "plans").mkdir(parents=True)
    _plant(board, marker)
    with pytest.raises(MARKERS[marker][0]):
        atomic_write(board / "plans" / "x.md", "x")
    assert not (board / "plans" / "x.md").exists()
    writer = owning_board if marker == "hosted" else syncing_board
    with recording() as recorder, writer(board):
        atomic_write(board / "plans" / "x.md", "x")
    assert recorder.relative_paths(board) == ["plans/x.md"]


# ---------------------------------------------------------------------------
# Symlink aliases into a board are checked as that board
# ---------------------------------------------------------------------------


def _alias_writes(alias: Path, task_id: str) -> dict:
    from lattice.storage.operations import _copy_atomic

    return {
        "atomic_write create": lambda: atomic_write(alias / "plans" / "new.md", "x"),
        "atomic_write replace": lambda: atomic_write(alias / "config.json", "{}"),
        "jsonl_append": lambda: jsonl_append(alias / "events" / f"{task_id}.jsonl", '{"x":1}\n'),
        "placement copy": lambda: _copy_atomic(
            alias / "plans" / f"{task_id}.md", alias / "archive" / "plans" / f"{task_id}.md"
        ),
        "placement unlink": lambda: unlink_path(alias / "plans" / f"{task_id}.md"),
        "ensure_dir": lambda: ensure_dir(alias / "resources" / "new-resource"),
    }


@pytest.mark.parametrize("marker", ["state", "hosted"])
@pytest.mark.parametrize(
    "write",
    [
        "atomic_write create",
        "atomic_write replace",
        "jsonl_append",
        "placement copy",
        "placement unlink",
        "ensure_dir",
    ],
)
def test_symlink_alias_into_a_marked_board_is_refused(
    board: Path, tmp_path: Path, marker: str, write: str
) -> None:
    """``/tmp/alias/plans/x.md`` with ``alias`` -> a marked ``.lattice``: the
    path as written has no ``.lattice`` component."""
    task_id = _task_id(board)
    (board / "archive" / "plans").mkdir(parents=True, exist_ok=True)
    alias = tmp_path / "alias"
    alias.symlink_to(board, target_is_directory=True)
    _plant(board, marker)
    before = _durable_tree(board)
    error_type, code = MARKERS[marker]
    with pytest.raises(error_type) as exc:
        _alias_writes(alias, task_id)[write]()
    assert exc.value.code == code
    assert _durable_tree(board) == before


@pytest.mark.parametrize("marker", ["state", "hosted"])
def test_symlink_alias_to_a_directory_inside_a_marked_board_is_refused(
    board: Path, tmp_path: Path, marker: str
) -> None:
    alias = tmp_path / "plans-alias"
    alias.symlink_to(board / "plans", target_is_directory=True)
    _plant(board, marker)
    for write in (
        lambda: atomic_write(alias / "x.md", "x"),
        lambda: jsonl_append(alias / "x.jsonl", "{}\n"),
        lambda: ensure_dir(alias / "sub"),
        lambda: unlink_path(alias / f"{_task_id(board)}.md"),
    ):
        with pytest.raises(MARKERS[marker][0]):
            write()
    assert sorted(p.name for p in (board / "plans").iterdir()) == [f"{_task_id(board)}.md"]


def test_symlink_alias_writes_are_recorded_as_the_board(board: Path, tmp_path: Path) -> None:
    alias = tmp_path / "alias"
    alias.symlink_to(board, target_is_directory=True)
    with recording() as recorder:
        atomic_write(alias / "plans" / "via-alias.md", "x")
        ensure_dir(alias / "resources" / "via-alias")
    assert recorder.relative_paths(board.resolve()) == [
        "plans/via-alias.md",
        "resources/via-alias",
    ]
