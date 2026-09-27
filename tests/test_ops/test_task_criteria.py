"""``task.criterion_add``, ``task.criterion_edit``, ``task.criterion_retire``
called directly (no CLI)."""

from __future__ import annotations

from pathlib import Path

import pytest

from lattice.boards import LocalBoard, resolve_board
from lattice.ops import Caller, OpError


@pytest.fixture()
def board(initialized_root: Path, monkeypatch: pytest.MonkeyPatch) -> LocalBoard:
    monkeypatch.delenv("LATTICE_ROOT", raising=False)
    return resolve_board(initialized_root)


@pytest.fixture()
def task(board: LocalBoard) -> str:
    return _run(board, "task.create", {"title": "t"}).value["id"]


def _run(board: LocalBoard, op: str, params: dict):  # noqa: ANN202
    return board.execute(op, params, Caller(actor="agent:t"))


def _error(board: LocalBoard, op: str, params: dict) -> OpError:
    with pytest.raises(OpError) as exc:
        _run(board, op, params)
    return exc.value


class TestCriterionAdd:
    def test_allocates_ids_and_trims(self, board: LocalBoard, task: str) -> None:
        result = _run(board, "task.criterion_add", {"task": task, "outcome": "  One  "})
        assert result.events[0]["type"] == "acceptance_criterion_added"
        assert result.events[0]["data"] == {
            "criterion_id": "AC-1",
            "outcome": "One",
            "revision": 1,
        }
        assert set(result.value) == {"task_id", "criterion", "snapshot"}
        assert result.value["task_id"] == task
        assert result.value["criterion"]["id"] == "AC-1"
        assert result.value["snapshot"] == result.task
        assert not result.idempotent

        result = _run(board, "task.criterion_add", {"task": task, "file": "Two\n"})
        assert result.value["criterion"]["id"] == "AC-2"
        assert result.value["criterion"]["outcome"] == "Two"

    def test_explicit_id_is_idempotent_on_same_prose(self, board: LocalBoard, task: str) -> None:
        params = {"task": task, "outcome": "Holds", "id": "c-one"}
        _run(board, "task.criterion_add", params)
        again = _run(board, "task.criterion_add", params)
        assert again.idempotent
        assert again.events == []
        assert again.value["criterion"]["id"] == "c-one"

        err = _error(board, "task.criterion_add", {**params, "outcome": "Different"})
        assert (err.code, err.message) == (
            "VALIDATION_ERROR",
            "Acceptance criterion c-one already exists with different initial prose.",
        )

    @pytest.mark.parametrize(
        ("params", "message"),
        [
            ({"outcome": "a", "file": "b"}, "Provide either OUTCOME or --file, not both."),
            ({}, "Provide acceptance-criterion outcome as OUTCOME or via --file."),
            ({"outcome": "   "}, "Acceptance-criterion outcome must be non-empty."),
            (
                {"outcome": "x", "id": "Bad Id!"},
                "Criterion ID must match ^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$.",
            ),
        ],
    )
    def test_input_rejections(self, board: LocalBoard, params: dict, message: str) -> None:
        # Input rules: refused before the task is looked up.
        err = _error(board, "task.criterion_add", {"task": "NOPE-1", **params})
        assert (err.code, err.message) == ("VALIDATION_ERROR", message)

    def test_unknown_task(self, board: LocalBoard) -> None:
        err = _error(board, "task.criterion_add", {"task": "NOPE-1", "outcome": "x"})
        assert (err.code, err.message) == ("NOT_FOUND", "Short ID 'NOPE-1' not found.")


class TestCriterionEditRetire:
    def test_edit_retire_lifecycle(self, board: LocalBoard, task: str) -> None:
        _run(board, "task.criterion_add", {"task": task, "outcome": "One", "id": "c"})
        result = _run(
            board, "task.criterion_edit", {"task": task, "criterion_id": "c", "outcome": "Uno"}
        )
        assert result.events[0]["data"] == {
            "criterion_id": "c",
            "from_outcome": "One",
            "outcome": "Uno",
            "revision": 2,
        }
        assert result.value["criterion"]["revision"] == 2

        same = _run(
            board, "task.criterion_edit", {"task": task, "criterion_id": "c", "file": " Uno\n"}
        )
        assert same.idempotent and same.events == []
        assert same.value["criterion"]["outcome"] == "Uno"

        result = _run(board, "task.criterion_retire", {"task": task, "criterion_id": "c"})
        assert result.events[0]["data"] == {"criterion_id": "c", "revision": 2}
        assert result.value["criterion"]["retired"] is True

        err = _error(board, "task.criterion_retire", {"task": task, "criterion_id": "c"})
        assert (err.code, err.message) == (
            "VALIDATION_ERROR",
            "Acceptance criterion c is already retired.",
        )
        err = _error(
            board, "task.criterion_edit", {"task": task, "criterion_id": "c", "outcome": "x"}
        )
        assert (err.code, err.message) == (
            "VALIDATION_ERROR",
            "Acceptance criterion c is retired.",
        )

    def test_missing_criterion(self, board: LocalBoard, task: str) -> None:
        for op, extra in (
            ("task.criterion_edit", {"outcome": "x"}),
            ("task.criterion_retire", {}),
        ):
            err = _error(board, op, {"task": task, "criterion_id": "nope", **extra})
            assert (err.code, err.message) == (
                "VALIDATION_ERROR",
                "Acceptance criterion nope not found.",
            )

    def test_input_order(self, board: LocalBoard) -> None:
        # The ID is checked before the outcome, and both before the task.
        err = _error(board, "task.criterion_edit", {"task": "NOPE-1", "criterion_id": "!"})
        assert err.message.startswith("Criterion ID must match")
        err = _error(board, "task.criterion_edit", {"task": "NOPE-1", "criterion_id": "c"})
        assert err.message == "Provide acceptance-criterion outcome as OUTCOME or via --file."
        err = _error(board, "task.criterion_retire", {"task": "NOPE-1", "criterion_id": "!"})
        assert err.message.startswith("Criterion ID must match")


class TestStorageErrors:
    def test_missing_archived_and_corrupt(self, board: LocalBoard, task: str) -> None:
        from click.testing import CliRunner

        from lattice.cli.main import cli

        missing = "task_01JZZZZZZZZZZZZZZZZZZZZZZZ"
        corrupt = _run(board, "task.create", {"title": "c"}).value["id"]
        _run(board, "task.criterion_add", {"task": corrupt, "outcome": "x", "id": "c"})
        with (board.lattice_dir / "events" / f"{corrupt}.jsonl").open("a") as f:
            f.write("{not json\n")
        result = CliRunner().invoke(
            cli, ["archive", task, "--actor", "agent:t"], env={"LATTICE_ROOT": str(board.root)}
        )
        assert result.exit_code == 0, result.output
        for op, extra in (
            ("task.criterion_add", {"outcome": "y"}),
            ("task.criterion_edit", {"criterion_id": "c", "outcome": "y"}),
            ("task.criterion_retire", {"criterion_id": "c"}),
        ):
            err = _error(board, op, {"task": missing, **extra})
            assert (err.code, err.message) == ("NOT_FOUND", f"Task {missing} does not exist."), op
            err = _error(board, op, {"task": task, **extra})
            assert (err.code, err.message) == ("NOT_FOUND", f"Task {task} is archived."), op
            err = _error(board, op, {"task": corrupt, **extra})
            assert err.code == "INTEGRITY_ERROR", op
            assert "invalid JSONL record" in err.message, op
