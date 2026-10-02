"""Shared event streaming infrastructure for lattice watch/wait.

Provides a generator that yields events from active, archived, and lifecycle
logs as they are written, using fswatch when available and falling back to
polling.
"""

from __future__ import annotations

import json
import subprocess
import time
from pathlib import Path
from typing import Iterator


def _check_fswatch() -> bool:
    """Return True if fswatch is installed."""
    try:
        subprocess.run(
            ["fswatch", "--version"],
            capture_output=True,
            timeout=5,
        )
        return True
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return False


def _parse_jsonl_file(
    path: Path,
    byte_offset: int,
) -> tuple[list[dict], int]:
    """Read new events from a JSONL file starting at *byte_offset*.

    Returns (new_events, new_offset). Serialized task IDs are preserved; a
    file-stem fallback is applied only to older records without ``task_id``.
    """
    fallback_task_id = path.stem
    events: list[dict] = []
    try:
        with path.open("rb") as fh:
            fh.seek(byte_offset)
            chunk = fh.read()
            new_offset = byte_offset + len(chunk)
    except OSError:
        return events, byte_offset

    for line in chunk.decode(errors="replace").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(event.get("task_id"), str) or not event["task_id"]:
            event["task_id"] = fallback_task_id
        events.append(event)

    return events, new_offset


def _event_log_paths(lattice_dir: Path) -> list[Path]:
    """Return active and archived JSONL logs in stable order."""
    paths: list[Path] = []
    for directory in (lattice_dir / "events", lattice_dir / "archive" / "events"):
        if directory.is_dir():
            paths.extend(sorted(directory.glob("*.jsonl")))
    return paths


def _logical_log_key(path: Path) -> str:
    """Key a per-task log by task across an active/archive move."""
    return path.stem


def _selected_event_logs(lattice_dir: Path, last_paths: dict[str, Path]) -> list[Path]:
    """Choose one current copy per task, preferring the previous path on overlap."""
    grouped: dict[str, list[Path]] = {}
    for path in _event_log_paths(lattice_dir):
        grouped.setdefault(_logical_log_key(path), []).append(path)
    selected: list[Path] = []
    for key, paths in sorted(grouped.items()):
        previous = last_paths.get(key)
        if previous in paths:
            selected.append(previous)
            continue
        active = next((path for path in paths if path.parent == lattice_dir / "events"), None)
        selected.append(active or paths[0])
    return selected


def _snapshot_event_offsets(
    lattice_dir: Path,
) -> tuple[dict[str, int], dict[str, Path]]:
    """Seed a live-only stream across both task-log locations."""
    last_paths: dict[str, Path] = {}
    selected = _selected_event_logs(lattice_dir, last_paths)
    offsets: dict[str, int] = {}
    for path in selected:
        key = _logical_log_key(path)
        try:
            offsets[key] = path.stat().st_size
        except OSError:
            offsets[key] = 0
        last_paths[key] = path
    return offsets, last_paths


def _scan_event_logs(
    lattice_dir: Path,
    offsets: dict[str, int],
    last_paths: dict[str, Path],
) -> list[dict]:
    """Read appended bytes, carrying each task offset across archive moves."""
    batch: list[dict] = []
    for path in _selected_event_logs(lattice_dir, last_paths):
        key = _logical_log_key(path)
        try:
            size = path.stat().st_size
        except OSError:
            continue
        offset = offsets.get(key, 0)
        if size < offset:
            # A reset replaced this logical log. Treat its current bytes as
            # history; the next append starts at this new boundary.
            offsets[key] = size
            last_paths[key] = path
            continue
        if size == offset:
            last_paths[key] = path
            continue
        new_events, offsets[key] = _parse_jsonl_file(path, offset)
        last_paths[key] = path
        batch.extend(new_events)
    batch.sort(key=lambda event: str(event.get("ts", "")))
    return batch


def _filtered_unique(
    events: list[dict],
    task_filter: list[str] | None,
    type_filter: list[str] | None,
    seen_event_ids: set[str],
) -> Iterator[dict]:
    """Apply filters and suppress mirrored event records by serialized ID."""
    for event in events:
        if not _matches_filters(event, task_filter, type_filter):
            continue
        event_id = event.get("id")
        if isinstance(event_id, str) and event_id:
            if event_id in seen_event_ids:
                continue
            seen_event_ids.add(event_id)
        yield event


