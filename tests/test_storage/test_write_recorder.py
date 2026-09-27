"""The write recorder (SPEC §8.5, H-8): every durable path a primitive writes,
appends, or unlinks is recorded, and a callback runs before each mutation with
its kind. H-22a writes undo entries from the callback; H-9 returns the path set
with the operation's result."""

from __future__ import annotations

import json
import threading
from pathlib import Path

import pytest

from lattice.ops import Caller, execute
from lattice.storage.fs import (
    WriteRecorder,
    atomic_write,
    ensure_dir,
    jsonl_append,
    recording,
    unlink_path,
)

OP_ID = "op_01J9ZABCDEFGHJKMNPQRSTVWXY"


@pytest.fixture()
def lattice_dir(initialized_root: Path) -> Path:
    return (initialized_root / ".lattice").resolve()


def test_kinds_and_callback_runs_before_each_mutation(lattice_dir: Path) -> None:
    seen: list[tuple[str, str, bytes | None]] = []

    def before(path: Path, kind: str) -> None:
        # The state the callback sees is the pre-image.
        seen.append((path.name, kind, path.read_bytes() if path.exists() else None))

    plan = lattice_dir / "plans" / "p.md"
    log = lattice_dir / "events" / "task_x.jsonl"
    with recording(before) as recorder:
        atomic_write(plan, "one")
        atomic_write(plan, "two")
        jsonl_append(log, '{"a":1}\n')
        jsonl_append(log, '{"b":2}\n')
        unlink_path(plan)
    assert seen == [
        ("p.md", "create", None),
        ("p.md", "replace", b"one"),
        ("task_x.jsonl", "append", None),
        ("task_x.jsonl", "append", b'{"a":1}\n'),
        ("p.md", "unlink", b"two"),
    ]
    assert recorder.paths == [plan, log]
    assert recorder.relative_paths(lattice_dir) == ["events/task_x.jsonl", "plans/p.md"]


def test_a_raising_callback_aborts_the_write(lattice_dir: Path) -> None:
    plan = lattice_dir / "plans" / "keep.md"
    log = lattice_dir / "events" / "task_x.jsonl"
    atomic_write(plan, "original")
    jsonl_append(log, '{"a":1}\n')

    def refuse(path: Path, kind: str) -> None:
        raise RuntimeError(f"no {kind}")

    with recording(refuse) as recorder:
        for write in (
            lambda: atomic_write(plan, "changed"),
            lambda: atomic_write(lattice_dir / "plans" / "new.md", "x"),
            lambda: jsonl_append(log, '{"b":2}\n'),
            lambda: unlink_path(plan),
        ):
            with pytest.raises(RuntimeError):
                write()
    assert plan.read_text() == "original"
    assert log.read_text() == '{"a":1}\n'
    assert not (lattice_dir / "plans" / "new.md").exists()
    assert recorder.paths == []


def test_only_durable_and_workspace_paths_are_recorded(lattice_dir: Path, tmp_path: Path) -> None:
    calls: list[tuple[str, str]] = []
    with recording(lambda path, kind: calls.append((path.name, kind))) as recorder:
        atomic_write(lattice_dir / "locks" / "x.json", "{}")
        ensure_dir(lattice_dir / "review_state")
        atomic_write(lattice_dir / "review_state" / "r.json", "{}")
        atomic_write(lattice_dir / "reviews.md", "unmanaged")
        ensure_dir(lattice_dir / "cache")
        atomic_write(lattice_dir / "cache" / "follower.json", "{}")
        atomic_write(tmp_path / "outside.txt", "not a board")
        ensure_dir(lattice_dir / "orchestration")
        atomic_write(lattice_dir / "orchestration" / "run-state.md", "workspace")
        ensure_dir(lattice_dir / "orchestration")  # exists: no mutation
        unlink_path(lattice_dir / "plans" / "missing.md", missing_ok=True)  # no mutation
    assert calls == [("orchestration", "create"), ("run-state.md", "create")]
    assert recorder.relative_paths(lattice_dir) == [
        "orchestration",
        "orchestration/run-state.md",
    ]


def test_no_recorder_outside_a_recording_block(lattice_dir: Path) -> None:
    calls: list[str] = []
    with recording(lambda path, kind: calls.append(kind)):
        pass
    atomic_write(lattice_dir / "plans" / "later.md", "x")
    assert calls == []


def test_recorder_does_not_follow_work_into_a_new_thread(lattice_dir: Path) -> None:
    with recording() as recorder:
        thread = threading.Thread(
            target=atomic_write, args=(lattice_dir / "plans" / "thread.md", "x")
        )
        thread.start()
        thread.join()
        atomic_write(lattice_dir / "plans" / "here.md", "x")
    assert recorder.relative_paths(lattice_dir) == ["plans/here.md"]


def test_nested_recording_is_its_own(lattice_dir: Path) -> None:
    with recording() as outer:
        atomic_write(lattice_dir / "plans" / "a.md", "x")
        with recording() as inner:
            atomic_write(lattice_dir / "plans" / "b.md", "x")
        atomic_write(lattice_dir / "plans" / "c.md", "x")
    assert outer.relative_paths(lattice_dir) == ["plans/a.md", "plans/c.md"]
    assert inner.relative_paths(lattice_dir) == ["plans/b.md"]


def test_recorder_repr_and_default(lattice_dir: Path) -> None:
    recorder = WriteRecorder()
    assert recorder.callback is None and recorder.paths == []


