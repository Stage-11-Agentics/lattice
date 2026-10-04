"""Pure scanner coverage for local watch/wait event projections."""

from __future__ import annotations

import json
import io
import os
import select
import shutil
from types import SimpleNamespace
from pathlib import Path

import pytest

import lattice.core.event_stream as event_stream
from lattice.core.event_stream import (
    _filtered_unique,
    _parse_jsonl_file,
    _scan_event_logs,
    _snapshot_event_offsets,
    stream_events,
)


def _line(event_id: str, task_id: str, kind: str = "comment_added") -> bytes:
    return (
        json.dumps(
            {
                "id": event_id,
                "task_id": task_id,
                "type": kind,
                "ts": f"2026-10-02T12:00:{event_id[-1:]}Z",
            }
        )
        + "\n"
    ).encode()


class _ScriptedReader:
    def __init__(self, chunks: list[bytes]) -> None:
        self.chunks = list(chunks)
        self.read_sizes: list[int] = []

    def read1(self, size: int) -> bytes:
        self.read_sizes.append(size)
        return self.chunks.pop(0) if self.chunks else b""


class _FakeProcess:
    def __init__(self, stdout: object, returncode: int | None = None) -> None:
        self.stdout = stdout
        self.returncode = returncode
        self.terminated = False
        self.wait_calls: list[float | None] = []

    def poll(self) -> int | None:
        return self.returncode

    def terminate(self) -> None:
        self.terminated = True
        self.returncode = 0

    def wait(self, timeout: float | None = None) -> int | None:
        self.wait_calls.append(timeout)
        return self.returncode


def _install_fswatch(
    monkeypatch: pytest.MonkeyPatch,
    stdout: object,
    select_fn,
    *,
    returncode: int | None = None,
) -> tuple[_FakeProcess, list[list[str]]]:
    process = _FakeProcess(stdout, returncode=returncode)
    commands: list[list[str]] = []

    def fake_popen(command: list[str], **_kwargs: object) -> _FakeProcess:
        commands.append(command)
        return process

    monkeypatch.setattr(event_stream, "_check_fswatch", lambda: True)
    monkeypatch.setattr(event_stream.subprocess, "Popen", fake_popen)
    monkeypatch.setattr(select, "select", select_fn)
    return process, commands


def test_parser_preserves_serialized_task_id_and_only_falls_back_when_missing(
    tmp_path: Path,
) -> None:
    path = tmp_path / "_lifecycle.jsonl"
    path.write_bytes(
        b'{"id":"ev_1","task_id":"task_real","type":"task_created"}\n'
        b'{"id":"ev_2","type":"task_created"}\n'
    )

    events, offset = _parse_jsonl_file(path, 0)

    assert [event["task_id"] for event in events] == ["task_real", "_lifecycle"]
    assert offset == path.stat().st_size


def test_scanner_discovers_new_archive_directory_after_start(tmp_path: Path) -> None:
    lattice_dir = tmp_path / ".lattice"
    (lattice_dir / "events").mkdir(parents=True)
    offsets, last_paths = _snapshot_event_offsets(lattice_dir)
    assert not (lattice_dir / "archive" / "events").exists()

    archived = lattice_dir / "archive" / "events" / "task_archived.jsonl"
    archived.parent.mkdir(parents=True)
    archived.write_bytes(_line("ev_1", "task_archived", "task_archived"))

    events = _scan_event_logs(lattice_dir, offsets, last_paths)
    assert [(event["id"], event["task_id"]) for event in events] == [("ev_1", "task_archived")]


def test_offsets_follow_active_archive_and_active_moves_without_replay_or_gap(
    tmp_path: Path,
) -> None:
    lattice_dir = tmp_path / ".lattice"
    active_dir = lattice_dir / "events"
    active_dir.mkdir(parents=True)
    active = active_dir / "task_1.jsonl"
    active.write_bytes(_line("ev_1", "task_1"))
    offsets, last_paths = _snapshot_event_offsets(lattice_dir)

    with active.open("ab") as handle:
        handle.write(_line("ev_2", "task_1"))
    assert [event["id"] for event in _scan_event_logs(lattice_dir, offsets, last_paths)] == [
        "ev_2"
    ]

    archived = lattice_dir / "archive" / "events" / active.name
    archived.parent.mkdir(parents=True)
    shutil.move(active, archived)
    with archived.open("ab") as handle:
        handle.write(_line("ev_3", "task_1"))
    assert [event["id"] for event in _scan_event_logs(lattice_dir, offsets, last_paths)] == [
        "ev_3"
    ]

    active.parent.mkdir(parents=True, exist_ok=True)
    shutil.move(archived, active)
    with active.open("ab") as handle:
        handle.write(_line("ev_4", "task_1"))
    assert [event["id"] for event in _scan_event_logs(lattice_dir, offsets, last_paths)] == [
        "ev_4"
    ]


