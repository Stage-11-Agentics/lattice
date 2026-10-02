"""Hosted watch never loses an event: scans after ``unchanged`` syncs, and
offsets that survive a reset (review round 1, finding 2)."""

from __future__ import annotations

import contextlib
import json
import threading

from lattice.remote.hosted_watch import _cache_epoch, hosted_stream_events
from lattice.storage.fs import LATTICE_DIR
from tests.test_remote.stream_stub import SLUG, StubSyncer, stub_remote, wait_for


def _line(n: int, kind: str = "comment_added") -> bytes:
    return (
        json.dumps({"id": f"ev_{n}", "type": kind, "ts": f"2026-09-27T06:00:{n:02d}Z"}) + "\n"
    ).encode()


def _lock(root):
    return contextlib.nullcontext(root / LATTICE_DIR)


def _collect(gen, out: list, expected: int) -> threading.Thread:
    """Consume *gen* in a thread until *expected* events (closing it stops the
    follower); its timeout is only a safety bound."""

    def run() -> None:
        for event in gen:
            out.append(event)
            if len(out) >= expected:
                break

    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    return thread


def test_event_applied_by_another_process_is_still_printed(tmp_path, stream_stub) -> None:
    """Another process's sync applies the update first; this follower's own
    sync then sees ``unchanged`` and must still scan the new log bytes."""
    syncer = StubSyncer(stream_stub.url)
    other_process = StubSyncer(stream_stub.url)
    outcomes: list[str] = []

    def catch_up(root, *, bulk=False):
        other_process(root)  # wins the race and applies the delta
        outcome = syncer(root, bulk=bulk)
        outcomes.append(outcome.kind)
        return outcome

    (tmp_path / LATTICE_DIR).mkdir()
    events: list[dict] = []
    gen = hosted_stream_events(
        tmp_path,
        stub_remote(stream_stub.url),
        SLUG,
        catch_up=catch_up,
        read_lock=_lock,
        timeout=8,
        heartbeat_seconds=0.2,
    )
    thread = _collect(gen, events, 1)
    assert wait_for(lambda: len(stream_stub.subscribers) == 1, 5)
    # Let the follower's first (heartbeat-driven) sync finish before writing, so
    # the write cannot land between the other process's request and its own.
    assert wait_for(lambda: len(outcomes) >= 2, 5), outcomes
    before = len(outcomes)
    stream_stub.write({"events/T1.jsonl": _line(1)})
    assert wait_for(lambda: len(events) == 1, 5), (events, outcomes)
    assert events[0]["id"] == "ev_1" and events[0]["task_id"] == "T1"
    assert len(outcomes) > before
    assert "applied" not in outcomes[before:]  # the follower's own syncs saw unchanged
    thread.join(4)
    assert not thread.is_alive()


def test_shorter_log_after_reset_does_not_suppress_later_events(tmp_path, stream_stub) -> None:
    syncer = StubSyncer(stream_stub.url)
    syncs: list[str] = []

    def catch_up(root, *, bulk=False):
        outcome = syncer(root, bulk=bulk)
        syncs.append(outcome.kind)
        return outcome

    long_log = b"".join(_line(n) for n in range(1, 11))
    stream_stub.files["events/T1.jsonl"] = long_log
    (tmp_path / LATTICE_DIR).mkdir()
    # The cache epoch each of the watch loop's scans saw, recorded once the scan is
    # done: the loop has taken its offsets from the reset cache when one saw it.
    scanned: list[str | None] = []

    @contextlib.contextmanager
    def recording_lock(root):
        epoch = _cache_epoch(root)
        yield root / LATTICE_DIR
        scanned.append(epoch)

    events: list[dict] = []
    gen = hosted_stream_events(
        tmp_path,
        stub_remote(stream_stub.url),
        SLUG,
        catch_up=catch_up,
        read_lock=recording_lock,
        timeout=12,
        # Long enough that a loaded machine never finds the stream silent and
        # polls: only the stream's messages trigger syncs.
        heartbeat_seconds=10,
    )
    thread = _collect(gen, events, 1)
    # The follower's own first sync after connecting, and its scan, are done
    # before the reset: under load that sync otherwise ran after the rotation,
    # applied the new epoch ahead of the reset message, and the reset's resync
    # then took the offsets after the append (a separate follower race).
    assert wait_for(lambda: len(stream_stub.subscribers) == 1, 5)
    assert wait_for(lambda: len(syncs) >= 2 and len(scanned) >= 2, 5), (syncs, scanned)

    # The history is rebuilt: T1's log is now one line, far shorter than before.
    stream_stub.rotate_epoch({"config.json": b"{}\n", "events/T1.jsonl": _line(50)})
    assert wait_for(lambda: stream_stub.epoch in scanned, 5), scanned
    # A later append must be printed, though the log is still shorter than before.
    stream_stub.write({"events/T1.jsonl": _line(50) + _line(51)})
    assert wait_for(lambda: [e["id"] for e in events] == ["ev_51"], 5), events
    thread.join(4)
    assert not thread.is_alive()


def test_log_replaced_by_a_reset_this_process_did_not_announce(tmp_path, stream_stub) -> None:
    """A shorter log without a reset seen (another process applied it) restarts
    at its size, so its next line is printed and its history is not."""
    from lattice.remote.hosted_watch import _scan, _snapshot_offsets

    events_dir = tmp_path / "events"
    events_dir.mkdir()
    log = events_dir / "T1.jsonl"
    log.write_bytes(b"".join(_line(n) for n in range(1, 6)))
    offsets, last_paths = _snapshot_offsets(events_dir)
    log.write_bytes(_line(60))
    assert _scan(events_dir, offsets, last_paths) == []
    log.write_bytes(_line(60) + _line(61))
    assert [e["id"] for e in _scan(events_dir, offsets, last_paths)] == ["ev_61"]


