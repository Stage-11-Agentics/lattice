"""The change stream (SPEC §8.9): its wire, heartbeats, AC-22 (no gap, no
duplicate), and AC-23's rotation with a stream open (the rest of rotation is in
``test_rotation.py``)."""

from __future__ import annotations

import hashlib
import json
import threading
import time
from collections.abc import Iterator
from pathlib import Path

import pytest

from lattice.server import syncstate, tokens
from lattice.server.testing import BoardServer, SSEReader, apply_sync, serve_board, wait_for

HEARTBEAT = 0.2


@pytest.fixture()
def board(tmp_path: Path) -> Iterator[BoardServer]:
    with serve_board(tmp_path, heartbeat_seconds=HEARTBEAT) as served:
        yield served


def create(board: BoardServer, title: str = "t") -> str:
    return board.op("task.create", {"title": title})["task"]["id"]


def journal_seqs(reader: SSEReader, count: int, timeout: float = 10.0) -> list[int]:
    return [reader.next_of("journal", timeout).data["seq"] for _ in range(count)]


def durable_hashes(lattice_dir: Path) -> dict[str, str]:
    return {
        rel: hashlib.sha256((lattice_dir / rel).read_bytes()).hexdigest()
        for rel in syncstate.synced_files(lattice_dir)
    }


def fetch(board: BoardServer):  # noqa: ANN201
    def get(href: str) -> bytes:
        status, data = board.file_href(href)
        assert status == 200
        return data

    return get


# ---------------------------------------------------------------------------
# Wire and heartbeats
# ---------------------------------------------------------------------------


def test_an_entry_is_the_journal_line_plus_its_events(board: BoardServer) -> None:
    task = create(board)
    with board.stream() as reader:
        first = reader.next()
        assert first.event == "heartbeat" and first.id is None
        assert first.data == {"epoch": board.project.journal.epoch, "head_seq": 1}
        board.op("task.comment", {"task": task, "text": "hello"})
        message = reader.next_of("journal")
    journal = board.project.journal
    line = json.loads((board.board / "hosted" / "journal.jsonl").read_bytes().splitlines()[1])
    assert message.id == f"{journal.epoch}:2:{journal.hash_at(2)}"
    events = message.data.pop("events")
    assert message.data == line
    logged = [
        json.loads(x) for x in (board.board / "events" / f"{task}.jsonl").read_text().splitlines()
    ]
    assert events == [e for e in logged if e["id"] in line["event_ids"]]
    assert [e["type"] for e in events] == ["comment_added"]


def test_heartbeats_arrive_at_once_and_periodically(board: BoardServer) -> None:
    create(board)
    with board.stream() as reader:
        started = time.monotonic()
        beats = [reader.next_of("heartbeat") for _ in range(4)]
        elapsed = time.monotonic() - started
    assert all(b.data["head_seq"] == 1 for b in beats)
    assert HEARTBEAT * 2.5 <= elapsed < HEARTBEAT * 3 + 1.0


def test_a_revoked_token_closes_the_stream_at_the_next_heartbeat(board: BoardServer) -> None:
    with board.stream() as reader:
        assert reader.next().event == "heartbeat"
        tokens.revoke_token(board.root, board.token_id)
        started = time.monotonic()
        while (message := reader.next(timeout=5)) is not None:
            assert message.event == "heartbeat"
        assert time.monotonic() - started < HEARTBEAT * 2 + 1.0
    assert wait_for(lambda: board.project.broadcaster.count() == 0)


def test_a_stale_line_hash_or_epoch_gets_reset(board: BoardServer) -> None:
    create(board)
    epoch = board.project.journal.epoch
    for resume in (f"{epoch}:1:{'0' * 32}", f"ep_other:1:{'0' * 32}", f"{epoch}:9:x", "junk"):
        with board.stream(last_event_id=resume) as reader:
            first = reader.next()
            assert first.event == "reset" and first.data == {"epoch": epoch}, resume


# ---------------------------------------------------------------------------
# AC-22: no gap, no duplicate
# ---------------------------------------------------------------------------


def test_resume_mid_burst_delivers_exactly_one_to_n(board: BoardServer) -> None:
    """AC-22: a follower disconnects mid-burst and resumes with Last-Event-ID; the
    seqs it receives are exactly 1..N, including entries committed while it was
    subscribing."""
    task = create(board)
    total = 60
    received: list[int] = []
    stop = threading.Event()

    def writer() -> None:
        for n in range(total - 1):
            board.op("task.comment", {"task": task, "text": str(n)})
        stop.set()

    epoch = board.project.journal.epoch
    with board.stream(since=0, epoch=epoch) as reader:
        thread = threading.Thread(target=writer)
        thread.start()  # writes race the subscription and the replay
        last_id = None
        while len(received) < 20:
            message = reader.next_of("journal")
            received.append(message.data["seq"])
            last_id = message.id
    # Disconnected mid-burst; entries keep committing while it is away.
    time.sleep(0.05)
    while True:
        with board.stream(last_event_id=last_id) as reader:
            try:
                while len(received) < total:
                    message = reader.next_of("journal", timeout=5)
                    received.append(message.data["seq"])
                    last_id = message.id
                    if len(received) == 40:
                        break  # a second disconnect, then resume again
            except EOFError:
                pass
        if len(received) >= total:
            break
    thread.join(10)
    assert stop.is_set()
    assert received == list(range(1, total + 1))