def test_lifecycle_mirror_yields_once_with_filters_applied_to_serialized_task(
    tmp_path: Path,
) -> None:
    lattice_dir = tmp_path / ".lattice"
    events_dir = lattice_dir / "events"
    events_dir.mkdir(parents=True)
    task_log = events_dir / "task_1.jsonl"
    lifecycle = events_dir / "_lifecycle.jsonl"
    mirrored = _line("ev_1", "task_1", "task_created")
    task_log.write_bytes(mirrored)
    lifecycle.write_bytes(mirrored)
    offsets, last_paths = _snapshot_event_offsets(lattice_dir)

    task_log.write_bytes(task_log.read_bytes() + _line("ev_2", "task_1", "task_created"))
    lifecycle.write_bytes(lifecycle.read_bytes() + _line("ev_2", "task_1", "task_created"))
    batch = _scan_event_logs(lattice_dir, offsets, last_paths)
    filtered = list(_filtered_unique(batch, ["task_1"], ["task_created"], set()))

    assert [event["id"] for event in filtered] == ["ev_2"]
    assert filtered[0]["task_id"] == "task_1"


def test_filters_exclude_nonmatching_task_and_type(tmp_path: Path) -> None:
    events = [
        {"id": "ev_1", "task_id": "task_1", "type": "comment_added"},
        {"id": "ev_2", "task_id": "task_2", "type": "task_archived"},
    ]

    assert list(_filtered_unique(events, ["task_1"], ["comment_added"], set())) == [events[0]]
    assert list(_filtered_unique(events, ["task_1"], ["task_archived"], set())) == []


def test_fswatch_command_watches_recursively(tmp_path: Path, monkeypatch) -> None:
    lattice_dir = tmp_path / ".lattice"
    (lattice_dir / "events").mkdir(parents=True)
    _process, commands = _install_fswatch(
        monkeypatch,
        io.BytesIO(),
        lambda readers, *_args: (readers, [], []),
    )

    assert list(stream_events(lattice_dir, timeout=1)) == []
    assert len(commands) == 1
    assert "-r" in commands[0]


