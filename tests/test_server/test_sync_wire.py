"""The sync wire (SPEC §8.8; plan-review resolution 1): response shapes, whole-file
hashes, the manifest form, and which paths may ever be returned."""

from __future__ import annotations

import base64
import dataclasses
import hashlib
import json
import os
import re
from collections.abc import Iterator
from pathlib import Path

import pytest

from lattice.server import syncstate
from lattice.server.testing import BoardServer, serve_board


@pytest.fixture()
def board(tmp_path: Path) -> Iterator[BoardServer]:
    with serve_board(tmp_path, audit=False) as served:
        yield served


def durable_hashes(lattice_dir: Path) -> dict[str, str]:
    return {
        rel: hashlib.sha256((lattice_dir / rel).read_bytes()).hexdigest()
        for rel in syncstate.synced_files(lattice_dir)
    }


def create(board: BoardServer, title: str = "t") -> str:
    return board.op("task.create", {"title": title})["task"]["id"]


def test_only_synced_board_paths_are_returned(board: BoardServer) -> None:
    lattice = board.board
    (lattice / "reviews").mkdir()
    (lattice / "reviews" / "r.md").write_text("unmanaged")
    (lattice / "runner.log").write_text("unmanaged")
    (lattice / "locks" / "x.lock").write_text("")
    (lattice / "orchestration").mkdir()
    project = board.project
    project._state = dataclasses.replace(
        project._state, manifest=syncstate.Manifest.build(lattice, project.journal.head_seq)
    )
    files = board.sync()["files"]
    assert not any(p.startswith(("reviews/", "locks/", "hosted/", "cache/")) for p in files)
    assert "runner.log" not in files
    workspace = {p for p in files if p.startswith("orchestration/")}
    assert workspace == set()  # an empty workspace directory holds no files


def test_manifest_lists_hashes_only(board: BoardServer) -> None:
    create(board)
    body = board.sync(manifest=True)
    assert body["reset"] is True and body["removed"] == []
    assert {rel: spec["sha256"] for rel, spec in body["files"].items()} == durable_hashes(
        board.board
    )
    assert all(set(spec) == {"sha256", "size"} for spec in body["files"].values())


HEX32 = re.compile(r"^[0-9a-f]{32}$")
HEX64 = re.compile(r"^[0-9a-f]{64}$")


def test_response_shape_at_seq_zero_and_at_a_nonzero_head(board: BoardServer) -> None:
    """Resolution 1: ``head_hash`` is absent exactly at seq 0, else 32 lowercase hex."""
    empty = board.sync()
    assert set(empty) == {"epoch", "head_seq", "reset", "files", "removed"}
    assert empty["head_seq"] == 0
    create(board)
    for body in (
        board.sync(),
        board.sync(since=0, epoch=empty["epoch"]),
        board.sync(manifest=True),
    ):
        assert set(body) == {"epoch", "head_seq", "head_hash", "reset", "files", "removed"}
        assert body["head_seq"] == 1 and HEX32.fullmatch(body["head_hash"])
        assert body["head_hash"] == board.project.journal.hash_at(1)
    at_head = board.sync(since=1, epoch=empty["epoch"], hash=board.project.journal.hash_at(1))
    assert HEX32.fullmatch(at_head["head_hash"]) and at_head["files"] == {}


def test_an_append_delta_describes_the_whole_file(board: BoardServer) -> None:
    """Resolution 1: ``sha256`` and ``size`` are the whole current file's, from the
    manifest; ``content_b64`` is only bytes ``append_from..size``."""
    task = create(board)
    head = board.sync()
    board.op("task.comment", {"task": task, "text": "more"})
    delta = board.sync(since=1, epoch=head["epoch"], hash=head["head_hash"])
    rel = f"events/{task}.jsonl"
    spec = delta["files"][rel]
    whole = (board.board / rel).read_bytes()
    entry = board.project.manifest.get(rel)
    assert set(spec) == {"sha256", "size", "append_from", "content_b64", "href"}
    assert HEX64.fullmatch(spec["sha256"])
    assert (spec["sha256"], spec["size"]) == (entry.sha256, entry.size)
    assert spec["sha256"] == hashlib.sha256(whole).hexdigest() and spec["size"] == len(whole)
    assert base64.b64decode(spec["content_b64"]) == whole[spec["append_from"] :]
    for other in delta["files"].values():
        assert HEX64.fullmatch(other["sha256"])


def test_symlinks_never_carry_outside_bytes_into_a_sync(board: BoardServer, tmp_path: Path):
    """Non-blocking (a): a reset and a delta never read through a symlink, a file
    link or a linked directory, out of the board."""
    secret_dir = tmp_path / "outside"
    secret_dir.mkdir()
    (secret_dir / "leak.md").write_text("SECRET")
    task = create(board)
    head = board.sync()
    os.symlink(secret_dir / "leak.md", board.board / "notes" / "leak.md")
    os.symlink(secret_dir, board.board / "plans" / "linked")
    # A journaled change naming those paths (an operation would never write them).
    project = board.project

    with project.locked():
        _seq, line, raw = project.journal.write(
            {
                "op": "external",
                "op_id": None,
                "fp": None,
                "token_id": None,
                "task_id": None,
                "event_ids": [],
                "paths": ["notes/leak.md", "plans/linked/leak.md"],
                "lengths": {},
            }
        )
        project.finalize_committed(line, raw)
    board.op("task.comment", {"task": task, "text": "x"})
    for body in (
        board.sync(),
        board.sync(since=head["head_seq"], epoch=head["epoch"], hash=head["head_hash"]),
        board.sync(manifest=True),
    ):
        text = json.dumps(body)
        assert "notes/leak.md" not in body["files"] and "plans/linked/leak.md" not in body["files"]
        assert base64.b64encode(b"SECRET").decode() not in text
    assert board.project.manifest.get("notes/leak.md") is None
    rebuilt = syncstate.Manifest.build(board.board)
    assert rebuilt.get("notes/leak.md") is None and rebuilt.get("plans/linked/leak.md") is None
