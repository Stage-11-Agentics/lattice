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


# ---------------------------------------------------------------------------
# Atomic and idempotent, directly (review round 1, finding 1)
# ---------------------------------------------------------------------------


def _live(project) -> tuple:  # noqa: ANN001
    """A deep snapshot of every in-memory component the finalizer changes."""
    journal, manifest = project.journal, project.manifest
    return (
        journal.head,
        journal.head_seq,
        journal.end_offset,
        list(journal.line_hashes),
        list(journal.line_offsets),
        {k: list(v) for k, v in journal.length_history.items()},
        dict(journal.known_lengths),
        manifest.seq,
        dict(manifest.entries),
        {k: v[1] for k, v in manifest._hashers.items()},
    )


@pytest.mark.parametrize(
    "fail",
    ["finish.memory", "finish.memory.manifest", "manifest.apply"],
)
def test_a_failed_finalize_changes_nothing_live_or_completes_on_retry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fail: str
) -> None:
    from lattice.server import syncstate
    from lattice.server.journal import Journal
    from lattice.server.syncstate import Manifest

    with serve_board(tmp_path) as board:
        task = board.op("task.create", {"title": "t"})["task"]["id"]
        board.op("task.comment", {"task": task, "text": "one"})
        project = board.project
        journal_path = board.board / "hosted" / "journal.jsonl"
        raw_before = journal_path.read_bytes()
        with project.locked():
            before = _live(project)
            seq, line, raw = project.journal.write(
                {
                    "op": "external",
                    "op_id": None,
                    "fp": None,
                    "token_id": None,
                    "task_id": None,
                    "event_ids": [],
                    "paths": ["context.md", f"events/{task}.jsonl"],
                    "lengths": {},
                }
            )
            (board.board / "context.md").write_text("# changed\n")
            with monkeypatch.context() as m:
                if fail == "manifest.apply":
                    # After the journal's commit: the one step that could leave a
                    # half-updated memory if it were not idempotent by seq.
                    def broken(self, staged, seq=None):  # noqa: ANN001, ANN202
                        raise OSError(5, "injected")

                    m.setattr(syncstate.Manifest, "apply", broken)
                else:
                    install(m, Injector(fail))
                with pytest.raises(OSError):
                    project.finalize_committed(line, raw)
            if fail != "manifest.apply":
                assert _live(project) == before  # every live component unchanged
            else:
                assert project.manifest.seq == before[7]  # the manifest did not move
            project.finalize_committed(line, raw)  # the retry completes it
            project.finalize_committed(line, raw)  # and a second retry is a no-op
            after = _live(project)
        assert journal_path.read_bytes().startswith(raw_before)
        fresh = Journal.load(board.board)
        assert after[:7] == (
            fresh.head,
            fresh.head_seq,
            fresh.end_offset,
            fresh.line_hashes,
            fresh.line_offsets,
            {k: list(v) for k, v in fresh.length_history.items()},
            fresh.known_lengths,
        )
        rebuilt = Manifest.build(board.board)
        assert after[7] == seq and after[8] == rebuilt.entries
        # The server keeps serving, and a sync at the new head answers from memory.
        body = board.sync(since=seq, epoch=fresh.epoch, hash=fresh.head_hash)
        assert body["head_seq"] == seq and body["files"] == {}


def test_journal_commit_drops_what_an_interrupted_commit_left(tmp_path: Path) -> None:
    from lattice.server.journal import Journal
    from lattice.storage.ownership import owning_board

    lattice = tmp_path / ".lattice"
    (lattice / "hosted").mkdir(parents=True)
    (lattice / "events").mkdir()
    (lattice / "config.json").write_text("{}")
    with owning_board(lattice):
        journal = Journal.create(lattice)
    line = {"seq": 1, "paths": ["events/a.jsonl"], "lengths": {"events/a.jsonl": 5}}
    delta = journal.stage(line, b'{"seq":1}')
    # Simulate a commit interrupted after its first assignments.
    journal.line_hashes.append("partial")
    journal.length_history.setdefault("events/a.jsonl", []).append((1, 99))
    journal.commit(delta)
    journal.commit(delta)  # idempotent
    assert journal.line_hashes == [delta.digest]
    assert journal.length_history == {"events/a.jsonl": [(1, 5)]}
    assert journal.head == (journal.epoch, 1, delta.digest)