def stream_events(
    lattice_dir: Path,
    task_filter: list[str] | None = None,
    type_filter: list[str] | None = None,
    poll_interval: int = 5,
    timeout: int = 0,
) -> Iterator[dict]:
    """Yield events as they are written to .lattice/events/.

    Uses fswatch when available, falls back to polling.  Each yielded
    dict is a parsed event with ``task_id`` (ULID) added.

    Parameters
    ----------
    lattice_dir:
        Path to the .lattice/ directory.
    task_filter:
        If provided, only yield events for these task IDs (ULIDs).
    type_filter:
        If provided, only yield events with these event types.
    poll_interval:
        Seconds between polls when fswatch is unavailable.
    timeout:
        Stop after this many seconds (0 = never).
    """
    lattice_dir = Path(lattice_dir).resolve()
    has_fswatch = _check_fswatch()
    start_time = time.monotonic()

    if has_fswatch:
        yield from _stream_with_fswatch(
            lattice_dir,
            task_filter=task_filter,
            type_filter=type_filter,
            timeout=timeout,
            start_time=start_time,
        )
    else:
        yield from _stream_with_poll(
            lattice_dir,
            task_filter=task_filter,
            type_filter=type_filter,
            poll_interval=poll_interval,
            timeout=timeout,
            start_time=start_time,
        )


def _matches_filters(
    event: dict,
    task_filter: list[str] | None,
    type_filter: list[str] | None,
) -> bool:
    """Return True if the event passes all active filters."""
    if task_filter is not None and event.get("task_id") not in task_filter:
        return False
    if type_filter is not None and event.get("type") not in type_filter:
        return False
    return True


def _stream_with_fswatch(
    lattice_dir: Path,
    task_filter: list[str] | None,
    type_filter: list[str] | None,
    timeout: int,
    start_time: float,
) -> Iterator[dict]:
    """Stream events using fswatch for near-instant detection."""
    events_dir = lattice_dir / "events"
    archive_events_dir = lattice_dir / "archive" / "events"
    offsets, last_paths = _snapshot_event_offsets(lattice_dir)
    seen_event_ids: set[str] = set()

    cmd = [
        "fswatch",
        "-0",
        "-r",
        "--event",
        "Updated",
        "--event",
        "Created",
        # Watch the stable board root so an archive/events directory created
        # after this stream starts is still observed.
        str(lattice_dir),
    ]
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE)

    try:
        buffer = b""
        while True:
            elapsed = time.monotonic() - start_time
            if timeout > 0 and elapsed >= timeout:
                return

            read_timeout = min(5.0, (timeout - elapsed) if timeout > 0 else 5.0)
            try:
                import select

                ready, _, _ = select.select([proc.stdout], [], [], read_timeout)
                if not ready:
                    continue

                chunk = proc.stdout.read(4096)
                if not chunk:
                    return

                buffer += chunk

                while b"\0" in buffer:
                    path_bytes, buffer = buffer.split(b"\0", 1)
                    changed_path = Path(path_bytes.decode().strip())

                    if changed_path.suffix != ".jsonl" or changed_path.parent not in {
                        events_dir,
                        archive_events_dir,
                    }:
                        continue
                    for event in _filtered_unique(
                        _scan_event_logs(lattice_dir, offsets, last_paths),
                        task_filter,
                        type_filter,
                        seen_event_ids,
                    ):
                        yield event

            except (OSError, ValueError):
                continue

    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()


def _stream_with_poll(
    lattice_dir: Path,
    task_filter: list[str] | None,
    type_filter: list[str] | None,
    poll_interval: int,
    timeout: int,
    start_time: float,
) -> Iterator[dict]:
    """Stream events by polling byte offsets at regular intervals."""
    offsets, last_paths = _snapshot_event_offsets(lattice_dir)
    seen_event_ids: set[str] = set()

    while True:
        elapsed = time.monotonic() - start_time
        if timeout > 0 and elapsed >= timeout:
            return

        for event in _filtered_unique(
            _scan_event_logs(lattice_dir, offsets, last_paths),
            task_filter,
            type_filter,
            seen_event_ids,
        ):
            yield event

        sleep_for = min(
            float(poll_interval),
            float(timeout - (time.monotonic() - start_time))
            if timeout > 0
            else float(poll_interval),
        )
        if sleep_for <= 0:
            return
        time.sleep(sleep_for)
