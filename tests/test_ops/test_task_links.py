"""``task.link``, ``task.unlink``, ``task.branch_link``, ``task.branch_unlink``,
``task.file_link``, ``task.file_unlink`` called directly (no CLI)."""

from __future__ import annotations

from pathlib import Path

import pytest

from lattice.boards import LocalBoard, resolve_board
from lattice.ops import Caller, OpError


@pytest.fixture()
def board(initialized_root: Path, monkeypatch: pytest.MonkeyPatch) -> LocalBoard:
    monkeypatch.delenv("LATTICE_ROOT", raising=False)
    return resolve_board(initialized_root)


def _run(board: LocalBoard, op: str, params: dict):  # noqa: ANN202
    return board.execute(op, params, Caller(actor="agent:t"))


def _error(board: LocalBoard, op: str, params: dict) -> OpError:
    with pytest.raises(OpError) as exc:
        _run(board, op, params)
    return exc.value


def _task(board: LocalBoard, title: str = "t") -> str:
    return _run(board, "task.create", {"title": title}).value["id"]


class TestLink:
    def test_link_then_unlink(self, board: LocalBoard) -> None:
        a, b = _task(board), _task(board)
        result = _run(board, "task.link", {"task": a, "type": "blocks", "target_task": b})
        assert [e["type"] for e in result.events] == ["relationship_added"]
        assert result.events[0]["data"] == {"type": "blocks", "target_task_id": b}
        assert result.value["relationships_out"][0]["target_task_id"] == b
        assert result.events[0]["origin"]["op"] == "task.link"

        result = _run(board, "task.unlink", {"task": a, "type": "blocks", "target_task": b})
        assert [e["type"] for e in result.events] == ["relationship_removed"]
        assert result.value["relationships_out"] == []

    def test_note(self, board: LocalBoard) -> None:
        a, b = _task(board), _task(board)
        params = {"task": a, "type": "related_to", "target_task": b, "note": "why"}
        assert _run(board, "task.link", params).events[0]["data"]["note"] == "why"

    def test_rejections(self, board: LocalBoard) -> None:
        a, b = _task(board), _task(board)
        params = {"task": a, "type": "bogus", "target_task": b}
        err = _error(board, "task.link", params)
        assert err.code == "VALIDATION_ERROR"
        assert err.message.startswith("Invalid relationship type: 'bogus'. Valid types: ")

        err = _error(board, "task.link", {"task": a, "type": "blocks", "target_task": a})
        assert (err.code, err.message) == (
            "VALIDATION_ERROR",
            "Cannot create a relationship from a task to itself.",
        )
        err = _error(board, "task.link", {"task": a, "type": "blocks", "target_task": "NOPE-9"})
        assert (err.code, err.message) == ("NOT_FOUND", "Short ID 'NOPE-9' not found.")

        missing = "task_01JZZZZZZZZZZZZZZZZZZZZZZZ"
        err = _error(board, "task.link", {"task": a, "type": "blocks", "target_task": missing})
        assert (err.code, err.message) == ("NOT_FOUND", f"Target task {missing} not found.")
        err = _error(board, "task.link", {"task": missing, "type": "blocks", "target_task": a})
        assert (err.code, err.message) == ("NOT_FOUND", f"Task {missing} not found.")

    def test_duplicate_is_conflict_with_snapshot(self, board: LocalBoard) -> None:
        a, b = _task(board), _task(board)
        _run(board, "task.link", {"task": a, "type": "blocks", "target_task": b})
        err = _error(board, "task.link", {"task": a, "type": "blocks", "target_task": b})
        assert (err.code, err.message) == (
            "CONFLICT",
            f"Duplicate: blocks relationship to {b} already exists.",
        )
        assert err.details["snapshot"]["id"] == a
        assert err.details["snapshot"]["last_event_id"]

    def test_unlink_missing(self, board: LocalBoard) -> None:
        a, b = _task(board), _task(board)
        err = _error(board, "task.unlink", {"task": a, "type": "blocks", "target_task": b})
        assert (err.code, err.message) == ("NOT_FOUND", f"No blocks relationship to {b}.")
        err = _error(board, "task.unlink", {"task": a, "type": "nope", "target_task": b})
        assert err.code == "VALIDATION_ERROR"


