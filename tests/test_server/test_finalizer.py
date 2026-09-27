"""The one committed-line finalizer (SPEC §8.6 step 6; plan-review resolution 2).

Transactions and ``external`` entries share ``Project.finalize_committed``: it
computes the complete next ``FinalizedState`` (journal index, manifest, floors,
watched baselines) off to the side and publishes it with one assignment. A
failure while finalizing quarantines the project with memory unchanged; a
publication failure closes the project's streams.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path

import pytest

from lattice.server.journal import Journal
from lattice.server.project import FinalizedState
from lattice.server.syncstate import Manifest
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
# One immutable state, one assignment (review round 2, the prescribed design)
# ---------------------------------------------------------------------------

POINTS = [
    "finish.memory",
    "finish.memory.manifest",
    "finish.memory.floors",
    "finish.memory.watched",
]


def _dump(state: FinalizedState) -> str:
    """Every field of a finalized state, byte for byte (hash states by their digest)."""
    journal = state.journal
    return json.dumps(
        {
            "journal": {
                "epoch": journal.epoch,
                "baseline": dict(journal.baseline),
                "line_offsets": list(journal.line_offsets),
                "line_hashes": list(journal.line_hashes),
                "end_offset": journal.end_offset,
                "head_seq": journal.head_seq,
                "head": list(journal.head),
                "length_history": {k: list(v) for k, v in journal.length_history.items()},
                "known_lengths": dict(journal.known_lengths),
            },
            "manifest": {
                "seq": state.manifest.seq,
                "entries": {k: [e.sha256, e.size] for k, e in state.manifest.entries.items()},
                "hashers": {
                    k: [h.hexdigest(), n] for k, (h, n) in state.manifest._hashers.items()
                },
            },
            "floors": dict(state.floors.max_observed),
            "watched": {k: list(v) if v else None for k, v in state.watched.items()},
        },
        sort_keys=True,
    )


def _committed_line(board: BoardServer, task: str) -> tuple[dict, bytes, list[dict]]:
    """Append a board log and write its journal line, as a transaction does before
    its finish step: the line is committed on disk, memory not yet finalized."""
    project = board.project
    log = board.board / "events" / f"{task}.jsonl"
    event = {"id": "ev_01J9Z000000000000000000FIN", "type": "x_fin", "data": {"short_id": "DEM-7"}}
    with open(log, "ab") as fh:
        fh.write(json.dumps(event).encode() + b"\n")
    (board.board / "context.md").write_text("# changed with the line\n")
    with project.locked():
        _seq, line, raw = project.journal.write(
            {
                "op": "xtest.fin",
                "op_id": None,
                "fp": None,
                "token_id": None,
                "task_id": task,
                "event_ids": [event["id"]],
                "paths": ["context.md", f"events/{task}.jsonl"],
                "lengths": {f"events/{task}.jsonl": log.stat().st_size},
            }
        )
    return line, raw, [event]


@pytest.fixture()
def fault_free(tmp_path: Path) -> str:
    """The finalized state a fault-free run produces, on an identical board."""
    with serve_board(tmp_path / "reference") as board:
        return _reference_run(board)


def _reference_run(board: BoardServer) -> str:
    task = board.op("task.create", {"title": "t"})["task"]["id"]
    line, raw, events = _committed_line(board, task)
    with board.project.locked():
        board.project.finalize_committed(line, raw, events)
    return _normalized(board, board.project._state)


def _normalized(board: BoardServer, state: FinalizedState) -> str:
    """The state with the values that differ between two boards (ids, epochs, times,
    inodes) replaced by stable ones, so a reference run can be compared."""
    text = _dump(state)
    task = next(iter(board.board.glob("tasks/*.json"))).stem
    text = text.replace(task, "TASK").replace(state.journal.epoch, "EPOCH")
    data = json.loads(text)
    data["watched"] = sorted(data["watched"])  # stat keys hold mtimes and inodes
    data["journal"]["line_hashes"] = len(data["journal"]["line_hashes"])
    data["journal"]["head"] = data["journal"]["head"][:2]
    data["journal"].pop("line_offsets")
    data["journal"].pop("end_offset")
    data["manifest"]["entries"] = sorted(data["manifest"]["entries"])
    data["manifest"]["hashers"] = sorted(data["manifest"]["hashers"])
    return json.dumps(data, sort_keys=True)


@pytest.mark.parametrize("point", POINTS)
def test_a_fault_anywhere_leaves_the_live_state_unchanged_and_a_retry_completes_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, point: str, fault_free: str
) -> None:
    """(a) the live state is byte-for-byte unchanged after a fault after each
    sub-step; (b) a retry completes to exactly the fault-free state; (c) a second
    retry is a no-op."""
    with serve_board(tmp_path / "board") as board:
        task = board.op("task.create", {"title": "t"})["task"]["id"]
        line, raw, events = _committed_line(board, task)
        project = board.project
        before = project._state
        before_dump = _dump(before)
        with project.locked():
            with monkeypatch.context() as m:
                injector = install(m, Injector(point))
                with pytest.raises(OSError):
                    project.finalize_committed(line, raw, events)
                assert injector.fired
            assert project._state is before  # (a) nothing was published
            assert _dump(project._state) == before_dump
            project.finalize_committed(line, raw, events)  # (b) the retry
            after = project._state
            assert after is not before and after.journal.head_seq == line["seq"]
            project.finalize_committed(line, raw, events)  # (c) a no-op
            assert project._state is after
        assert _normalized(board, after) == fault_free
        # Independent rebuilds agree with the retried state.
        rebuilt = Journal.load(board.board).index
        assert (rebuilt.line_offsets, rebuilt.line_hashes, rebuilt.end_offset) == (
            after.journal.line_offsets,
            after.journal.line_hashes,
            after.journal.end_offset,
        )
        assert dict(rebuilt.length_history) == dict(after.journal.length_history)
        assert rebuilt.head == after.journal.head
        assert dict(Manifest.build(board.board).entries) == dict(after.manifest.entries)
        assert after.floors.max_observed["DEM"] == 7
        assert after.watched["context.md"] is not None
        assert after.watched["context.md"] != before.watched["context.md"]
        # And the server serves the finalized state.
        body = board.sync(
            since=line["seq"], epoch=after.journal.epoch, hash=after.journal.head_hash
        )
        assert body["head_seq"] == line["seq"] and body["files"] == {}


def test_the_head_advances_only_through_the_single_assignment(board: BoardServer) -> None:
    """``Journal.accept`` on a project's journal is refused; its index is the state's."""
    project = board.project
    assert project.journal.index is project._state.journal
    with pytest.raises(RuntimeError):
        project.journal.accept({"seq": 1, "paths": [], "lengths": {}}, b"{}")
