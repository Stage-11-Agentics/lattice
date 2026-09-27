"""Argument errors keep their v1 precedence over "no board": ``comment`` checks its
body before looking for a board; ``create`` and ``status`` look for the board first."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from click.testing import CliRunner

from lattice.cli.main import cli


@pytest.fixture()
def outside(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.chdir(tmp_path)
    (tmp_path / "body.md").write_text("body\n")
    (tmp_path / "bad.bin").write_bytes(b"\xff\xfe\x00bad")
    return tmp_path


@pytest.mark.parametrize(
    ("args", "code", "message"),
    [
        (["comment", "LAT-1"], "VALIDATION_ERROR", "Provide comment text as TEXT or via --file."),
        (
            ["comment", "LAT-1", "x", "--file", "body.md"],
            "VALIDATION_ERROR",
            "Provide either TEXT or --file, not both.",
        ),
        (
            ["comment", "LAT-1", "x", "--file", "bad.bin"],
            "VALIDATION_ERROR",
            "Provide either TEXT or --file, not both.",
        ),
        (["comment", "LAT-1", "hi", "--actor", "a:b"], "NOT_INITIALIZED", None),
        (["comment", "LAT-1", "hi"], "NOT_INITIALIZED", None),
        (["create", "T"], "NOT_INITIALIZED", None),
        (["create", "T", "--status", "zzz", "--actor", "a:b"], "NOT_INITIALIZED", None),
        (["create", "T", "--actor", "bad"], "NOT_INITIALIZED", None),
        (["status", "LAT-1", "zzz"], "NOT_INITIALIZED", None),
        (["status", "junk!", "done", "--actor", "a:b"], "NOT_INITIALIZED", None),
    ],
)
def test_error_precedence_outside_any_board(
    outside: Path, args: list[str], code: str, message: str | None
) -> None:
    result = CliRunner().invoke(cli, [*args, "--json"], env={"LATTICE_ROOT": None})
    assert result.exit_code == 1
    error = json.loads(result.output)["error"]
    assert error["code"] == code
    if message is not None:
        assert error["message"] == message
    plain = CliRunner().invoke(cli, args, env={"LATTICE_ROOT": None})
    assert plain.exit_code == 1
    assert plain.stderr.startswith("Error: ")
