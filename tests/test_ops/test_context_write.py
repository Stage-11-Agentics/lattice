"""AC-5 (local part): ``lattice context write`` round trip (SPEC §3.9).

The hosted half (another client reads the new ``context.md`` from its cache)
is H-12's.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from lattice.boards import resolve_board
from lattice.ops import Caller, OpError
from lattice.storage.fs import LATTICE_DIR


def test_round_trip_from_file_and_stdin(invoke, initialized_root: Path, tmp_path: Path) -> None:  # noqa: ANN001
    context = initialized_root / LATTICE_DIR / "context.md"
    src = tmp_path / "context.md"
    src.write_text("# Project\n\nWhy it exists.\n")
    result = invoke("context", "write", "--file", str(src))
    assert result.exit_code == 0, result.output
    assert result.output.strip() == "Wrote .lattice/context.md (26 bytes)"
    assert context.read_text() == "# Project\n\nWhy it exists.\n"

    result = invoke("context", "write", "--stdin", "--json", input="Replaced.\n")
    assert result.exit_code == 0, result.output
    data = json.loads(result.output)["data"]
    assert data["path"] == "context.md" and data["bytes"] == 10
    assert context.read_text() == "Replaced.\n"

    result = invoke("context", "write", "--stdin", input="Replaced.\n")
    assert result.output.strip() == "Unchanged .lattice/context.md"


@pytest.mark.parametrize("as_json", [False, True])
def test_argument_errors_come_before_any_read(invoke, tmp_path: Path, as_json: bool) -> None:  # noqa: ANN001
    flag = ["--json"] if as_json else []
    for args, needle in (
        (["--file", str(tmp_path)], "Is a directory"),
        (["--file", str(tmp_path), "--stdin"], "not both"),
        ([], "--file PATH or --stdin"),
    ):
        result = invoke("context", "write", *args, *flag)
        assert result.exit_code == 1
        assert needle in result.output
        if as_json:
            assert json.loads(result.output)["error"]["code"] == "VALIDATION_ERROR"


def test_operation_needs_no_actor(initialized_root: Path, monkeypatch) -> None:  # noqa: ANN001
    monkeypatch.delenv("LATTICE_ROOT", raising=False)
    board = resolve_board(initialized_root)
    result = board.execute("board.context_write", {"stdin": "ctx"}, Caller())
    assert result.paths == ("context.md",)
    assert (board.lattice_dir / "context.md").read_text() == "ctx"
    with pytest.raises(OpError) as exc:
        board.execute("board.context_write", {"stdin": "a", "file": "b"}, Caller())
    assert exc.value.code == "VALIDATION_ERROR"
