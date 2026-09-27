"""``task.plan_write`` with ``if_absent``: create the plan only when none exists.

The dashboard scaffolds a missing plan with it (H-13a), so a plan authored
between the dashboard's check and its write is never replaced. The check runs
under the task lock; every other caller keeps today's behavior (the default).
"""

from __future__ import annotations

import dataclasses
import json
from pathlib import Path

import pytest

from lattice.boards import LocalBoard, resolve_board
from lattice.ops import Caller, OpError, get_operation, parse_params
from lattice.storage.operations import TaskMutationDecision, mutate_task

A = Caller(actor="agent:t")


@pytest.fixture()
def board_task(initialized_root: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[LocalBoard, str]:
    monkeypatch.delenv("LATTICE_ROOT", raising=False)
    board = resolve_board(initialized_root)
    task_id = board.execute("task.create", {"title": "T"}, A).value["id"]
    (board.lattice_dir / "plans" / f"{task_id}.md").unlink()  # create scaffolds one
    return board, task_id


def _events(board: LocalBoard, task_id: str) -> list[dict]:
    path = board.lattice_dir / "events" / f"{task_id}.jsonl"
    return [json.loads(line) for line in path.read_text().splitlines()]


def test_absent_plan_is_created(board_task) -> None:  # noqa: ANN001
    board, task_id = board_task
    result = board.execute(
        "task.plan_write", {"task": task_id, "stdin": "# Scaffold\n", "if_absent": True}, A
    )
    assert (board.lattice_dir / "plans" / f"{task_id}.md").read_text() == "# Scaffold\n"
    assert result.events[-1]["type"] == "plan_written"


def test_existing_plan_is_refused_and_left_alone(board_task) -> None:  # noqa: ANN001
    board, task_id = board_task
    plan = board.lattice_dir / "plans" / f"{task_id}.md"
    plan.write_text("# Authored\n\nReal plan.\n")
    before = _events(board, task_id)
    with pytest.raises(OpError) as exc:
        board.execute(
            "task.plan_write", {"task": task_id, "stdin": "# Scaffold\n", "if_absent": True}, A
        )
    assert exc.value.code == "CONFLICT"
    assert exc.value.details["reason"] == "ALREADY_EXISTS"
    assert exc.value.details["snapshot"]["id"] == task_id
    assert plan.read_text() == "# Authored\n\nReal plan.\n"
    assert _events(board, task_id) == before


def test_plan_written_while_the_caller_waits_for_the_lock_survives(board_task) -> None:  # noqa: ANN001
    """Deterministic interleave: the plan appears after the caller saw none but
    before the operation holds the task lock; the check inside the lock wins."""
    board, task_id = board_task
    plan = board.lattice_dir / "plans" / f"{task_id}.md"
    assert not plan.exists()  # the caller's observation

    def author(context):  # noqa: ANN001, ANN202 - another writer, holding the lock first
        plan.write_text("# Authored meanwhile\n")
        return TaskMutationDecision(idempotent=True)

    mutate_task(board.lattice_dir, task_id, author, board.load_config(), run_hooks=False)
    with pytest.raises(OpError) as exc:
        board.execute(
            "task.plan_write", {"task": task_id, "stdin": "# Scaffold\n", "if_absent": True}, A
        )
    assert exc.value.details["reason"] == "ALREADY_EXISTS"
    assert plan.read_text() == "# Authored meanwhile\n"


def test_default_keeps_todays_replace_behavior(board_task) -> None:  # noqa: ANN001
    board, task_id = board_task
    plan = board.lattice_dir / "plans" / f"{task_id}.md"
    plan.write_text("# Old\n")
    board.execute("task.plan_write", {"task": task_id, "stdin": "# New\n"}, A)
    assert plan.read_text() == "# New\n"


def test_if_absent_with_expect_sha256_is_refused(board_task) -> None:  # noqa: ANN001
    board, task_id = board_task
    with pytest.raises(OpError) as exc:
        board.execute(
            "task.plan_write",
            {"task": task_id, "stdin": "x", "if_absent": True, "expect_sha256": "0" * 64},
            A,
        )
    assert exc.value.code == "VALIDATION_ERROR"


def test_if_absent_defaults_off_so_clients_omit_it() -> None:
    """SPEC §15: a parameter equal to its default is omitted on the wire, so a
    request without it (every existing client) means today's behavior."""
    params_cls = get_operation("task.plan_write").Params
    field = next(f for f in dataclasses.fields(params_cls) if f.name == "if_absent")
    assert field.default is False
    parsed = parse_params(params_cls, {"task": "task_x", "stdin": "x"})
    assert parsed.if_absent is False


def test_notes_write_has_no_if_absent() -> None:
    with pytest.raises(OpError) as exc:
        parse_params(
            get_operation("task.notes_write").Params,
            {"task": "task_x", "stdin": "x", "if_absent": True},
        )
    assert exc.value.code == "VALIDATION_ERROR"
