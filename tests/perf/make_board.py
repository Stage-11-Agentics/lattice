"""Generate the perf board: 1,000 active tasks with long logs plus 300 archived tasks.

The board is built directly from core event helpers (the CLI would take tens of
minutes for ~70,000 events), with the same on-disk layout ``lattice create`` /
``comment`` / ``status`` / ``archive`` produce: per-task event log and snapshot,
``_lifecycle.jsonl``, ``ids.json``, and a plan file per task. Generation is
seeded, so every run builds the same board apart from the directory it lives in.

    uv run python -m tests.perf.make_board /tmp/perf-board   # then lattice doctor
"""

from __future__ import annotations

import json
import random
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

from ulid import ULID

from lattice.core.config import default_config, serialize_config
from lattice.core.events import create_event, serialize_event
from lattice.core.tasks import apply_event_to_snapshot, serialize_snapshot
from lattice.storage.fs import atomic_write, ensure_lattice_dirs

PROJECT_CODE = "PERF"
ACTIVE_TASKS = 1000
ARCHIVED_TASKS = 300
COMMENTS_PER_TASK = 40
BASE_TS = datetime(2026, 1, 1, tzinfo=UTC)
ACTORS = ("human:atin", "agent:claude", "agent:codex", "agent:reviewer")
STATUS_WALK = ("in_planning", "planned", "in_progress", "review")
BODY = (
    "Progress note: checked the reducer path, the snapshot rewrite, and the "
    "lock ordering; nothing surprising, moving to the next step. "
)


class _Ids:
    """Deterministic, time-ordered ULIDs from a seeded generator."""

    def __init__(self, rng: random.Random) -> None:
        self._rng = rng
        self._ms = int(BASE_TS.timestamp() * 1000)

    def __call__(self, prefix: str) -> str:
        self._ms += 1
        raw = self._ms.to_bytes(6, "big") + self._rng.randbytes(10)
        return f"{prefix}_{ULID.from_bytes(raw)}"


def _ts(minutes: int) -> str:
    return (BASE_TS + timedelta(minutes=minutes)).strftime("%Y-%m-%dT%H:%M:%SZ")


def _task_events(
    task_id: str, short_id: str, n: int, ids: _Ids, rng: random.Random, clock: list[int]
) -> list[dict]:
    def ev(type_: str, actor: str, data: dict) -> dict:
        clock[0] += 1
        return create_event(type_, task_id, actor, data, event_id=ids("ev"), ts=_ts(clock[0]))

    creator = rng.choice(ACTORS)
    events = [
        ev(
            "task_created",
            creator,
            {
                "title": f"Perf task {n}: exercise the read and write paths",
                "status": "backlog",
                "priority": rng.choice(("critical", "high", "medium", "low")),
                "type": rng.choice(("task", "bug", "chore")),
                "short_id": short_id,
                "description": "Generated for the local latency baseline.",
            },
        ),
        ev("assignment_changed", creator, {"from": None, "to": rng.choice(ACTORS)}),
    ]
    walk = STATUS_WALK[: rng.randint(0, len(STATUS_WALK))]
    status = "backlog"
    for i in range(COMMENTS_PER_TASK):
        events.append(ev("comment_added", rng.choice(ACTORS), {"body": f"{BODY}({i})"}))
        if walk and i % 10 == 9:
            nxt, walk = walk[0], walk[1:]
            events.append(ev("status_changed", rng.choice(ACTORS), {"from": status, "to": nxt}))
            status = nxt
    return events


def build_board(root: Path, *, seed: int = 11) -> Path:
    """Write the perf board under *root*; return the project root."""
    rng = random.Random(seed)
    ids = _Ids(rng)
    ensure_lattice_dirs(root)
    ld = root / ".lattice"
    config = default_config()
    config.update(
        {
            "project_code": PROJECT_CODE,
            "default_actor": "human:atin",
            "auto_code_review_on_transition": False,
            "auto_plan_review_on_transition": False,
        }
    )
    atomic_write(ld / "config.json", serialize_config(config))

    lifecycle: list[str] = []
    id_map: dict[str, str] = {}
    clock = [0]
    total = ACTIVE_TASKS + ARCHIVED_TASKS
    for n in range(1, total + 1):
        task_id = ids("task")
        short_id = f"{PROJECT_CODE}-{n}"
        id_map[short_id] = task_id
        events = _task_events(task_id, short_id, n, ids, rng, clock)
        archived = n > ACTIVE_TASKS
        if archived:
            clock[0] += 1
            events.append(
                create_event(
                    "task_archived",
                    task_id,
                    "human:atin",
                    {},
                    event_id=ids("ev"),
                    ts=_ts(clock[0]),
                )
            )
        snapshot = None
        for event in events:
            snapshot = apply_event_to_snapshot(snapshot, event)
        base = ld / "archive" if archived else ld
        (base / "events").mkdir(parents=True, exist_ok=True)
        (base / "tasks").mkdir(parents=True, exist_ok=True)
        (base / "plans").mkdir(parents=True, exist_ok=True)
        (base / "events" / f"{task_id}.jsonl").write_text(
            "".join(serialize_event(e) for e in events)
        )
        (base / "tasks" / f"{task_id}.json").write_text(serialize_snapshot(snapshot))
        (base / "plans" / f"{task_id}.md").write_text(f"# {short_id}\n\nGenerated plan.\n")
        lifecycle.append(serialize_event(events[0]))
        if archived:
            lifecycle.append(serialize_event(events[-1]))

    (ld / "events" / "_lifecycle.jsonl").write_text("".join(lifecycle))
    ids_doc = {"map": id_map, "next_seqs": {PROJECT_CODE: total + 1}, "schema_version": 2}
    atomic_write(ld / "ids.json", json.dumps(ids_doc, sort_keys=True, indent=2) + "\n")
    return root


if __name__ == "__main__":
    target = Path(sys.argv[1]) if len(sys.argv) > 1 else Path("perf-board")
    build_board(target)
    print(f"built {ACTIVE_TASKS} active + {ARCHIVED_TASKS} archived tasks under {target}")