def test_scanner_finds_archive_directory_created_after_start(tmp_path) -> None:
    from lattice.remote.hosted_watch import _scan, _snapshot_offsets

    lattice_dir = tmp_path / LATTICE_DIR
    events_dir = lattice_dir / "events"
    events_dir.mkdir(parents=True)
    offsets, last_paths = _snapshot_offsets(lattice_dir)
    assert not (lattice_dir / "archive" / "events").exists()

    archived = lattice_dir / "archive" / "events" / "T2.jsonl"
    archived.parent.mkdir(parents=True)
    archived.write_bytes(_line(1, "task_archived"))

    assert [
        (event["id"], event["task_id"]) for event in _scan(lattice_dir, offsets, last_paths)
    ] == [("ev_1", "T2")]


def test_cache_scanner_carries_offsets_across_moves_and_deduplicates_lifecycle(
    tmp_path,
) -> None:
    import shutil

    from lattice.core.event_stream import _filtered_unique
    from lattice.remote.hosted_watch import _scan, _snapshot_offsets

    lattice_dir = tmp_path / LATTICE_DIR
    active_dir = lattice_dir / "events"
    active_dir.mkdir(parents=True)
    active = active_dir / "T1.jsonl"
    lifecycle = active_dir / "_lifecycle.jsonl"
    first = (
        json.dumps(
            {"id": "ev_1", "task_id": "SERIALIZED", "type": "task_created", "ts": "1"}
        ).encode()
        + b"\n"
    )
    active.write_bytes(first)
    lifecycle.write_bytes(first)
    offsets, last_paths = _snapshot_offsets(lattice_dir)

    next_event = (
        json.dumps(
            {"id": "ev_2", "task_id": "SERIALIZED", "type": "task_archived", "ts": "2"}
        ).encode()
        + b"\n"
    )
    active.write_bytes(active.read_bytes() + next_event)
    lifecycle.write_bytes(lifecycle.read_bytes() + next_event)
    batch = _scan(lattice_dir, offsets, last_paths)
    unique = list(_filtered_unique(batch, ["SERIALIZED"], ["task_archived"], set()))
    assert [(event["id"], event["task_id"]) for event in unique] == [("ev_2", "SERIALIZED")]

    archived = lattice_dir / "archive" / "events" / active.name
    archived.parent.mkdir(parents=True)
    shutil.move(active, archived)
    third = (
        json.dumps(
            {"id": "ev_3", "task_id": "SERIALIZED", "type": "task_archived", "ts": "3"}
        ).encode()
        + b"\n"
    )
    archived.write_bytes(archived.read_bytes() + third)
    assert [event["id"] for event in _scan(lattice_dir, offsets, last_paths)] == ["ev_3"]

    active.parent.mkdir(parents=True, exist_ok=True)
    shutil.move(archived, active)
    fourth = (
        json.dumps(
            {"id": "ev_4", "task_id": "SERIALIZED", "type": "task_archived", "ts": "4"}
        ).encode()
        + b"\n"
    )
    active.write_bytes(active.read_bytes() + fourth)
    assert [event["id"] for event in _scan(lattice_dir, offsets, last_paths)] == ["ev_4"]


def test_unannounced_reset_while_polling_with_a_longer_log(tmp_path, stream_stub) -> None:
    """The stream is refused, so the follower polls and never sees ``reset``;
    a poll applies the new epoch, whose T1 log is longer than the old offset.
    Its history must not be printed; the next real append must (round 2, item 3)."""
    from lattice.remote.follower import Follower
    from tests.test_remote.stream_stub import TestProxy

    stream_stub.files["events/T1.jsonl"] = _line(1) + _line(2)
    (tmp_path / LATTICE_DIR).mkdir()
    followers: list[Follower] = []

    def factory(*args, **kwargs) -> Follower:
        followers.append(Follower(*args, **kwargs))
        return followers[-1]

    with TestProxy(stream_stub.url, "refuse").running() as proxy:
        syncer = StubSyncer(proxy.url)
        events: list[dict] = []
        gen = hosted_stream_events(
            tmp_path,
            stub_remote(proxy.url),
            SLUG,
            catch_up=syncer,
            read_lock=_lock,
            timeout=8,
            heartbeat_seconds=0.2,
            follower_factory=factory,
        )
        thread = _collect(gen, events, 1)
        assert wait_for(lambda: followers and followers[0].polling, 3)
        old_epoch = syncer.state(tmp_path)["epoch"]
        replacement = b"".join(_line(n) for n in range(20, 26))  # 6 lines, longer
        stream_stub.rotate_epoch({"config.json": b"{}\n", "events/T1.jsonl": replacement})
        assert wait_for(lambda: syncer.state(tmp_path).get("epoch") not in (None, old_epoch), 3)
        stream_stub.write({"events/T1.jsonl": replacement + _line(30)})
        assert wait_for(lambda: len(events) >= 1, 3), events
        thread.join(4)
        assert [e["id"] for e in events] == ["ev_30"], events
        assert followers[0].deliveries["reset"] == 0  # the reset was never announced
