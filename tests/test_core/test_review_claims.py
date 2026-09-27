"""Review-slot claims carry a token; a displaced claim's writes and clear no-op."""

from __future__ import annotations

from pathlib import Path

from lattice.core.review import (
    claim_review_state,
    clear_owned_review_state,
    read_review_state,
    take_over_review_state,
    write_owned_review_state,
)


def _record(task: str, pid: int) -> dict:
    return {
        "task_id": task,
        "mode": "single",
        "review_type": "code-review",
        "started_at": "t",
        "started_by_pid": pid,
        "auto_fired": False,
        "agents": [],
    }


def test_a_displaced_claim_neither_writes_nor_clears(tmp_path: Path) -> None:
    lattice = tmp_path / ".lattice"
    lattice.mkdir()
    claimed, state = claim_review_state(
        lattice,
        "task_1",
        mode="single",
        review_type="code-review",
        started_by_pid=11,
        auto_fired=False,
    )
    assert claimed and state is not None
    first = state["claim"]
    assert write_owned_review_state(lattice, {**_record("task_1", 11), "agents": ["a"]}, first)

    second = take_over_review_state(lattice, _record("task_1", 22))
    assert second != first
    assert not write_owned_review_state(
        lattice, {**_record("task_1", 11), "status": "failed"}, first
    )
    assert not clear_owned_review_state(lattice, "task_1", first)
    held = read_review_state(lattice, "task_1")
    assert held is not None and held["started_by_pid"] == 22 and held["claim"] == second

    assert clear_owned_review_state(lattice, "task_1", second)
    assert read_review_state(lattice, "task_1") is None
    # A cleared slot is not recreated by a stale claim.
    assert not write_owned_review_state(lattice, _record("task_1", 11), first)
    assert read_review_state(lattice, "task_1") is None


def test_a_caller_without_a_claim_writes_as_before(tmp_path: Path) -> None:
    lattice = tmp_path / ".lattice"
    lattice.mkdir()
    take_over_review_state(lattice, _record("task_2", 22))
    assert write_owned_review_state(lattice, _record("task_2", 33), None)
    assert read_review_state(lattice, "task_2")["started_by_pid"] == 33
    assert clear_owned_review_state(lattice, "task_2", None)
    assert read_review_state(lattice, "task_2") is None