class TestBranchLink:
    def test_link_then_unlink_with_repo(self, board: LocalBoard) -> None:
        a = _task(board)
        params = {"task": a, "branch": "feat/x", "repo": "origin"}
        result = _run(board, "task.branch_link", params)
        assert result.events[0]["data"] == {"branch": "feat/x", "repo": "origin"}
        assert result.value["branch_links"][0]["branch"] == "feat/x"

        err = _error(board, "task.branch_link", params)
        assert (err.code, err.message) == (
            "CONFLICT",
            f"Duplicate: branch 'feat/x' (repo: origin) already linked to {a}.",
        )
        assert err.details["snapshot"]["id"] == a

        result = _run(board, "task.branch_unlink", params)
        assert [e["type"] for e in result.events] == ["branch_unlinked"]
        err = _error(board, "task.branch_unlink", params)
        assert (err.code, err.message) == (
            "NOT_FOUND",
            f"No branch link 'feat/x' (repo: origin) on {a}.",
        )

    def test_blank_repo_means_none(self, board: LocalBoard) -> None:
        a = _task(board)
        result = _run(board, "task.branch_link", {"task": a, "branch": "b", "repo": "  "})
        assert result.events[0]["data"] == {"branch": "b"}
        # The same branch with no repo is the same link.
        err = _error(board, "task.branch_link", {"task": a, "branch": "b"})
        assert err.message == f"Duplicate: branch 'b' already linked to {a}."

    @pytest.mark.parametrize(
        ("branch", "message"),
        [
            ("  ", "Branch name must not be empty or whitespace-only."),
            ("-x", "Branch name must not start with '-': '-x'."),
            ("a\tb", "Branch name must not contain control characters: ''a\\tb''."),
        ],
    )
    def test_bad_branch_names(self, board: LocalBoard, branch: str, message: str) -> None:
        for op in ("task.branch_link", "task.branch_unlink"):
            err = _error(board, op, {"task": "task_01JZZZZZZZZZZZZZZZZZZZZZZZ", "branch": branch})
            # An input rule: refused before the task is even looked up.
            assert (err.code, err.message) == ("VALIDATION_ERROR", message)


class TestFileLink:
    def test_link_then_unlink(self, board: LocalBoard) -> None:
        a = _task(board)
        result = _run(board, "task.file_link", {"task": a, "filepaths": ["./src/a.py", "b.py"]})
        assert result.events[0]["data"] == {"paths": ["src/a.py", "b.py"]}
        assert result.value["linked_files"] == ["src/a.py", "b.py"]

        # Only new paths are recorded.
        result = _run(board, "task.file_link", {"task": a, "filepaths": ["b.py", "c.py"]})
        assert result.events[0]["data"] == {"paths": ["c.py"]}

        err = _error(board, "task.file_link", {"task": a, "filepaths": ["b.py"]})
        assert (err.code, err.message) == (
            "CONFLICT",
            "All specified files are already linked to this task.",
        )
        assert err.details["snapshot"]["id"] == a

        result = _run(board, "task.file_unlink", {"task": a, "filepaths": ["b.py", "zzz.py"]})
        assert result.events[0]["data"] == {"paths": ["b.py"]}
        err = _error(board, "task.file_unlink", {"task": a, "filepaths": ["b.py"]})
        assert (err.code, err.message) == (
            "NOT_FOUND",
            "None of the specified files are linked to this task.",
        )

    def test_absolute_path_inside_the_project(self, board: LocalBoard) -> None:
        a = _task(board)
        inside = str(board.root / "src" / "x.py")
        result = _run(board, "task.file_link", {"task": a, "filepaths": [inside]})
        assert result.events[0]["data"] == {"paths": ["src/x.py"]}

    @pytest.mark.parametrize(
        ("path", "message"),
        [
            ("", "File path must not be empty."),
            ("a\x01b", "File path contains control characters: 'a\\x01b'."),
            ("../outside.py", "Path '../outside.py' escapes the project root."),
            ("/elsewhere/x.py", "Path '/elsewhere/x.py' is outside the project root."),
        ],
    )
    def test_bad_paths(self, board: LocalBoard, path: str, message: str) -> None:
        a = _task(board)
        for op in ("task.file_link", "task.file_unlink"):
            err = _error(board, op, {"task": a, "filepaths": [path]})
            assert (err.code, err.message) == ("VALIDATION_ERROR", message)

    def test_no_paths(self, board: LocalBoard) -> None:
        for op in ("task.file_link", "task.file_unlink"):
            err = _error(board, op, {"task": _task(board), "filepaths": []})
            assert err.code == "VALIDATION_ERROR"

    def test_archived_task_not_found(self, board: LocalBoard) -> None:
        a = _task(board)
        _archive(board, a)
        err = _error(board, "task.file_link", {"task": a, "filepaths": ["y"]})
        assert (err.code, err.message) == ("NOT_FOUND", f"Task {a} not found.")


def _archive(board: LocalBoard, task_id: str) -> None:
    from click.testing import CliRunner

    from lattice.cli.main import cli

    result = CliRunner().invoke(
        cli,
        ["archive", task_id, "--actor", "agent:t"],
        env={"LATTICE_ROOT": str(board.root)},
    )
    assert result.exit_code == 0, result.output
