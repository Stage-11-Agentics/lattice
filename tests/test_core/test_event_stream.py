"""Pure scanner coverage for local watch/wait event projections."""

from __future__ import annotations

import json
import shutil
from pathlib import Path

from lattice.core.event_stream import (
    _filtered_unique,
    _parse_jsonl_file,
    _scan_event_logs,
    _snapshot_event_offsets,
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
