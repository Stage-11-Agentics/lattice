"""Real boards at SPEC §8.8's supported size, built through operations.

The board is written locally with ``lattice.ops.execute`` (fast: no server
round trip per write), then served by the real server through
``serve_board(source=...)``, which places it with a fresh journal.
"""

from __future__ import annotations

from pathlib import Path

from ulid import ULID

from lattice.ops import Caller, execute
from lattice.storage.board_init import create_board

MIB = 1024 * 1024
_COMMENT = 60_000  # bytes of text per comment that grows the hot log
_PLAN = 5 * MIB  # bytes per padding plan


def _op(board: Path, name: str, params: dict) -> object:
    caller = Caller(actor="human:envelope", origin={"op_id": f"op_{ULID()}"})
    return execute(board, name, params, caller, run_hooks=False)


def durable_bytes(board: Path) -> int:
    from tests.test_remote.stub_sync_server import durable_files

    return sum(path.stat().st_size for path in durable_files(board).values())


def build_envelope(
    root: Path,
    *,
    tasks: int = 2000,
    hot_log_bytes: int = 4 * MIB,
    total_bytes: int | None = None,
) -> str:
    """Create a board under *root* with *tasks* tasks, one whose event log holds
    *hot_log_bytes* of real comments, padded with real plans until it holds
    *total_bytes* of durable data. Returns the hot task's ID."""
    root.mkdir(parents=True, exist_ok=True)
    create_board(root, project_code="ENV", actor="human:envelope")
    board = root / ".lattice"
    ids = [_op(board, "task.create", {"title": f"Task {n}"}).task["id"] for n in range(tasks)]
    hot = ids[0]
    hot_log = board / "events" / f"{hot}.jsonl"
    n = 0
    while hot_log.stat().st_size < hot_log_bytes:
        _op(board, "task.comment", {"task": hot, "text": f"history {n} " + "x" * _COMMENT})
        n += 1
    if total_bytes:
        index = 1
        while durable_bytes(board) < total_bytes:
            plan = f"# Plan {index}\n\n" + "p" * _PLAN
            _op(board, "task.plan_write", {"task": ids[index], "file": plan})
            index += 1
    return hot
