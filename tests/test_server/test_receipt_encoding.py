"""Receipts and undo logs are written with ASCII escapes (a lone surrogate in event
data has no UTF-8 encoding). Files a pre-change server wrote in raw UTF-8 still
read, replay, and roll back; a replayed surrogate operation returns its stored
result (H-12 review round 2, item 3)."""

from __future__ import annotations

import dataclasses
import json
from dataclasses import dataclass
from pathlib import Path

from lattice.ops.base import CommonParams, OpContext, OpResult, operation
from lattice.server.testing import ServerHandle
from lattice.server.transactions import read_receipt, read_undo_log
from lattice.storage.fs import atomic_write, jsonl_append
from tests.test_server.conftest import board_hash, create_task

UNICODE = "héllo ✓ 漢字"


def _legacy(line: bytes) -> bytes:
    """*line* as the pre-change server wrote it: raw UTF-8, no ASCII escapes."""
    value = json.loads(line)
    return (json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)).encode()


def _op(server: ServerHandle, token: str, op: str, params: dict, op_id: str) -> tuple[int, dict]:
    status, _, body = server.op("alpha", op, params, token=token, op_id=op_id, actor="agent:dev")
    return status, body


def test_a_legacy_utf8_receipt_reads_and_replays(server: ServerHandle, token: str) -> None:
    task = create_task(server, token)
    op_id = "op_01K5ZZZZZZZZZZZZZZZZZZZZZZ"
    params = {"task": task["id"], "text": UNICODE}
    status, first = _op(server, token, "task.comment", params, op_id)
    assert status == 200, first

    project = server.project("alpha")
    board = project.board
    # Rewrite every receipt line of that file in the old encoding, and point the
    # in-memory index at the rewritten lines (what H-22's load-time rebuild reads).
    key = next(k for k in project.index if k[1] == op_id)
    entry = project.index[key]
    path = board / "hosted" / "receipts" / entry.receipt
    new_lines: list[bytes] = []
    offsets: dict[int, tuple[int, int]] = {}
    position = 0
    for raw in path.read_bytes().splitlines():
        legacy = _legacy(raw)
        assert (UNICODE.encode() in legacy) == (json.loads(raw)["op_id"] == op_id)
        offsets[json.loads(raw)["seq"]] = (position, len(legacy))
        new_lines.append(legacy + b"\n")
        position += len(legacy) + 1
    path.write_bytes(b"".join(new_lines))
    for k, e in list(project.index.items()):
        if e.receipt == entry.receipt and e.seq in offsets:
            offset, length = offsets[e.seq]
            project.index[k] = dataclasses.replace(e, offset=offset, length=length)

    receipt = read_receipt(board, project.index[key])
    assert receipt["result"] == first["data"]["result"]
    assert UNICODE.encode() in path.read_bytes()  # the file really is raw UTF-8 now

    status, again = _op(server, token, "task.comment", params, op_id)
    assert status == 200, again
    assert again["data"]["result"].pop("replayed") is True
    expected = {k: v for k, v in first["data"]["result"].items() if k != "replayed"}
    assert again["data"]["result"] == expected


@dataclass(frozen=True, kw_only=True)
class LegacyUndoParams(CommonParams):
    task: str


@operation("xtest.legacy_undo_then_fail")
class LegacyUndoThenFail:
    """Writes a file with a non-ASCII name and appends to a log, rewrites its own
    undo log the way the pre-change server wrote it (raw UTF-8), then fails: the
    rollback must read the old encoding."""

    Params = LegacyUndoParams

    def run(self, ctx: OpContext, p: LegacyUndoParams) -> OpResult:
        atomic_write(ctx.lattice_dir / "notes" / f"résumé-{UNICODE}.md", "never kept\n")
        jsonl_append(ctx.lattice_dir / "events" / f"{p.task}.jsonl", '{"torn": true}\n')
        op_id = ctx.caller.origin["op_id"]
        (undo,) = (ctx.lattice_dir / "hosted" / "undo").glob(f"*--{op_id}.jsonl")
        legacy = b"".join(_legacy(line) + b"\n" for line in undo.read_bytes().splitlines())
        assert UNICODE.encode() in legacy
        undo.write_bytes(legacy)
        assert [e["path"] for e in read_undo_log(undo)] == [
            f"notes/résumé-{UNICODE}.md",
            f"events/{p.task}.jsonl",
        ]
        raise RuntimeError("fail after writing")


def test_a_legacy_utf8_undo_log_rolls_back(server: ServerHandle, token: str, root: Path) -> None:
    task = create_task(server, token)
    before = board_hash(root, "alpha")
    status, body = _op(
        server,
        token,
        "xtest.legacy_undo_then_fail",
        {"task": task["id"]},
        "op_01K5YYYYYYYYYYYYYYYYYYYYYY",
    )
    assert status == 500, body
    assert board_hash(root, "alpha") == before
    board = root / "projects" / "alpha" / ".lattice"
    assert not list((board / "notes").glob("résumé*"))
    assert not list((board / "hosted" / "undo").iterdir())


def test_a_replayed_surrogate_event_returns_its_stored_result(
    server: ServerHandle, token: str
) -> None:
    task = create_task(server, token)
    op_id = "op_01K5XXXXXXXXXXXXXXXXXXXXXX"
    params = {"task": task["id"], "event_type": "x_text", "data": '{"k": "\\ud800 ✓"}'}
    status, first = _op(server, token, "task.event", params, op_id)
    assert status == 200, first
    event = first["data"]["result"]["events"][0]
    assert event["data"] == {"k": "\ud800 ✓"}

    status, again = _op(server, token, "task.event", params, op_id)
    assert status == 200, again
    replayed = again["data"]["result"]
    assert replayed.pop("replayed") is True
    assert replayed == {k: v for k, v in first["data"]["result"].items() if k != "replayed"}
    assert again["data"]["seq"] == first["data"]["seq"]
