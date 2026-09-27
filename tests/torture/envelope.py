"""Boards at SPEC §8.8's supported size, written straight to disk for the stub.

The client only sees files and their hashes, so the torture tests build the
envelope quickly from synthetic task files instead of 2,000 real operations.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

from ulid import ULID

MIB = 1024 * 1024


def task_id() -> str:
    return f"task_{ULID()}"


def event_line(task: str, n: int, pad: int = 0) -> bytes:
    event = {
        "id": f"ev_{ULID()}",
        "task_id": task,
        "type": "comment_added",
        "data": {"body": f"comment {n} " + "x" * pad},
    }
    return (json.dumps(event, sort_keys=True, separators=(",", ":")) + "\n").encode()


def build_envelope(
    lattice_dir: Path,
    *,
    tasks: int = 2000,
    hot_log_bytes: int = 4 * MIB,
    total_bytes: int | None = None,
) -> str:
    """Write *tasks* tasks (snapshot, log, plan) under *lattice_dir*, one of them
    with a log of *hot_log_bytes*, padded with artifact payloads to reach
    *total_bytes* of durable data. Returns the hot task's ID."""
    for rel in ("tasks", "events", "plans", "artifacts/payload"):
        (lattice_dir / rel).mkdir(parents=True, exist_ok=True)
    written = 0
    hot = ""
    for n in range(tasks):
        task = task_id()
        hot = hot or task
        snapshot = json.dumps({"id": task, "title": f"Task {n}", "status": "backlog"}) + "\n"
        (lattice_dir / "tasks" / f"{task}.json").write_text(snapshot)
        log = event_line(task, 0, pad=200)
        (lattice_dir / "events" / f"{task}.jsonl").write_bytes(log)
        plan = f"# Task {n}\n\nPlan text.\n"
        (lattice_dir / "plans" / f"{task}.md").write_text(plan)
        written += len(snapshot) + len(log) + len(plan)
    hot_path = lattice_dir / "events" / f"{hot}.jsonl"
    with open(hot_path, "ab") as fh:
        n = 1
        while hot_path.stat().st_size < hot_log_bytes:
            line = event_line(hot, n, pad=1000)
            fh.write(line)
            fh.flush()
            written += len(line)
            n += 1
    if total_bytes:
        chunk = 4 * MIB
        index = 0
        while written < total_bytes:
            size = min(chunk, total_bytes - written)
            (lattice_dir / "artifacts" / "payload" / f"pad_{index:04d}.bin").write_bytes(
                os.urandom(size)
            )
            written += size
            index += 1
    return hot
