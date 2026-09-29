"""``task.attach`` (SPEC §3.8): the payload travels in params.

``payload: {filename, content_b64, sha256}``; the hash is verified; the file
is stored at ``artifacts/payload/<artifact_id><suffix>``; ``filename`` is only
metadata (title, content type, suffix) and is never used as a path.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from lattice.boards import LocalBoard, resolve_board
from lattice.ops import Caller, OpError
from lattice.ops.task_attach import encode_payload


@pytest.fixture()
def board(initialized_root: Path, monkeypatch: pytest.MonkeyPatch) -> LocalBoard:
    monkeypatch.delenv("LATTICE_ROOT", raising=False)
    return resolve_board(initialized_root)


def _run(board: LocalBoard, params: dict):  # noqa: ANN202
    return board.execute("task.attach", params, Caller(actor="agent:t"))


def _task(board: LocalBoard) -> str:
    return board.execute("task.create", {"title": "T"}, Caller(actor="agent:t")).value["id"]


def _payloads(board: LocalBoard) -> list[str]:
    return sorted(p.name for p in (board.lattice_dir / "artifacts" / "payload").iterdir())


def test_payload_is_stored_under_the_artifact_id(board: LocalBoard) -> None:
    task_id = _task(board)
    result = _run(
        board,
        {
            "task": task_id,
            "payload": encode_payload("trace.jsonl", b'{"a": 1}\n'),
            "role": "review",
        },
    )
    meta = result.value
    assert meta["title"] == "trace.jsonl"
    assert meta["payload"] == {
        "file": f"{meta['id']}.jsonl",
        "content_type": None,
        "size_bytes": 9,
    }
    stored = board.lattice_dir / "artifacts" / "payload" / f"{meta['id']}.jsonl"
    assert stored.read_bytes() == b'{"a": 1}\n'
    assert [e["type"] for e in result.events] == ["artifact_attached"]


@pytest.mark.parametrize(
    ("filename", "content_type"),
    # mimetypes.guess_type parsed these as URLs (null, then text/markdown); the
    # name is read literally now (SPEC §3.8, G-6; LAT-356).
    [("data:report.md", "text/markdown"), ("release:report.md?1", None)],
)
def test_the_content_type_reads_the_filename_literally(
    board: LocalBoard, filename: str, content_type: str | None
) -> None:
    task_id = _task(board)
    meta = _run(board, {"task": task_id, "payload": encode_payload(filename, b"x")}).value
    assert meta["payload"]["content_type"] == content_type


@pytest.mark.parametrize(
    "filename", ["../../escape.md", "/etc/escape.md", "a/b/escape.md", "..\\..\\escape.md"]
)
def test_filename_is_never_a_path(board: LocalBoard, filename: str, tmp_path: Path) -> None:
    task_id = _task(board)
    meta = _run(board, {"task": task_id, "payload": encode_payload(filename, b"x")}).value
    assert _payloads(board) == [f"{meta['id']}.md"]
    assert meta["payload"]["content_type"] == "text/markdown"
    assert not list(tmp_path.rglob("escape.md"))


@pytest.mark.parametrize(
    ("mutate", "needle"),
    [
        (lambda p: {**p, "sha256": "0" * 64}, "sha256 does not match"),
        (lambda p: {**p, "content_b64": "***"}, "not valid base64"),
        (lambda p: {k: v for k, v in p.items() if k != "sha256"}, "exactly"),
        (lambda p: {**p, "extra": "x"}, "exactly"),
        (lambda p: {**p, "filename": "a.m\x00d"}, "Invalid payload filename"),
        (lambda p: {**p, "filename": ""}, "Invalid payload filename"),
    ],
)
def test_bad_payloads_are_refused_before_anything_is_written(
    board: LocalBoard, mutate, needle: str
) -> None:  # noqa: ANN001
    task_id = _task(board)
    with pytest.raises(OpError) as exc:
        _run(board, {"task": task_id, "payload": mutate(encode_payload("f.md", b"x"))})
    assert exc.value.code == "VALIDATION_ERROR"
    assert needle in exc.value.message
    assert _payloads(board) == []


def test_bare_non_url_source_is_not_found(board: LocalBoard) -> None:
    task_id = _task(board)
    with pytest.raises(OpError) as exc:
        _run(board, {"task": task_id, "source": "missing.md"})
    assert (exc.value.code, exc.value.message) == (
        "NOT_FOUND",
        "Source file not found: 'missing.md'.",
    )


def test_source_and_payload_together(board: LocalBoard) -> None:
    task_id = _task(board)
    with pytest.raises(OpError) as exc:
        _run(
            board,
            {"task": task_id, "source": "https://x", "payload": encode_payload("f", b"x")},
        )
    assert exc.value.code == "VALIDATION_ERROR"


def test_missing_task_is_not_found(board: LocalBoard) -> None:
    missing = "task_01AAAAAAAAAAAAAAAAAAAAAAAA"
    with pytest.raises(OpError) as exc:
        _run(board, {"task": missing, "inline": "x"})
    assert (exc.value.code, exc.value.message) == ("NOT_FOUND", f"Task {missing} not found.")


class TestCliSource:
    """The client turns a readable file into a payload; anything else stays a SOURCE."""

    @pytest.fixture()
    def unreadable(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
        """A file whose read fails (patched, so it holds when the suite runs as root)."""
        src = tmp_path / "secret.md"
        src.write_text("x")
        real = Path.read_bytes

        def read_bytes(self: Path) -> bytes:
            if self == src:
                raise PermissionError(13, "Permission denied")
            return real(self)

        monkeypatch.setattr(Path, "read_bytes", read_bytes)
        return src

    def test_directory_source_is_not_found_as_before(
        self, invoke, create_task, tmp_path: Path
    ) -> None:  # noqa: ANN001
        task = create_task("T")
        result = invoke("attach", task["id"], str(tmp_path), "--actor", "agent:t", "--json")
        assert json.loads(result.output)["error"] == {
            "code": "NOT_FOUND",
            "message": f"Source file not found: '{tmp_path}'.",
        }

    def test_read_error_is_a_validation_error(self, invoke, create_task, unreadable: Path) -> None:  # noqa: ANN001
        task = create_task("T")
        for flag in ([], ["--json"]):
            result = invoke("attach", task["id"], str(unreadable), "--actor", "agent:t", *flag)
            assert result.exit_code == 1
            assert "Cannot read source file" in result.output

    @pytest.mark.parametrize("as_json", [False, True])
    @pytest.mark.parametrize(
        ("extra", "task_arg", "code", "needle"),
        [
            (["--role", "bogus"], None, "INVALID_ROLE", "Unknown role: 'bogus'"),
            ([], "NOPE-9", "NOT_FOUND", "Short ID 'NOPE-9' not found."),
            ([], "task_01AAAAAAAAAAAAAAAAAAAAAAAA", "NOT_FOUND", "not found."),
            (["--criterion", "nope"], None, "VALIDATION_ERROR", "nope"),
            (["--type", "bogus"], None, "VALIDATION_ERROR", "Invalid artifact type: 'bogus'"),
            (["--id", "nonsense"], None, "INVALID_ID", "Invalid artifact ID format"),
        ],
    )
    def test_validations_win_over_an_unreadable_file(
        self,
        invoke,
        create_task,
        unreadable: Path,
        extra: list[str],
        task_arg: str | None,
        code: str,
        needle: str,
        as_json: bool,
    ) -> None:  # noqa: ANN001
        """Today's order: every rule that ran before the file was read still wins."""
        task = create_task("T")
        flag = ["--json"] if as_json else []
        result = invoke(
            "attach", task_arg or task["id"], str(unreadable), *extra, "--actor", "agent:t", *flag
        )
        assert result.exit_code == 1
        assert needle in result.output
        assert "Cannot read" not in result.output
        if as_json:
            assert json.loads(result.output)["error"]["code"] == code

    def test_the_probe_writes_nothing(
        self, invoke, create_task, unreadable: Path, initialized_root: Path
    ) -> None:  # noqa: ANN001
        task = create_task("T")
        lattice_dir = initialized_root / ".lattice"
        before = sorted(
            (str(p), p.read_text()) for p in lattice_dir.rglob("*.json*") if p.is_file()
        )
        invoke("attach", task["id"], str(unreadable), "--actor", "agent:t")
        after = sorted(
            (str(p), p.read_text()) for p in lattice_dir.rglob("*.json*") if p.is_file()
        )
        assert after == before
        assert list((lattice_dir / "artifacts" / "payload").iterdir()) == []

    def test_argument_errors_win_over_the_source(
        self, invoke, create_task, tmp_path: Path
    ) -> None:  # noqa: ANN001
        task = create_task("T")
        result = invoke(
            "attach", task["id"], str(tmp_path), "--inline", "x", "--actor", "agent:t", "--json"
        )
        assert json.loads(result.output)["error"]["message"] == (
            "Provide either SOURCE or --inline, not both."
        )
