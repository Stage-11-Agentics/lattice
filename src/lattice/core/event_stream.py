"""Shared event streaming infrastructure for lattice watch/wait.

Provides a generator that yields events from .lattice/events/ as they are
written, using fswatch when available and falling back to polling.
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

    Returns (new_events, new_offset).  Each event dict has ``task_id``
    injected from the filename stem.
    """
    task_id = path.stem
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
        event["task_id"] = task_id
        events.append(event)

    return events, new_offset


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
    events_dir = lattice_dir / "events"
    has_fswatch = _check_fswatch()
    start_time = time.monotonic()

    if has_fswatch:
        yield from _stream_with_fswatch(
            events_dir,
            task_filter=task_filter,
            type_filter=type_filter,
            timeout=timeout,
            start_time=start_time,
        )
    else:
        yield from _stream_with_poll(
            events_dir,
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
    events_dir: Path,
    task_filter: list[str] | None,
    type_filter: list[str] | None,
    timeout: int,
    start_time: float,
) -> Iterator[dict]:
    """Stream events using fswatch for near-instant detection."""
    # Track byte offsets per file to avoid re-emitting on re-read
    offsets: dict[Path, int] = {}

    # Seed initial offsets so we don't replay history
    if events_dir.is_dir():
        for jsonl_path in events_dir.glob("*.jsonl"):
            try:
                offsets[jsonl_path] = jsonl_path.stat().st_size
            except OSError:
                offsets[jsonl_path] = 0

    cmd = [
        "fswatch",
        "-0",
        "--event",
        "Updated",
        "--event",
        "Created",
        str(events_dir),
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

                    if changed_path.suffix != ".jsonl":
                        continue

                    offset = offsets.get(changed_path, 0)
                    new_events, new_offset = _parse_jsonl_file(changed_path, offset)
                    offsets[changed_path] = new_offset

                    for event in new_events:
                        if _matches_filters(event, task_filter, type_filter):
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
    events_dir: Path,
    task_filter: list[str] | None,
    type_filter: list[str] | None,
    poll_interval: int,
    timeout: int,
    start_time: float,
) -> Iterator[dict]:
    """Stream events by polling byte offsets at regular intervals."""
    offsets: dict[Path, int] = {}

    # Seed offsets to skip existing history
    if events_dir.is_dir():
        for jsonl_path in events_dir.glob("*.jsonl"):
            try:
                offsets[jsonl_path] = jsonl_path.stat().st_size
            except OSError:
                offsets[jsonl_path] = 0

    while True:
        elapsed = time.monotonic() - start_time
        if timeout > 0 and elapsed >= timeout:
            return

        if events_dir.is_dir():
            for jsonl_path in events_dir.glob("*.jsonl"):
                offset = offsets.get(jsonl_path, 0)
                new_events, new_offset = _parse_jsonl_file(jsonl_path, offset)
                offsets[jsonl_path] = new_offset

                for event in new_events:
                    if _matches_filters(event, task_filter, type_filter):
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
