"""The opt-in replay cache the hosted dashboard reads through (AC-42, H-13b)."""

from __future__ import annotations

from pathlib import Path

from ulid import ULID

from lattice.ops import Caller, execute
from lattice.storage import operations
from lattice.storage.board_init import create_board
from lattice.storage.operations import AuthorityCache, authority_cache, read_task_authority


def _op(board: Path, name: str, params: dict):
    caller = Caller(actor="human:t", origin={"op_id": f"op_{ULID()}"})
    return execute(board, name, params, caller, run_hooks=False)


def _board(tmp_path: Path) -> Path:
    create_board(tmp_path, project_code="CAC", actor="human:t")
    return tmp_path / ".lattice"


def _counting(monkeypatch) -> list[str]:
    calls: list[str] = []
    original = operations._read_task_authority_locked

    def counted(ld, task_id, **kw):
        calls.append(task_id)
        return original(ld, task_id, **kw)

    monkeypatch.setattr(operations, "_read_task_authority_locked", counted)
    return calls


def test_unchanged_logs_are_not_replayed_again(tmp_path, monkeypatch) -> None:
    board = _board(tmp_path)
    task = _op(board, "task.create", {"title": "a"}).task["id"]
    calls = _counting(monkeypatch)
    cache = AuthorityCache()
    with authority_cache(cache):
        first = read_task_authority(board, task)
        again = read_task_authority(board, task)
    assert again is first and calls == [task]


def test_an_append_invalidates(tmp_path, monkeypatch) -> None:
    board = _board(tmp_path)
    task = _op(board, "task.create", {"title": "a"}).task["id"]
    cache = AuthorityCache()
    with authority_cache(cache):
        before = read_task_authority(board, task)
    _op(board, "task.comment", {"task": task, "text": "more"})
    with authority_cache(cache):
        after = read_task_authority(board, task)
    assert len(after.events) == len(before.events) + 1


def test_archive_and_unarchive_move_the_authority(tmp_path) -> None:
    board = _board(tmp_path)
    task = _op(board, "task.create", {"title": "a"}).task["id"]
    cache = AuthorityCache()
    with authority_cache(cache):
        assert read_task_authority(board, task).location == "active"
    _op(board, "task.archive", {"task": task})
    with authority_cache(cache):
        assert read_task_authority(board, task).location == "archived"
    _op(board, "task.unarchive", {"task": task})
    with authority_cache(cache):
        assert read_task_authority(board, task).location == "active"


def test_a_missing_task_is_never_cached(tmp_path) -> None:
    board = _board(tmp_path)
    cache = AuthorityCache()
    with authority_cache(cache):
        assert (
            read_task_authority(board, "task_01J9Z000000000000000000000", allow_missing=True)
            is None
        )
    assert len(cache) == 0


def test_the_byte_budget_evicts_oldest_first(tmp_path) -> None:
    board = _board(tmp_path)
    ids = [_op(board, "task.create", {"title": f"t{n}"}).task["id"] for n in range(3)]
    one = len((board / "events" / f"{ids[0]}.jsonl").read_bytes())
    cache = AuthorityCache(max_bytes=one * 2 + one // 2)
    with authority_cache(cache):
        for task in ids:
            read_task_authority(board, task)
    assert len(cache) == 2 and cache.bytes <= cache.max_bytes


def test_outside_the_context_nothing_is_cached(tmp_path, monkeypatch) -> None:
    board = _board(tmp_path)
    task = _op(board, "task.create", {"title": "a"}).task["id"]
    calls = _counting(monkeypatch)
    read_task_authority(board, task)
    read_task_authority(board, task)
    assert calls == [task, task]