def test_fswatch_parses_split_bursts_and_filesystem_paths_before_one_scan(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    lattice_dir = tmp_path / ".lattice"
    events_dir = lattice_dir / "events"
    events_dir.mkdir(parents=True)
    first_path = os.fsencode(str(events_dir / "partial.jsonl"))
    invalid_path = os.fsencode(str(events_dir)) + b"/bad_\xff.jsonl"
    last_paths = [
        os.fsencode(str(events_dir / "second.jsonl")),
        os.fsencode(str(events_dir / "third.jsonl")),
    ]
    reader = _ScriptedReader(
        [
            first_path[:7],
            first_path[7:]
            + b"\0"
            + invalid_path
            + b"\0"
            + last_paths[0]
            + b"\0"
            + b"/irrelevant/not-an-event.txt\0"
            + last_paths[1]
            + b"\0",
        ]
    )
    process, _commands = _install_fswatch(
        monkeypatch,
        reader,
        lambda readers, *_args: (readers, [], []),
    )
    expected_event = {"id": "ev_1", "task_id": "task_1", "type": "comment_added"}
    scan_calls: list[Path] = []

    def fake_scan(
        scan_dir: Path,
        _offsets: dict[str, int],
        _last_paths: dict[str, Path],
    ) -> list[dict]:
        scan_calls.append(scan_dir)
        return [expected_event]

    original_path = event_stream.Path
    observed_notifications: list[Path] = []

    def observe_path(value: str | Path) -> Path:
        path = original_path(value)
        if isinstance(value, str) and value != str(lattice_dir):
            observed_notifications.append(path)
        return path

    monkeypatch.setattr(event_stream, "Path", observe_path)
    monkeypatch.setattr(event_stream, "_scan_event_logs", fake_scan)
    events = stream_events(lattice_dir, timeout=5)
    try:
        assert next(events) == expected_event
    finally:
        events.close()

    assert scan_calls == [lattice_dir.resolve()]
    assert reader.read_sizes == [65536, 65536]
    assert observed_notifications == [
        Path(os.fsdecode(first_path)),
        Path(os.fsdecode(invalid_path)),
        Path(os.fsdecode(last_paths[0])),
        Path("/irrelevant/not-an-event.txt"),
        Path(os.fsdecode(last_paths[1])),
    ]
    assert process.terminated


def test_fswatch_idle_timeout_uses_remaining_deadline(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clock = [30.0]
    waits: list[float] = []

    def fake_select(_readers, _writers, _errors, timeout: float):
        waits.append(timeout)
        clock[0] += timeout
        return [], [], []

    _install_fswatch(monkeypatch, _ScriptedReader([]), fake_select)
    monkeypatch.setattr(
        event_stream,
        "time",
        SimpleNamespace(monotonic=lambda: clock[0]),
    )

    assert list(stream_events(tmp_path / ".lattice", timeout=3)) == []
    assert waits == [3.0]
    assert clock[0] == 33.0


def test_fswatch_repeated_irrelevant_notifications_do_not_extend_timeout(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clock = [0.0]
    waits: list[float] = []
    reader = _ScriptedReader([b"/tmp/irrelevant.txt\0", b"/tmp/also-irrelevant.txt\0"])

    def fake_select(readers, _writers, _errors, timeout: float):
        waits.append(timeout)
        if len(waits) <= 2:
            clock[0] += 0.75
            return readers, [], []
        clock[0] += timeout
        return [], [], []

    _install_fswatch(monkeypatch, reader, fake_select)
    monkeypatch.setattr(
        event_stream,
        "time",
        SimpleNamespace(monotonic=lambda: clock[0]),
    )

    assert list(stream_events(tmp_path / ".lattice", timeout=2)) == []
    assert waits == [2.0, 1.25, 0.5]
    assert clock[0] == 2.0


def test_fswatch_eof_returns_without_retrying(tmp_path: Path, monkeypatch) -> None:
    reader = _ScriptedReader([])
    process, _commands = _install_fswatch(
        monkeypatch,
        reader,
        lambda readers, *_args: (readers, [], []),
    )

    assert list(stream_events(tmp_path / ".lattice", timeout=5)) == []
    assert reader.read_sizes == [65536]
    assert process.terminated


def test_fswatch_exited_child_returns_when_pipe_is_not_ready(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    select_calls: list[None] = []

    def not_ready(_readers, *_args):
        select_calls.append(None)
        return [], [], []

    process, _commands = _install_fswatch(
        monkeypatch,
        _ScriptedReader([]),
        not_ready,
        returncode=1,
    )

    assert list(stream_events(tmp_path / ".lattice", timeout=5)) == []
    assert len(select_calls) == 1
    assert not process.terminated


def test_fswatch_persistent_descriptor_error_raises_instead_of_spinning(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    select_calls: list[None] = []

    def bad_descriptor(_readers, *_args):
        select_calls.append(None)
        raise OSError("bad descriptor")

    process, _commands = _install_fswatch(
        monkeypatch,
        _ScriptedReader([]),
        bad_descriptor,
    )

    with pytest.raises(RuntimeError, match="fswatch notification stream failed"):
        list(stream_events(tmp_path / ".lattice", timeout=5))

    assert len(select_calls) == 1
    assert process.terminated


@pytest.mark.timeout(2)
def test_fswatch_read1_returns_path_from_open_real_pipe(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    lattice_dir = tmp_path / ".lattice"
    events_dir = lattice_dir / "events"
    events_dir.mkdir(parents=True)
    event_path = events_dir / "task_1.jsonl"
    event_path.write_bytes(_line("ev_0", "task_1"))
    expected_event = {
        "id": "ev_1",
        "task_id": "task_1",
        "type": "comment_added",
        "ts": "2026-10-02T12:00:1Z",
    }
    read_fd, write_fd = os.pipe()
    stdout = os.fdopen(read_fd, "rb")
    process = _FakeProcess(stdout)
    writer_open = [True]
    commands: list[list[str]] = []

    def terminate() -> None:
        process.terminated = True
        process.returncode = 0
        if writer_open[0]:
            os.close(write_fd)
            writer_open[0] = False

    def wait(timeout: float | None = None) -> int:
        process.wait_calls.append(timeout)
        stdout.close()
        return process.returncode or 0

    process.terminate = terminate
    process.wait = wait

    def fake_popen(command: list[str], **_kwargs: object) -> _FakeProcess:
        commands.append(command)
        with event_path.open("ab") as handle:
            handle.write(_line("ev_1", "task_1"))
        os.write(write_fd, os.fsencode(str(event_path)) + b"\0")
        return process

    monkeypatch.setattr(event_stream, "_check_fswatch", lambda: True)
    monkeypatch.setattr(event_stream.subprocess, "Popen", fake_popen)
    events = stream_events(lattice_dir, timeout=5)
    try:
        assert next(events) == expected_event
        os.fstat(write_fd)
        assert writer_open[0]
        assert not process.terminated
    finally:
        events.close()

    assert process.terminated
    assert len(commands) == 1


def test_stream_events_uses_polling_fallback_and_yields_appended_event(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    lattice_dir = tmp_path / ".lattice"
    events_dir = lattice_dir / "events"
    events_dir.mkdir(parents=True)
    event_path = events_dir / "task_1.jsonl"
    event_path.write_bytes(_line("ev_0", "task_1"))
    clock = [0.0]
    sleep_calls: list[float] = []
    appended = False

    def fake_sleep(seconds: float) -> None:
        nonlocal appended
        sleep_calls.append(seconds)
        if not appended:
            with event_path.open("ab") as handle:
                handle.write(_line("ev_1", "task_1"))
            appended = True
        clock[0] += seconds

    def unexpected_popen(*_args, **_kwargs):
        pytest.fail("fswatch process started despite unavailable fswatch")

    monkeypatch.setattr(event_stream, "_check_fswatch", lambda: False)
    monkeypatch.setattr(event_stream.subprocess, "Popen", unexpected_popen)
    monkeypatch.setattr(
        event_stream,
        "time",
        SimpleNamespace(monotonic=lambda: clock[0], sleep=fake_sleep),
    )

    events = list(stream_events(lattice_dir, poll_interval=1, timeout=2))

    assert [event["id"] for event in events] == ["ev_1"]
    assert sleep_calls == [1.0, 1.0]
