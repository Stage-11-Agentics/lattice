"""AC-5 (local part): ``lattice board write`` / ``board.file_write`` (SPEC §3.9, §6.1).

``orchestration/run-state.md`` and a loose review pack under ``plans/`` are
written and read back; a task's ``<task_id>.md``, a runtime path, and ``..``
are refused; the command works on a local board. The hosted half (read from
another client's cache) is H-12's.
"""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path

import pytest

from lattice.boards import LocalBoard, resolve_board
from lattice.cli.prose_cmds import normalize_board_path
from lattice.ops import Caller, OpError
from lattice.ops.board_file_write import check_board_path
from lattice.storage.fs import LATTICE_DIR

RUN_STATE = "# Run state\n\n- wave 1 merged\n"


@pytest.fixture()
def board(initialized_root: Path, monkeypatch: pytest.MonkeyPatch) -> LocalBoard:
    monkeypatch.delenv("LATTICE_ROOT", raising=False)
    return resolve_board(initialized_root)


def _write(board: LocalBoard, path: str, text: str, **extra):  # noqa: ANN003, ANN202
    return board.execute("board.file_write", {"path": path, "file": text, **extra}, Caller())


def _task(board: LocalBoard) -> str:
    return board.execute("task.create", {"title": "T"}, Caller(actor="agent:t")).value["id"]


class TestWritesAndReadsBack:
    def test_orchestration_run_state(self, board: LocalBoard) -> None:
        result = _write(board, "orchestration/run-state.md", RUN_STATE)
        path = board.lattice_dir / "orchestration" / "run-state.md"
        assert path.read_text() == RUN_STATE
        assert result.value == {
            "path": "orchestration/run-state.md",
            "sha256": hashlib.sha256(RUN_STATE.encode()).hexdigest(),
            "bytes": len(RUN_STATE),
        }
        assert result.events == []
        assert result.paths == ("orchestration", "orchestration/run-state.md")

    def test_nested_orchestration_creates_directories(self, board: LocalBoard) -> None:
        _write(board, "orchestration/runs/2026/wave-1/notes.md", "x")
        assert (board.lattice_dir / "orchestration/runs/2026/wave-1/notes.md").read_text() == "x"

    def test_loose_review_pack_under_plans_and_notes(self, board: LocalBoard) -> None:
        _task(board)
        _write(board, "plans/review-pack-LAT-9.md", "pack")
        _write(board, "notes/handoff.md", "handoff")
        assert (board.lattice_dir / "plans" / "review-pack-LAT-9.md").read_text() == "pack"
        assert (board.lattice_dir / "notes" / "handoff.md").read_text() == "handoff"

    def test_same_content_is_idempotent(self, board: LocalBoard) -> None:
        _write(board, "orchestration/a.md", "same")
        assert _write(board, "orchestration/a.md", "same").idempotent

    def test_expect_sha256(self, board: LocalBoard) -> None:
        first = _write(board, "orchestration/a.md", "one").value["sha256"]
        _write(board, "orchestration/a.md", "two", expect_sha256=first.upper())
        with pytest.raises(OpError) as exc:
            _write(board, "orchestration/a.md", "three", expect_sha256=first)
        assert exc.value.code == "CONFLICT"
        assert (board.lattice_dir / "orchestration/a.md").read_text() == "two"

    def test_a_task_named_like_a_task_that_does_not_exist_is_loose(
        self, board: LocalBoard
    ) -> None:
        _write(board, "plans/task_01AAAAAAAAAAAAAAAAAAAAAAAA.md", "loose")


