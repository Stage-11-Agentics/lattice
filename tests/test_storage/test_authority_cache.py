"""The replay cache the hosted dashboard reads through (AC-42, H-13b): reused
within one scope, revalidated by exact log bytes across scopes, never
consulted outside its context."""

from __future__ import annotations

import os
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


def _count_replays(monkeypatch) -> list[str]:
    calls: list[str] = []
    original = operations.resolve_task_authority

    def counted(ld, task_id, **kw):
        calls.append(task_id)
        return original(ld, task_id, **kw)

    monkeypatch.setattr(operations, "resolve_task_authority", counted)
    return calls


def _read(cache: AuthorityCache, scope: object, board: Path, task: str):
    cache.begin(scope)
    with authority_cache(cache):
        return read_task_authority(board, task)


def _rewrite_same_size(path: Path, old: bytes, new: bytes) -> None:
    """Rewrite *path* in place (same inode), same size, mtime restored."""
    st = path.stat()
    data = path.read_bytes()
    assert len(old) == len(new) and old in data
    with open(path, "r+b") as handle:
        handle.write(data.replace(old, new))
    os.utime(path, ns=(st.st_atime_ns, st.st_mtime_ns))
    after = path.stat()
    assert (after.st_size, after.st_mtime_ns, after.st_ino) == (
        st.st_size,
        st.st_mtime_ns,
        st.st_ino,
    )


def test_one_scope_replays_each_task_once(tmp_path, monkeypatch) -> None:
    board = _board(tmp_path)
    task = _op(board, "task.create", {"title": "a"}).task["id"]
    calls = _count_replays(monkeypatch)
    cache = AuthorityCache()
    first = _read(cache, ("ep", 1), board, task)
    again = _read(cache, ("ep", 1), board, task)
    assert again is first and calls == [task]


def test_a_new_scope_reuses_only_byte_identical_logs(tmp_path, monkeypatch) -> None:
    board = _board(tmp_path)
    a = _op(board, "task.create", {"title": "a"}).task["id"]
    b = _op(board, "task.create", {"title": "b"}).task["id"]
    cache = AuthorityCache()
    _read(cache, ("ep", 1), board, a)
    _read(cache, ("ep", 1), board, b)
    _op(board, "task.comment", {"task": b, "text": "more"})
    calls = _count_replays(monkeypatch)
    assert _read(cache, ("ep", 2), board, a).task_id == a  # unchanged: reused
    assert len(_read(cache, ("ep", 2), board, b).events) == 2  # appended: replayed
    assert calls == [b]


def test_a_same_size_rewrite_with_mtime_restored_is_replayed(tmp_path) -> None:
    board = _board(tmp_path)
    task = _op(board, "task.create", {"title": "aaaa"}).task["id"]
    cache = AuthorityCache()
    assert _read(cache, ("ep", 1), board, task).snapshot["title"] == "aaaa"
    _rewrite_same_size(board / "events" / f"{task}.jsonl", b'"aaaa"', b'"bbbb"')
    assert _read(cache, ("ep", 2), board, task).snapshot["title"] == "bbbb"


def test_a_replaced_file_is_replayed_whatever_its_stat(tmp_path) -> None:
    """A recycled inode with the same size and mtime changes nothing: entries are
    validated by bytes, never by stat."""
    board = _board(tmp_path)
    task = _op(board, "task.create", {"title": "aaaa"}).task["id"]
    cache = AuthorityCache()
    _read(cache, ("ep", 1), board, task)
    path = board / "events" / f"{task}.jsonl"
    st = path.stat()
    replacement = path.with_name("replacement.tmp")
    replacement.write_bytes(path.read_bytes().replace(b'"aaaa"', b'"cccc"'))
    os.replace(replacement, path)
    os.utime(path, ns=(st.st_atime_ns, st.st_mtime_ns))
    assert _read(cache, ("ep", 2), board, task).snapshot["title"] == "cccc"


def test_archive_and_unarchive_move_the_authority(tmp_path) -> None:
    board = _board(tmp_path)
    task = _op(board, "task.create", {"title": "a"}).task["id"]
    cache = AuthorityCache()
    assert _read(cache, 1, board, task).location == "active"
    _op(board, "task.archive", {"task": task})
    assert _read(cache, 2, board, task).location == "archived"
    _op(board, "task.unarchive", {"task": task})
    assert _read(cache, 3, board, task).location == "active"


def test_a_missing_task_is_never_cached(tmp_path) -> None:
    board = _board(tmp_path)
    cache = AuthorityCache()
    cache.begin(1)
    with authority_cache(cache):
        missing = read_task_authority(board, "task_01J9Z000000000000000000000", allow_missing=True)
    assert missing is None and len(cache) == 0


def test_the_byte_budget_evicts_oldest_first(tmp_path) -> None:
    board = _board(tmp_path)
    ids = [_op(board, "task.create", {"title": f"t{n}"}).task["id"] for n in range(3)]
    one = len((board / "events" / f"{ids[0]}.jsonl").read_bytes())
    cache = AuthorityCache(max_bytes=one * 2 + one // 2)
    for task in ids:
        _read(cache, 1, board, task)
    assert len(cache) == 2 and cache.bytes <= cache.max_bytes


def test_outside_the_context_nothing_is_cached(tmp_path, monkeypatch) -> None:
    board = _board(tmp_path)
    task = _op(board, "task.create", {"title": "a"}).task["id"]
    cache = AuthorityCache()
    _read(cache, 1, board, task)
    calls = _count_replays(monkeypatch)
    read_task_authority(board, task)
    read_task_authority(board, task)
    assert calls == [task, task]
