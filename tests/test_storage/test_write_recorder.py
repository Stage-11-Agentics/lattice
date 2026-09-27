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
        ensure_dir(lattice_dir / "orchestration")  # directories are not recorded
        atomic_write(lattice_dir / "orchestration" / "run-state.md", "workspace")
        unlink_path(lattice_dir / "plans" / "missing.md", missing_ok=True)  # no mutation
    assert calls == [("run-state.md", "create")]
    assert recorder.relative_paths(lattice_dir) == ["orchestration/run-state.md"]


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


def test_an_operation_reports_every_durable_path(lattice_dir: Path) -> None:
    """``task.create`` through ``execute``: its event log, snapshot, lifecycle
    entry, short-ID index, and plan scaffold, and nothing else."""
    kinds: dict[str, set[str]] = {}

    def before(path: Path, kind: str) -> None:
        kinds.setdefault(path.relative_to(lattice_dir).as_posix(), set()).add(kind)

    config = json.loads((lattice_dir / "config.json").read_text())
    config["project_code"] = "REC"
    (lattice_dir / "config.json").write_text(json.dumps(config))
    ids_existed = (lattice_dir / "ids.json").exists()
    with recording(before) as recorder:
        result = execute(
            lattice_dir,
            "task.create",
            {"title": "Recorded"},
            Caller(actor="human:t", origin={"op_id": OP_ID}),
            run_hooks=False,
        )
    task_id = result.task["id"]
    expected = {
        f"events/{task_id}.jsonl": {"append"},
        f"tasks/{task_id}.json": {"create"},
        f"plans/{task_id}.md": {"create"},
        "events/_lifecycle.jsonl": {"append"},
        "ids.json": {"replace" if ids_existed else "create"},
    }
    assert result.task["short_id"] == "REC-1"
    assert kinds == expected
    assert recorder.relative_paths(lattice_dir) == sorted(expected)