class TestRefusals:
    @pytest.mark.parametrize(
        "path",
        [
            "../outside.md",
            "orchestration/../config.json",
            "orchestration/./x.md",
            "/etc/passwd",
            "locks/x.lock",
            "review_state/x.json",
            ".daemon/x.log",
            "tasks/x.json",
            "config.json",
            "context.md",
            "orchestration",
            "orchestration/",
            "plans/nested/pack.md",
            "archive/plans/pack.md",
            "orchestration/.tmp.x",
            "orchestration/a\\b.md",
            "orchestration/a\x00.md",
            "",
        ],
    )
    def test_outside_the_workspace(self, board: LocalBoard, path: str) -> None:
        with pytest.raises(OpError) as exc:
            _write(board, path, "x")
        assert exc.value.code == "VALIDATION_ERROR"

    @pytest.mark.parametrize("folder", ["plans", "notes"])
    def test_a_tasks_own_file(self, board: LocalBoard, folder: str) -> None:
        task_id = _task(board)
        before = (board.lattice_dir / "plans" / f"{task_id}.md").read_bytes()
        with pytest.raises(OpError) as exc:
            _write(board, f"{folder}/{task_id}.md", "x")
        assert exc.value.code == "VALIDATION_ERROR"
        assert f"lattice {folder.rstrip('s') if folder == 'plans' else folder} write" in (
            exc.value.message
        )
        assert (board.lattice_dir / "plans" / f"{task_id}.md").read_bytes() == before

    def test_an_archived_tasks_own_file(self, board: LocalBoard) -> None:
        task_id = _task(board)
        board.execute("task.archive", {"task": task_id}, Caller(actor="agent:t"))
        with pytest.raises(OpError):
            _write(board, f"plans/{task_id}.md", "x")

    def test_symlink_out_of_the_board(self, board: LocalBoard, tmp_path: Path) -> None:
        outside = tmp_path / "elsewhere"
        outside.mkdir()
        (board.lattice_dir / "orchestration").symlink_to(outside)
        with pytest.raises(OpError) as exc:
            _write(board, "orchestration/x.md", "x")
        assert exc.value.code == "VALIDATION_ERROR"
        assert list(outside.iterdir()) == []

    def test_directory_target_and_file_ancestor(self, board: LocalBoard) -> None:
        _write(board, "orchestration/a/b.md", "x")
        with pytest.raises(OpError):
            _write(board, "orchestration/a", "x")
        with pytest.raises(OpError):
            _write(board, "orchestration/a/b.md/c.md", "x")

    def test_malformed_expect_sha256(self, board: LocalBoard) -> None:
        with pytest.raises(OpError) as exc:
            _write(board, "orchestration/a.md", "x", expect_sha256="abc")
        assert exc.value.code == "VALIDATION_ERROR"


def test_check_board_path_accepts_the_workspace() -> None:
    assert check_board_path("orchestration/a/b.md") == ("orchestration", "a", "b.md")
    assert check_board_path("plans/pack.md") == ("plans", "pack.md")


def test_client_only_tidies_relative_paths() -> None:
    assert normalize_board_path("./orchestration//a.md") == "orchestration/a.md"
    assert normalize_board_path("orchestration/../x") == "orchestration/../x"
    assert normalize_board_path("/etc/passwd") == "/etc/passwd"


@pytest.mark.parametrize("as_json", [False, True])
def test_absolute_path_inside_the_board_is_refused(
    invoke, initialized_root: Path, tmp_path: Path, as_json: bool
) -> None:  # noqa: ANN001
    """PATH is relative to .lattice/ (SPEC §3.9); an absolute one is never resolved."""
    src = tmp_path / "x.md"
    src.write_text("x")
    target = initialized_root / LATTICE_DIR / "orchestration" / "abs.md"
    flag = ["--json"] if as_json else []
    result = invoke("board", "write", str(target), "--file", str(src), *flag)
    assert result.exit_code == 1
    assert "absolute" in result.output
    if as_json:
        assert json.loads(result.output)["error"]["code"] == "VALIDATION_ERROR"
    assert not target.exists()


