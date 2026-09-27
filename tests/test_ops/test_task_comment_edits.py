"""``task.comment_edit``, ``task.comment_delete``, ``task.react``,
``task.unreact`` called directly, and the CLI's argument-before-board order."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from click.testing import CliRunner

from lattice.boards import LocalBoard, resolve_board
from lattice.cli.main import cli
from lattice.ops import Caller, OpError


@pytest.fixture()
def board(initialized_root_with_policies: Path, monkeypatch: pytest.MonkeyPatch) -> LocalBoard:
    monkeypatch.delenv("LATTICE_ROOT", raising=False)
    return resolve_board(initialized_root_with_policies)


@pytest.fixture()
def task(board: LocalBoard) -> str:
    return _run(board, "task.create", {"title": "t"}).value["id"]


def _run(board: LocalBoard, op: str, params: dict, actor: str = "agent:t"):  # noqa: ANN202
    return board.execute(op, params, Caller(actor=actor))


def _error(board: LocalBoard, op: str, params: dict, actor: str = "agent:t") -> OpError:
    with pytest.raises(OpError) as exc:
        _run(board, op, params, actor)
    return exc.value


def _comment(board: LocalBoard, task: str, text: str = "First", **extra: str) -> str:
    return _run(board, "task.comment", {"task": task, "text": text, **extra}).events[0]["id"]


class TestCommentEdit:
    def test_edit_body_and_role(self, board: LocalBoard, task: str) -> None:
        cid = _comment(board, task)
        result = _run(
            board, "task.comment_edit", {"task": task, "comment_id": cid, "new_text": "Second"}
        )
        assert result.events[0]["type"] == "comment_edited"
        assert result.events[0]["data"] == {
            "comment_id": cid,
            "body": "Second",
            "previous_body": "First",
        }
        assert not result.idempotent

        result = _run(
            board,
            "task.comment_edit",
            {"task": task, "comment_id": cid, "file": "Second", "role": "review"},
        )
        assert result.events[0]["data"]["role"] == "review"
        assert result.events[0]["data"]["previous_role"] is None

        result = _run(
            board,
            "task.comment_edit",
            {"task": task, "comment_id": cid, "new_text": "Second", "clear_role": True},
        )
        assert result.events[0]["data"]["role"] is None
        assert result.events[0]["data"]["previous_role"] == "review"

    def test_unchanged_is_idempotent(self, board: LocalBoard, task: str) -> None:
        cid = _comment(board, task)
        result = _run(
            board, "task.comment_edit", {"task": task, "comment_id": cid, "new_text": "First"}
        )
        assert result.idempotent and result.events == []
        assert result.value["id"] == task

    @pytest.mark.parametrize(
        ("params", "message"),
        [
            (
                {"new_text": "x", "role": "review", "clear_role": True},
                "--role and --clear-role are mutually exclusive.",
            ),
            ({"new_text": "x", "file": "y"}, "Provide either NEW_TEXT or --file, not both."),
            ({}, "Provide the new comment text as NEW_TEXT or via --file."),
            # Mutual exclusion is checked first, as the command always did.
            (
                {"role": "review", "clear_role": True},
                "--role and --clear-role are mutually exclusive.",
            ),
        ],
    )
    def test_input_rejections(self, board: LocalBoard, params: dict, message: str) -> None:
        err = _error(
            board, "task.comment_edit", {"task": "NOPE-1", "comment_id": "ev_x", **params}
        )
        assert (err.code, err.message) == ("VALIDATION_ERROR", message)

    def test_rule_rejections(self, board: LocalBoard, task: str) -> None:
        cid = _comment(board, task)
        err = _error(
            board, "task.comment_edit", {"task": task, "comment_id": cid, "new_text": "  "}
        )
        assert err.code == "VALIDATION_ERROR"
        err = _error(
            board,
            "task.comment_edit",
            {"task": task, "comment_id": cid, "new_text": "x", "role": "bogus"},
        )
        assert err.code == "INVALID_ROLE"
        assert err.message.startswith("Unknown role: 'bogus'. Valid roles: ")
        err = _error(
            board, "task.comment_edit", {"task": task, "comment_id": "ev_missing", "new_text": "x"}
        )
        assert err.code == "VALIDATION_ERROR"

    def test_missing_task_keeps_validation_error(self, board: LocalBoard) -> None:
        # Today's mapping: the write's placement error is a VALIDATION_ERROR.
        missing = "task_01JZZZZZZZZZZZZZZZZZZZZZZZ"
        for op, extra in (
            ("task.comment_edit", {"new_text": "x"}),
            ("task.comment_delete", {}),
            ("task.react", {"emoji": "rocket"}),
        ):
            err = _error(board, op, {"task": missing, "comment_id": "ev_x", **extra})
            assert err.code == "VALIDATION_ERROR", op
        err = _error(board, "task.unreact", {"task": missing, "comment_id": "ev_x", "emoji": "r"})
        assert err.code == "NOT_FOUND"


class TestCommentDelete:
    def test_delete_then_refuse(self, board: LocalBoard, task: str) -> None:
        cid = _comment(board, task)
        result = _run(board, "task.comment_delete", {"task": task, "comment_id": cid})
        assert result.events[0]["type"] == "comment_deleted"
        assert result.events[0]["data"] == {"comment_id": cid}

        for op, extra in (
            ("task.comment_delete", {}),
            ("task.comment_edit", {"new_text": "Revive"}),
            ("task.react", {"emoji": "rocket"}),
        ):
            err = _error(board, op, {"task": task, "comment_id": cid, **extra})
            assert err.code == "VALIDATION_ERROR", op


class TestReactions:
    def test_react_idempotent_per_actor(self, board: LocalBoard, task: str) -> None:
        cid = _comment(board, task)
        params = {"task": task, "comment_id": cid, "emoji": "thumbsup"}
        result = _run(board, "task.react", params)
        assert result.events[0]["type"] == "reaction_added"
        assert result.events[0]["data"] == {"comment_id": cid, "emoji": "thumbsup"}

        again = _run(board, "task.react", params)
        assert again.idempotent and again.events == []

        other = _run(board, "task.react", params, actor="agent:other")
        assert not other.idempotent

        result = _run(board, "task.unreact", params)
        assert result.events[0]["type"] == "reaction_removed"
        err = _error(board, "task.unreact", params)
        assert (err.code, err.message) == (
            "NOT_FOUND",
            f"Reaction :thumbsup: by agent:t not found on comment {cid}.",
        )

    def test_react_on_a_reply(self, board: LocalBoard, task: str) -> None:
        parent = _comment(board, task)
        reply = _comment(board, task, "Reply", reply_to=parent)
        params = {"task": task, "comment_id": reply, "emoji": "rocket"}
        _run(board, "task.react", params)
        assert _run(board, "task.react", params).idempotent

    def test_bad_emoji_and_missing_comment(self, board: LocalBoard, task: str) -> None:
        cid = _comment(board, task)
        for op in ("task.react", "task.unreact"):
            err = _error(board, op, {"task": task, "comment_id": cid, "emoji": "no spaces"})
            assert (err.code, err.message) == (
                "VALIDATION_ERROR",
                "Invalid emoji: 'no spaces'. Must be 1-50 alphanumeric, underscore, or "
                "hyphen characters.",
            )
        err = _error(board, "task.react", {"task": task, "comment_id": "ev_x", "emoji": "r"})
        assert err.code == "VALIDATION_ERROR"
        err = _error(board, "task.unreact", {"task": task, "comment_id": "ev_x", "emoji": "r"})
        assert err.code == "NOT_FOUND"


class TestArgumentsBeforeBoard:
    """Commands that checked arguments before finding the board still do."""

    @pytest.mark.parametrize(
        ("args", "message"),
        [
            (
                ["branch-link", "X-1", "a\tb"],
                "Branch name must not contain control characters: ''a\\tb''.",
            ),
            (["branch-unlink", "X-1", " "], "Branch name must not be empty or whitespace-only."),
            (
                ["criterion", "add", "X-1"],
                "Provide acceptance-criterion outcome as OUTCOME or via --file.",
            ),
            (
                ["criterion", "retire", "X-1", "!"],
                "Criterion ID must match ^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$.",
            ),
            (
                ["comment-edit", "X-1", "ev_x", "t", "--role", "r", "--clear-role"],
                "--role and --clear-role are mutually exclusive.",
            ),
        ],
    )
    def test_argument_error_wins_over_no_board(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, args: list[str], message: str
    ) -> None:
        monkeypatch.chdir(tmp_path)
        monkeypatch.delenv("LATTICE_ROOT", raising=False)
        result = CliRunner().invoke(cli, [*args, "--actor", "agent:t", "--json"])
        assert result.exit_code == 1
        assert json.loads(result.output)["error"] == {
            "code": "VALIDATION_ERROR",
            "message": message,
        }
