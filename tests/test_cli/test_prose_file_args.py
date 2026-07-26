"""Prose bodies read from a file, on every command that takes one (LAT-263).

The incident these tests exist for: an agent wrote a long body inline into a
double-quoted shell argument and the backticks in the text ran as command
substitution — once silently eating a clause, once running the whole pytest
suite and splicing ~15 KB of output into the comment. A file is never handed
to the shell, so the body must arrive byte for byte.

Covered commands: ``comment --file``, ``comment-edit --file``,
``complete --review-file``, ``needs-human --file``. All four share one
resolution helper (``lattice.cli.helpers.resolve_body``), so these tests
assert one behaviour four times on purpose.
"""

from __future__ import annotations

import json

import pytest

_ACTOR = "human:test"

# Every shell metacharacter that mangles a body when it goes through a
# double-quoted argument: backticks, $(...) and ${...}. Deliberately no
# leading/trailing whitespace — comment bodies are stripped on the way in,
# so the interior is what must survive untouched.
SHELL_HOSTILE_BODY = """Root cause: `scripts/ci.sh` shells out before quoting.

The old inline path ran `pytest -q` and $(git rev-parse HEAD) instead of
recording them, and ${HOME} expanded to the operator's home directory.
Backtick pair two: `make test` — still text, not a command.

Cost: $(1 + 1) engineer-days. Literally that string, never evaluated."""


# ---------------------------------------------------------------------------
# Setup helpers
# ---------------------------------------------------------------------------


def _new_task(invoke, title: str = "Prose task") -> str:
    r = invoke("create", title, "--actor", _ACTOR, "--json")
    assert r.exit_code == 0, r.output
    return json.loads(r.output)["data"]["id"]


def _task_in_progress(invoke, fill_plan) -> str:
    task_id = _new_task(invoke)
    invoke("status", task_id, "in_planning", "--actor", _ACTOR)
    fill_plan(task_id, "Prose task")
    invoke("status", task_id, "planned", "--actor", _ACTOR)
    invoke("status", task_id, "in_progress", "--actor", _ACTOR)
    return task_id


def _add_comment(invoke, task_id: str, body: str) -> str:
    r = invoke("comment", task_id, body, "--actor", _ACTOR, "--json")
    assert r.exit_code == 0, r.output
    return json.loads(r.output)["data"]["last_event_id"]


def _comment_bodies(invoke, task_id: str) -> list[str]:
    r = invoke("comments", task_id, "--json")
    assert r.exit_code == 0, r.output
    return [c["body"] for c in json.loads(r.output)["data"]]


def _write(tmp_path, body: str, name: str = "body.md"):
    path = tmp_path / name
    path.write_text(body, encoding="utf-8")
    return str(path)


# ---------------------------------------------------------------------------
# The body arrives intact — one test per command
# ---------------------------------------------------------------------------


class TestBodyFromFile:
    def test_comment(self, invoke, initialized_root, tmp_path) -> None:
        task_id = _new_task(invoke)
        r = invoke(
            "comment",
            task_id,
            "--file",
            _write(tmp_path, "Findings, at length."),
            "--actor",
            _ACTOR,
        )
        assert r.exit_code == 0, r.output
        assert _comment_bodies(invoke, task_id) == ["Findings, at length."]

    def test_comment_edit(self, invoke, initialized_root, tmp_path) -> None:
        task_id = _new_task(invoke)
        comment_id = _add_comment(invoke, task_id, "original")
        r = invoke(
            "comment-edit",
            task_id,
            comment_id,
            "--file",
            _write(tmp_path, "Revised, at length."),
            "--actor",
            _ACTOR,
        )
        assert r.exit_code == 0, r.output
        assert _comment_bodies(invoke, task_id) == ["Revised, at length."]

    def test_complete_review_file(self, invoke, initialized_root, fill_plan, tmp_path) -> None:
        task_id = _task_in_progress(invoke, fill_plan)
        r = invoke(
            "complete",
            task_id,
            "--review-file",
            _write(tmp_path, "Reviewed, at length."),
            "--actor",
            _ACTOR,
            "--json",
        )
        assert r.exit_code == 0, r.output
        assert json.loads(r.output)["data"]["status"] == "done"
        assert _comment_bodies(invoke, task_id) == ["Reviewed, at length."]

    def test_needs_human(self, invoke, initialized_root, tmp_path) -> None:
        task_id = _new_task(invoke)
        r = invoke(
            "needs-human",
            task_id,
            "--file",
            _write(tmp_path, "Need: which OAuth provider?"),
            "--actor",
            _ACTOR,
            "--json",
        )
        assert r.exit_code == 0, r.output
        flag = json.loads(r.output)["data"]["needs_human"]
        assert flag["reason"] == "Need: which OAuth provider?"


