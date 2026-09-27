"""``needs-human --file``: the argument checks run in today's order, and the file
is read only when its text becomes the reason (LAT-298 review round 1)."""

from __future__ import annotations

import json
import pathlib
from pathlib import Path

import pytest

_ACTOR = "agent:test"
_BOTH = "Provide either REASON or --file, not both."
_CLEAR = (
    "REASON / --file is only for setting the flag. To clear, use --clear (optionally with --note)."
)
_NOTE = "--note is only for clearing the flag. To set, pass a REASON."


def _create(invoke) -> str:  # noqa: ANN001
    r = invoke("create", "Flag me", "--actor", _ACTOR, "--json")
    return json.loads(r.output)["data"]["id"]


@pytest.fixture(params=["directory", "undecodable", "permission"])
def bad_file(request, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:  # noqa: ANN001
    """A --file path that exists but cannot be read as text."""
    if request.param == "directory":
        path = tmp_path / "a_dir"
        path.mkdir()
    elif request.param == "undecodable":
        path = tmp_path / "bad.md"
        path.write_bytes(b"\xff\xfe\xfa not utf-8")
    else:
        # Root can read a mode-000 file, so refuse the read itself.
        path = tmp_path / "locked.md"
        path.write_text("secret")
        real = pathlib.Path.read_text

        def read_text(self, *a, **kw):  # noqa: ANN001, ANN002, ANN003, ANN202
            if self == path:
                raise PermissionError(13, "Permission denied", str(path))
            return real(self, *a, **kw)

        monkeypatch.setattr(pathlib.Path, "read_text", read_text)
    return path


def _assert_error(result, code: str, message: str, as_json: bool) -> None:  # noqa: ANN001
    assert result.exit_code == 1, result.output
    if as_json:
        assert json.loads(result.stdout) == {
            "ok": False,
            "error": {"code": code, "message": message},
        }
    else:
        assert result.stdout == ""
        assert result.stderr == f"Error: {message}\n"


@pytest.mark.parametrize("as_json", [False, True], ids=["plain", "json"])
class TestFileNeverReadWhenRejected:
    def test_reason_and_file(self, invoke, bad_file: Path, as_json: bool) -> None:  # noqa: ANN001
        task_id = _create(invoke)
        args = ["needs-human", task_id, "inline", "--file", str(bad_file), "--actor", _ACTOR]
        _assert_error(
            invoke(*args, *(["--json"] if as_json else [])), "VALIDATION_ERROR", _BOTH, as_json
        )

    def test_clear_with_file(self, invoke, bad_file: Path, as_json: bool) -> None:  # noqa: ANN001
        task_id = _create(invoke)
        args = ["needs-human", task_id, "--clear", "--file", str(bad_file), "--actor", _ACTOR]
        _assert_error(
            invoke(*args, *(["--json"] if as_json else [])), "VALIDATION_ERROR", _CLEAR, as_json
        )

    def test_note_when_setting(self, invoke, bad_file: Path, as_json: bool) -> None:  # noqa: ANN001
        task_id = _create(invoke)
        args = ["needs-human", task_id, "--note", "n", "--file", str(bad_file), "--actor", _ACTOR]
        _assert_error(
            invoke(*args, *(["--json"] if as_json else [])), "VALIDATION_ERROR", _NOTE, as_json
        )

    def test_unknown_task_before_file(self, invoke, bad_file: Path, as_json: bool) -> None:  # noqa: ANN001
        args = ["needs-human", "NOPE-1", "--file", str(bad_file), "--actor", _ACTOR]
        result = invoke(*args, *(["--json"] if as_json else []))
        _assert_error(result, "NOT_FOUND", "Short ID 'NOPE-1' not found.", as_json)

    def test_bad_on_behalf_of_before_file(self, invoke, bad_file: Path, as_json: bool) -> None:  # noqa: ANN001
        task_id = _create(invoke)
        args = ["needs-human", task_id, "--file", str(bad_file), "--actor", _ACTOR]
        result = invoke(*args, "--on-behalf-of", "nocolon", *(["--json"] if as_json else []))
        message = (
            "Invalid actor format: 'nocolon'. "
            "Expected prefix:identifier (e.g., human:atin, agent:claude)."
        )
        _assert_error(result, "INVALID_ACTOR", message, as_json)


def test_file_alone_still_surfaces_the_read_error(invoke, bad_file: Path) -> None:  # noqa: ANN001
    """As before, an unreadable reason file on a valid call is the read error, and
    nothing is written."""
    task_id = _create(invoke)
    result = invoke("needs-human", task_id, "--file", str(bad_file), "--actor", _ACTOR, "--json")
    assert result.exit_code == 1
    assert isinstance(result.exception, (OSError, UnicodeDecodeError))
    shown = invoke("show", task_id, "--json")
    assert json.loads(shown.output)["data"].get("needs_human") is None