def test_resume_by_query_matches_resume_by_header(board: BoardServer) -> None:
    task = create(board)
    for n in range(4):
        board.op("task.comment", {"task": task, "text": str(n)})
    journal = board.project.journal
    with board.stream(since=2, epoch=journal.epoch, hash=journal.hash_at(2)) as by_query:
        query = [(m.id, m.data) for m in (by_query.next_of("journal") for _ in range(3))]
    with board.stream(last_event_id=f"{journal.epoch}:2:{journal.hash_at(2)}") as by_header:
        header = [(m.id, m.data) for m in (by_header.next_of("journal") for _ in range(3))]
    assert query == header and [d["seq"] for _, d in query] == [3, 4, 5]


# Twenty writers on a loaded machine: every wait is on a condition (the barrier,
# each delivered entry), so the only wall-clock bound is this safety timeout.
@pytest.mark.timeout(60)
def test_twenty_concurrent_writers_deliver_strictly_increasing_seqs(board: BoardServer) -> None:
    tasks = [create(board, f"t{n}") for n in range(20)]
    epoch = board.project.journal.epoch
    head = board.project.journal.head_seq
    per_writer = 3
    with board.stream(since=head, epoch=epoch, hash=board.project.journal.hash_at(head)) as reader:
        writers = [
            tokens.create_token(board.root, user=f"human:w{n}", machine="m", projects=["demo"])
            for n in range(20)
        ]
        # All twenty are running before any writes, so their writes interleave.
        start = threading.Barrier(len(writers))
        failures: list[object] = []

        def write(task: str, minted: dict) -> None:
            actor = minted["record"]["user"]
            start.wait()
            for n in range(per_writer):
                status, _, body = board.handle.op(
                    "demo",
                    "task.comment",
                    {"task": task, "text": str(n)},
                    token=minted["token"],
                    actor=actor,
                )
                if status != 200:
                    failures.append(body)
                    return

        threads = [
            threading.Thread(target=write, args=(task, minted))
            for task, minted in zip(tasks, writers, strict=True)
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        assert failures == []
        seqs = journal_seqs(reader, 20 * per_writer, timeout=30)
    assert seqs == list(range(head + 1, head + 1 + 20 * per_writer))


# ---------------------------------------------------------------------------
# AC-23: rotation and the reset broadcast
# ---------------------------------------------------------------------------


def test_rotation_during_an_operation_resets_the_stream(board: BoardServer, tmp_path: Path):
    """AC-23 (H-10a part): rotate-epoch through a control request while a stream is
    open and an operation is in flight: the operation completes, the stream gets
    ``reset``, and a full sync from the old epoch ends byte-identical."""
    task = create(board)
    mirror = tmp_path / "mirror"
    before = board.sync()
    apply_sync(mirror, before, fetch(board))
    old_epoch = before["epoch"]
    outcome: dict = {}
    with board.stream(last_event_id=f"{old_epoch}:1:{before['head_hash']}") as reader:
        assert reader.next().event == "heartbeat"

        def slow_write() -> None:
            outcome["op"] = board.handle.op(
                "demo", "xtest.sleep", {"ms": 300}, token=board.token, actor=board.user
            )

        in_flight = threading.Thread(target=slow_write)
        in_flight.start()
        assert wait_for(lambda: board.project.work.locked())
        rotated = board.rotate_epoch()  # waits behind the operation
        in_flight.join(10)
        assert outcome["op"][0] == 200 and outcome["op"][2]["data"]["seq"] == 2
        assert rotated["via"] == "server" and rotated["old_epoch"] == old_epoch
        entry = reader.next_of("journal")
        assert entry.id.startswith(f"{old_epoch}:2:")  # the op finished in the old epoch
        reset = reader.next_of("reset")
        assert reset.data == {"epoch": rotated["epoch"]}
        beat = reader.next_of("heartbeat")
        assert beat.data == {"epoch": rotated["epoch"], "head_seq": 0}
        board.op("task.comment", {"task": task, "text": "new epoch"})
        first_new = reader.next_of("journal")
        assert first_new.id.startswith(f"{rotated['epoch']}:1:")
    resync = board.sync(since=before["head_seq"], epoch=old_epoch, hash=before["head_hash"])
    assert resync["reset"] is True and resync["epoch"] == rotated["epoch"]
    apply_sync(mirror, resync, fetch(board))
    assert durable_hashes(mirror) == durable_hashes(board.board)
    assert (board.board / "hosted" / f"journal.{old_epoch}.jsonl").exists()
    assert not (board.board / "hosted" / "rotation.json").exists()