# ---------------------------------------------------------------------------
# THE REGRESSION THIS TICKET EXISTS FOR
# ---------------------------------------------------------------------------


class TestShellHostileBodyRoundTrip:
    """A body full of backticks and $(...) must survive --file unchanged.

    This is the whole point of LAT-263. Inline, these characters are command
    substitution and the shell eats or replaces them before Lattice ever sees
    the text; via --file the bytes go straight from disk into the event.
    """

    def test_comment_file_body_with_backticks_round_trips_byte_identically(
        self, invoke, initialized_root, tmp_path
    ) -> None:
        task_id = _new_task(invoke)
        r = invoke(
            "comment",
            task_id,
            "--file",
            _write(tmp_path, SHELL_HOSTILE_BODY),
            "--actor",
            _ACTOR,
        )
        assert r.exit_code == 0, r.output

        stored = _comment_bodies(invoke, task_id)[0]
        assert stored == SHELL_HOSTILE_BODY
        # Named explicitly so a future regression names itself:
        assert "`pytest -q`" in stored
        assert "$(git rev-parse HEAD)" in stored
        assert "${HOME}" in stored
        assert "$(1 + 1)" in stored

    def test_complete_review_file_with_backticks_round_trips_byte_identically(
        self, invoke, initialized_root, fill_plan, tmp_path
    ) -> None:
        task_id = _task_in_progress(invoke, fill_plan)
        r = invoke(
            "complete",
            task_id,
            "--review-file",
            _write(tmp_path, SHELL_HOSTILE_BODY),
            "--actor",
            _ACTOR,
        )
        assert r.exit_code == 0, r.output
        assert _comment_bodies(invoke, task_id)[0] == SHELL_HOSTILE_BODY


# ---------------------------------------------------------------------------
# Exactly one of inline / file — both or neither is a VALIDATION_ERROR
# ---------------------------------------------------------------------------

COMMANDS = ["comment", "comment-edit", "complete", "needs-human"]


def _case(name: str, invoke, fill_plan) -> tuple[list[str], list[str], str]:
    """Return (base_args, inline_args, file_flag) for a prose-taking command."""
    if name == "comment":
        return ["comment", _new_task(invoke)], ["inline text"], "--file"
    if name == "comment-edit":
        task_id = _new_task(invoke)
        return (
            ["comment-edit", task_id, _add_comment(invoke, task_id, "original")],
            ["inline text"],
            "--file",
        )
    if name == "complete":
        return (
            ["complete", _task_in_progress(invoke, fill_plan)],
            ["--review", "inline"],
            "--review-file",
        )
    if name == "needs-human":
        return ["needs-human", _new_task(invoke)], ["inline reason"], "--file"
    raise AssertionError(f"unknown command: {name}")


def _assert_validation_error_envelope(result) -> str:
    """Assert the --json error envelope shape and return the message."""
    assert result.exit_code != 0
    parsed = json.loads(result.output)
    assert set(parsed) == {"ok", "error"}
    assert parsed["ok"] is False
    assert set(parsed["error"]) == {"code", "message"}
    assert parsed["error"]["code"] == "VALIDATION_ERROR"
    assert isinstance(parsed["error"]["message"], str) and parsed["error"]["message"]
    return parsed["error"]["message"]


class TestExactlyOneSource:
    @pytest.mark.parametrize("name", COMMANDS)
    def test_both_inline_and_file_rejected(
        self, name, invoke, initialized_root, fill_plan, tmp_path
    ) -> None:
        base, inline, file_flag = _case(name, invoke, fill_plan)
        result = invoke(
            *base, *inline, file_flag, _write(tmp_path, "from file"), "--actor", _ACTOR, "--json"
        )
        message = _assert_validation_error_envelope(result)
        assert "not both" in message

    @pytest.mark.parametrize("name", COMMANDS)
    def test_neither_inline_nor_file_rejected(
        self, name, invoke, initialized_root, fill_plan
    ) -> None:
        base, _inline, _file_flag = _case(name, invoke, fill_plan)
        result = invoke(*base, "--actor", _ACTOR, "--json")
        _assert_validation_error_envelope(result)

    @pytest.mark.parametrize("name", COMMANDS)
    def test_missing_file_is_rejected_by_click(
        self, name, invoke, initialized_root, fill_plan, tmp_path
    ) -> None:
        """click.Path(exists=True) — a typo'd path never becomes an empty body."""
        base, _inline, file_flag = _case(name, invoke, fill_plan)
        result = invoke(*base, file_flag, str(tmp_path / "nope.md"), "--actor", _ACTOR)
        assert result.exit_code != 0
        assert "does not exist" in result.output
