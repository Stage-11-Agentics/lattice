"""An erased task refuses H-4's writes with ``TASK_ERASED`` (H-7's tombstone guard)."""

from __future__ import annotations

from pathlib import Path

import pytest

from lattice.boards import LocalBoard, resolve_board
from lattice.ops import Caller, OpError
from lattice.ops.task_attach import encode_payload

A = Caller(actor="agent:t")


@pytest.fixture()
def erased(initialized_root: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[LocalBoard, str]:
    monkeypatch.delenv("LATTICE_ROOT", raising=False)
    board = resolve_board(initialized_root)
    task_id = board.execute("task.create", {"title": "T"}, A).value["id"]
    board.execute(
        "task.status",
        {"task": task_id, "new_status": "review", "force": True, "reason": "setup"},
        A,
    )
    board.execute("task.erase", {"task": task_id, "reason": "mistake"}, A)
    return board, task_id


@pytest.mark.parametrize(
    ("op", "params"),
    [
        ("task.attach", {"payload": encode_payload("f.md", b"x")}),
        ("task.attach", {"inline": "x"}),
        ("task.complete", {"review": "ok"}),
        ("task.plan_write", {"stdin": "# Plan"}),
        ("task.notes_write", {"stdin": "notes"}),
    ],
)
def test_erased_task_refuses(erased: tuple[LocalBoard, str], op: str, params: dict) -> None:
    board, task_id = erased
    payloads = board.lattice_dir / "artifacts" / "payload"
    before = sorted(payloads.iterdir()) if payloads.exists() else []
    with pytest.raises(OpError) as exc:
        board.execute(op, {"task": task_id, **params}, A)
    assert exc.value.code == "TASK_ERASED"
    assert (sorted(payloads.iterdir()) if payloads.exists() else []) == before


def test_erased_task_refuses_through_the_cli(erased, invoke, tmp_path: Path) -> None:  # noqa: ANN001
    _board, task_id = erased
    src = tmp_path / "f.md"
    src.write_text("x")
    for args in (
        ("attach", task_id, str(src)),
        ("complete", task_id, "--review", "ok"),
        ("plan", "write", task_id, "--file", str(src)),
    ):
        result = invoke(*args, "--actor", "agent:t", "--json")
        assert result.exit_code == 1
        assert '"TASK_ERASED"' in result.output, args