class TestCli:
    """The command on a local board, plain and --json, and its argument checks."""

    def test_round_trip(self, invoke, initialized_root: Path, tmp_path: Path) -> None:  # noqa: ANN001
        src = tmp_path / "run-state.md"
        src.write_bytes(b"line one\r\nline two\n")
        result = invoke("board", "write", "orchestration/run-state.md", "--file", str(src))
        assert result.exit_code == 0, result.output
        assert "Wrote .lattice/orchestration/run-state.md (19 bytes)" in result.output
        written = initialized_root / LATTICE_DIR / "orchestration" / "run-state.md"
        assert written.read_bytes() == b"line one\r\nline two\n"

        result = invoke("board", "write", "plans/pack.md", "--stdin", "--json", input="pack\n")
        assert result.exit_code == 0, result.output
        assert json.loads(result.output)["data"]["path"] == "plans/pack.md"

    @pytest.mark.parametrize("as_json", [False, True])
    def test_argument_errors(self, invoke, tmp_path: Path, as_json: bool) -> None:  # noqa: ANN001
        src = tmp_path / "x.md"
        src.write_text("x")
        flag = ["--json"] if as_json else []
        cases = [
            (["orchestration/a.md", "--file", str(tmp_path)], "Is a directory"),
            (["orchestration/a.md", "--file", str(tmp_path / "missing")], "Cannot read"),
            (["orchestration/a.md", "--file", str(src), "--stdin"], "not both"),
            (["orchestration/a.md"], "--file PATH or --stdin"),
            (["locks/a", "--file", str(tmp_path)], "outside the workspace"),
            (["orchestration/a.md", "--file", str(src), "--expect-sha256", "zz"], "64 hex"),
        ]
        for args, needle in cases:
            result = invoke("board", "write", *args, *flag)
            assert result.exit_code == 1, (args, result.output)
            if as_json:
                error = json.loads(result.output)["error"]
                assert error["code"] == "VALIDATION_ERROR"
                assert needle in error["message"]
            else:
                assert needle in result.output

    @pytest.mark.skipif(os.geteuid() == 0, reason="root reads unreadable files")
    def test_unreadable_file(self, invoke, tmp_path: Path) -> None:  # noqa: ANN001
        src = tmp_path / "secret.md"
        src.write_text("x")
        src.chmod(0)
        result = invoke("board", "write", "orchestration/a.md", "--file", str(src), "--json")
        assert json.loads(result.output)["error"]["code"] == "VALIDATION_ERROR"

    def test_non_utf8_content(self, invoke, tmp_path: Path) -> None:  # noqa: ANN001
        src = tmp_path / "bin"
        src.write_bytes(b"\xff\xfe")
        result = invoke("board", "write", "orchestration/a.md", "--file", str(src), "--json")
        assert json.loads(result.output)["error"]["message"].endswith("is not UTF-8 text.")

    def test_read_error_is_a_validation_error(
        self, invoke, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:  # noqa: ANN001
        """The unreadable-file path, also where the suite runs as root."""
        src = tmp_path / "secret.md"
        src.write_text("x")

        def denied(self: Path) -> bytes:
            raise PermissionError(13, "Permission denied")

        monkeypatch.setattr(Path, "read_bytes", denied)
        for flag in ([], ["--json"]):
            result = invoke("board", "write", "orchestration/a.md", "--file", str(src), *flag)
            assert result.exit_code == 1
            assert "Cannot read --file" in result.output and "Permission denied" in result.output


class TestSymlinkWithSameContent:
    """A symlink out of the board that already holds the requested bytes must
    be refused, not reported as an idempotent success: the target is resolved
    and confined before it is read."""

    def test_board_write(self, board: LocalBoard, tmp_path: Path) -> None:
        outside = tmp_path / "outside.md"
        outside.write_text(RUN_STATE)
        (board.lattice_dir / "orchestration").mkdir()
        (board.lattice_dir / "orchestration" / "link.md").symlink_to(outside)
        with pytest.raises(OpError) as exc:
            _write(board, "orchestration/link.md", RUN_STATE)
        assert exc.value.code == "VALIDATION_ERROR"
        assert exc.value.details["reason"] == "PATH_OUTSIDE_BOARD"
        assert outside.read_text() == RUN_STATE

    def test_context_write(self, board: LocalBoard, tmp_path: Path) -> None:
        outside = tmp_path / "context.md"
        outside.write_text("ctx")
        context = board.lattice_dir / "context.md"
        context.unlink(missing_ok=True)
        context.symlink_to(outside)
        with pytest.raises(OpError) as exc:
            board.execute("board.context_write", {"stdin": "ctx"}, Caller())
        assert exc.value.code == "VALIDATION_ERROR"

    def test_plan_write(self, board: LocalBoard, tmp_path: Path) -> None:
        task_id = _task(board)
        plan = board.lattice_dir / "plans" / f"{task_id}.md"
        outside = tmp_path / "plan.md"
        outside.write_text("same")
        plan.unlink()
        plan.symlink_to(outside)
        with pytest.raises(OpError) as exc:
            board.execute(
                "task.plan_write", {"task": task_id, "stdin": "same"}, Caller(actor="agent:t")
            )
        assert exc.value.code == "VALIDATION_ERROR"