def test_ensure_dir_reports_each_directory_it_creates(lattice_dir: Path) -> None:
    seen: list[tuple[str, str, bool]] = []

    def before(path: Path, kind: str) -> None:
        seen.append((path.relative_to(lattice_dir).as_posix(), kind, path.exists()))

    with recording(before) as recorder:
        ensure_dir(lattice_dir / "resources" / "db" / "shards")
    # Missing parents first, each reported before its mkdir.
    assert seen == [("resources/db", "create", False), ("resources/db/shards", "create", False)]
    assert recorder.relative_paths(lattice_dir) == ["resources/db", "resources/db/shards"]
    assert (lattice_dir / "resources" / "db" / "shards").is_dir()


def test_ensure_dir_callback_raising_leaves_no_directory(lattice_dir: Path) -> None:
    def refuse(path: Path, kind: str) -> None:
        raise RuntimeError("no")

    with recording(refuse), pytest.raises(RuntimeError):
        ensure_dir(lattice_dir / "resources" / "db" / "shards")
    assert not (lattice_dir / "resources" / "db").exists()

    def refuse_leaf(path: Path, kind: str) -> None:
        if path.name == "shards":
            raise RuntimeError("no")

    with recording(refuse_leaf) as recorder, pytest.raises(RuntimeError):
        ensure_dir(lattice_dir / "resources" / "db" / "shards")
    # The parent was reported before it was made; the refused leaf was never made.
    assert recorder.relative_paths(lattice_dir) == ["resources/db"]
    assert (lattice_dir / "resources" / "db").is_dir()
    assert not (lattice_dir / "resources" / "db" / "shards").exists()


def _caller() -> Caller:
    return Caller(actor="human:t", origin={"op_id": OP_ID})


def _with_project_code(lattice_dir: Path) -> None:
    config = json.loads((lattice_dir / "config.json").read_text())
    config["project_code"] = "REC"
    (lattice_dir / "config.json").write_text(json.dumps(config))


def test_execute_owns_the_recorder_and_returns_its_paths(lattice_dir: Path) -> None:
    """``task.create`` through ``execute`` alone: the callback sees every durable
    mutation before it happens, with its kind, and ``OpResult.paths`` lists
    exactly its event log, snapshot, lifecycle entry, short-ID index, and plan."""
    _with_project_code(lattice_dir)
    ids_existed = (lattice_dir / "ids.json").exists()
    seen: list[tuple[str, str, int | None]] = []

    def before(path: Path, kind: str) -> None:
        size = path.stat().st_size if path.exists() else None
        seen.append((path.relative_to(lattice_dir).as_posix(), kind, size))

    lifecycle_size = (lattice_dir / "events" / "_lifecycle.jsonl").stat().st_size
    result = execute(
        lattice_dir,
        "task.create",
        {"title": "Recorded"},
        _caller(),
        run_hooks=False,
        on_mutation=before,
    )
    task_id = result.task["id"]
    assert result.task["short_id"] == "REC-1"
    expected = {
        f"events/{task_id}.jsonl": ("append", None),
        f"tasks/{task_id}.json": ("create", None),
        f"plans/{task_id}.md": ("create", None),
        "events/_lifecycle.jsonl": ("append", lifecycle_size),
        "ids.json": ("replace", None) if ids_existed else ("create", None),
    }
    assert {path: (kind, size) for path, kind, size in seen if path != "ids.json"} == {
        k: v for k, v in expected.items() if k != "ids.json"
    }
    assert {kind for path, kind, _ in seen if path == "ids.json"} == {expected["ids.json"][0]}
    assert result.paths == tuple(sorted(expected))


def test_each_execute_call_has_its_own_paths(lattice_dir: Path) -> None:
    created = execute(lattice_dir, "task.create", {"title": "A"}, _caller(), run_hooks=False)
    task_id = created.task["id"]
    other_op = "op_01J9ZABCDEFGHJKMNPQRSTVWXZ"
    with recording() as outer:
        commented = execute(
            lattice_dir,
            "task.comment",
            {"task": task_id, "text": "hi"},
            Caller(actor="human:t", origin={"op_id": other_op}),
            run_hooks=False,
        )
    assert commented.paths == (f"events/{task_id}.jsonl", f"tasks/{task_id}.json")
    assert outer.paths == []  # the call's own recorder, not an enclosing one


def test_execute_callback_raising_aborts_before_any_write(lattice_dir: Path) -> None:
    created = execute(lattice_dir, "task.create", {"title": "A"}, _caller(), run_hooks=False)
    task_id = created.task["id"]
    log = lattice_dir / "events" / f"{task_id}.jsonl"
    before_bytes = log.read_bytes()

    def refuse(path: Path, kind: str) -> None:
        raise RuntimeError(f"undo log full: {kind} {path.name}")

    with pytest.raises(RuntimeError, match="append"):
        execute(
            lattice_dir,
            "task.comment",
            {"task": task_id, "text": "hi"},
            _caller(),
            run_hooks=False,
            on_mutation=refuse,
        )
    assert log.read_bytes() == before_bytes


def test_execute_in_a_worker_thread_records_there(lattice_dir: Path) -> None:
    results: list = []
    thread = threading.Thread(
        target=lambda: results.append(
            execute(lattice_dir, "task.create", {"title": "T"}, _caller(), run_hooks=False)
        )
    )
    thread.start()
    thread.join()
    task_id = results[0].task["id"]
    assert f"events/{task_id}.jsonl" in results[0].paths
