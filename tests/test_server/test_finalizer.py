"""The one committed-line finalizer (SPEC §8.6 step 6; plan-review resolution 2).

Transactions and ``external`` entries share ``Project.finalize_committed``: the
manifest change is staged, then swapped in with the line's hash and length
history. A failure while finalizing memory quarantines the project with memory
unchanged; a publication failure closes the project's streams.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest

from lattice.server.testing import BoardServer, serve_board
from tests.test_server.faults import Injector, install


@pytest.fixture()
def board(tmp_path: Path) -> Iterator[BoardServer]:
    with serve_board(tmp_path, heartbeat_seconds=0.2) as served:
        yield served


def test_a_finish_memory_failure_quarantines_and_closes_streams(
    board: BoardServer, monkeypatch: pytest.MonkeyPatch
) -> None:
    task = board.op("task.create", {"title": "t"})["task"]["id"]
    project = board.project
    with board.stream() as reader:
        reader.next()
        with monkeypatch.context() as m:
            install(m, Injector("finish.memory"))
            status, _, body = board.handle.op(
                "demo",
                "task.comment",
                {"task": task, "text": "x"},
                token=board.token,
                actor=board.user,
            )
        assert status == 503 and body["error"]["code"] == "BOARD_UNAVAILABLE"
        assert project.state == "unavailable"
        assert reader.next(timeout=5) is None  # streams closed with the quarantine
    # Committed on disk (never rolled back); the next load rebuilds memory from it.
    lines = (board.board / "hosted" / "journal.jsonl").read_bytes().splitlines()
    assert len(lines) == 2


def test_finish_memory_state_is_swapped_in_whole(
    board: BoardServer, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The fault fires after staging: nothing staged reached the manifest or journal."""
    task = board.op("task.create", {"title": "t"})["task"]["id"]
    project = board.project
    journal, manifest = project.journal, project.manifest
    head_before, entries_before = journal.head, dict(manifest.entries)
    with monkeypatch.context() as m:
        install(m, Injector("finish.memory"))
        board.handle.op(
            "demo",
            "task.comment",
            {"task": task, "text": "x"},
            token=board.token,
            actor=board.user,
        )
    assert journal.head == head_before  # the object the project held, unchanged
    assert manifest.entries == entries_before


def test_an_external_entry_whose_finalizer_fails_quarantines(
    board: BoardServer, monkeypatch: pytest.MonkeyPatch
) -> None:
    board.op("task.create", {"title": "t"})
    (board.board / "context.md").write_text("# edited by hand\n")
    with monkeypatch.context() as m:
        install(m, Injector("finish.memory"))
        status, _, body = board.handle.op(
            "demo", "task.create", {"title": "u"}, token=board.token, actor=board.user
        )
    assert status == 503 and body["error"]["code"] == "BOARD_UNAVAILABLE"
    assert board.project.state == "unavailable"
    assert "external" in (board.project.reason or "")


def test_an_external_entry_whose_publication_fails_closes_streams(
    board: BoardServer, monkeypatch: pytest.MonkeyPatch
) -> None:
    board.op("task.create", {"title": "t"})
    project = board.project
    real = project._publish
    calls: list[str] = []

    def failing(line: dict) -> None:
        calls.append(line["op"])
        if line["op"] == "external":
            raise OSError(5, "injected publication failure")
        real(line)

    with board.stream() as reader:
        reader.next()
        monkeypatch.setattr(project, "_publish", failing)
        monkeypatch.setattr(project, "publish", failing)
        (board.board / "context.md").write_text("# edited by hand\n")
        board.op("task.create", {"title": "u"})  # admission journals the hand edit first
        assert reader.next(timeout=5) is None  # closed, so the follower replays
    assert calls[:1] == ["external"]
    assert project.state == "loaded"
    assert project.journal.head_seq == 3
    # The manifest holds the edited file: its entry was finalized before publication.
    assert project.manifest.get("context.md").size == len("# edited by hand\n")
    with board.stream(since=1, epoch=project.journal.epoch, hash=project.journal.hash_at(1)) as r:
        assert [r.next_of("journal").data["op"] for _ in range(2)] == ["external", "task.create"]
