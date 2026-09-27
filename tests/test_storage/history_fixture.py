"""A board carrying v1's history damage (SPEC §11; EVALUATION's LAT-347 row).

``DamagedBoard`` writes task logs by hand, the way v1's concurrent writers left
them, and keeps each task's snapshot file as v1 showed it: every event applied
as recorded, then any field a racing writer's snapshot overwrote. It never runs
Lattice's writers, so nothing here depends on the code under test beyond the
reducer that stands in for v1's.

``build_fixture`` builds the EVALUATION board:

- stale ``from`` values on ``status_changed`` (a move to ``done`` and a backward
  move), ``assignment_changed``, and ``field_updated`` (the list field ``tags``
  and the key ``custom_fields.k``);
- a short ID held by three tasks three times: ``LAT-2`` mapped by ``ids.json``
  (to a later holder), ``LAT-3`` unmapped, ``LAT-4`` with a tombstoned holder
  that ``ids.json`` maps;
- an out-of-prefix ID (``OLD-7``) and an out-of-prefix ID that is also
  duplicated (``OLD-9``);
- stale history on an archived task and on an erased task.
"""

from __future__ import annotations

import json
from pathlib import Path

from lattice.core.config import default_config, serialize_config
from lattice.core.events import create_event, serialize_event
from lattice.core.ids import generate_task_id
from lattice.core.tasks import apply_event_to_snapshot, serialize_snapshot
from lattice.storage.fs import LATTICE_DIR, ensure_lattice_dirs

V1 = "human:v1"


class DamagedBoard:
    """Hand-written task logs and v1-style snapshots on a fresh board."""

    def __init__(self, root: Path, prefix: str = "LAT") -> None:
        self.root = root
        ensure_lattice_dirs(root)
        self.lattice = root / LATTICE_DIR
        cfg = default_config()
        cfg["project_code"] = prefix
        cfg["auto_code_review_on_transition"] = False
        cfg["auto_plan_review_on_transition"] = False
        (self.lattice / "config.json").write_text(serialize_config(cfg))
        (self.lattice / "events" / "_lifecycle.jsonl").write_text("")
        self.clock = 0
        self.archived: set[str] = set()
        self.ids_map: dict[str, str] = {}
        self.next_seq = 1

    # -- paths ----------------------------------------------------------

    def base(self, task_id: str) -> Path:
        return self.lattice / "archive" if task_id in self.archived else self.lattice

    def log(self, task_id: str) -> Path:
        return self.base(task_id) / "events" / f"{task_id}.jsonl"

    def snapshot_path(self, task_id: str) -> Path:
        return self.base(task_id) / "tasks" / f"{task_id}.json"

    def snapshot(self, task_id: str) -> dict:
        return json.loads(self.snapshot_path(task_id).read_text())

    # -- writing --------------------------------------------------------

    def event(self, etype: str, task_id: str, data: dict, actor: str = V1) -> dict:
        self.clock += 1
        ts = f"2025-01-01T00:{self.clock // 60:02d}:{self.clock % 60:02d}Z"
        return create_event(etype, task_id, actor, data, ts=ts)

    def task(self, title: str, short_id: str | None, *, status: str = "backlog") -> str:
        task_id = generate_task_id()
        data = {"title": title, "status": status, "type": "task", "priority": "medium"}
        if short_id is not None:
            data["short_id"] = short_id
        created = self.event("task_created", task_id, data)
        self.log(task_id).write_text(serialize_event(created))
        self._show(task_id, apply_event_to_snapshot(None, created))
        (self.lattice / "plans" / f"{task_id}.md").write_text(f"# {title}\n")
        with (self.lattice / "events" / "_lifecycle.jsonl").open("a") as fh:
            fh.write(serialize_event(created))
        return task_id

    def append(self, task_id: str, etype: str, data: dict) -> dict:
        """Append as a v1 writer did: no ``from`` check; the snapshot follows."""
        event = self.event(etype, task_id, data)
        with self.log(task_id).open("a") as fh:
            fh.write(serialize_event(event))
        snap = apply_event_to_snapshot(self.snapshot(task_id), event, accept_stale_from=True)
        self._show(task_id, snap)
        return event

    def show(self, task_id: str, **fields: object) -> None:
        """Overwrite snapshot fields the way a racing writer's snapshot did."""
        snap = self.snapshot(task_id)
        for name, value in fields.items():
            if name.startswith("custom__"):
                key = name[len("custom__") :]
                if value is _ABSENT:
                    snap.setdefault("custom_fields", {}).pop(key, None)
                else:
                    snap.setdefault("custom_fields", {})[key] = value
            else:
                snap[name] = value
        self._show(task_id, snap)

    def _show(self, task_id: str, snap: dict) -> None:
        self.snapshot_path(task_id).write_text(serialize_snapshot(snap))

    def archive(self, task_id: str) -> None:
        event = self.append(task_id, "task_archived", {})
        with (self.lattice / "events" / "_lifecycle.jsonl").open("a") as fh:
            fh.write(serialize_event(event))
        for kind, suffix in (("events", "jsonl"), ("tasks", "json"), ("plans", "md")):
            source = self.lattice / kind / f"{task_id}.{suffix}"
            source.rename(self.lattice / "archive" / kind / f"{task_id}.{suffix}")
        self.archived.add(task_id)

    def erase(self, task_id: str) -> None:
        self.append(task_id, "task_tombstoned", {"reason": "fixture"})

    def write_ids(self) -> None:
        index = {"schema_version": 2, "next_seqs": {"LAT": self.next_seq}, "map": self.ids_map}
        (self.lattice / "ids.json").write_text(json.dumps(index, sort_keys=True, indent=2) + "\n")

    # -- reading --------------------------------------------------------

    def logs(self) -> dict[str, bytes]:
        return {
            str(path.relative_to(self.lattice)): path.read_bytes()
            for path in sorted(self.lattice.glob("**/task_*.jsonl"))
        }


class _Absent:
    def __repr__(self) -> str:
        return "ABSENT"


_ABSENT = _Absent()
ABSENT = _ABSENT


def build_fixture(root: Path) -> tuple[DamagedBoard, dict[str, str]]:
    """The EVALUATION board. Returns the board and task IDs by role."""
    b = DamagedBoard(root)
    t: dict[str, str] = {}

    # Stale ``from`` values.
    t["done"] = b.task("Move to done", "LAT-1")
    b.append(t["done"], "status_changed", {"from": "backlog", "to": "in_planning"})
    b.append(t["done"], "status_changed", {"from": "backlog", "to": "done"})  # stale

    t["m1"] = b.task("Mapped dup, earliest", "LAT-2")
    t["m2"] = b.task("Mapped dup, mapped", "LAT-2")
    t["m3"] = b.task("Mapped dup, latest", "LAT-2")
    t["u1"] = b.task("Unmapped dup, earliest", "LAT-3")
    t["u2"] = b.task("Unmapped dup, middle", "LAT-3")
    t["u3"] = b.task("Unmapped dup, latest", "LAT-3")
    t["t1"] = b.task("Tombstoned dup, earliest", "LAT-4")
    t["t2"] = b.task("Tombstoned dup, middle", "LAT-4")
    t["t3"] = b.task("Tombstoned dup, latest", "LAT-4")
    b.erase(t["t1"])

    t["back"] = b.task("Backward move", "LAT-5")
    b.append(t["back"], "status_changed", {"from": "backlog", "to": "in_planning"})
    b.append(t["back"], "status_changed", {"from": "in_planning", "to": "planned"})
    b.append(t["back"], "status_changed", {"from": "in_planning", "to": "backlog"})  # stale
    b.show(t["back"], status="planned")

    t["assign"] = b.task("Stale assignment", "LAT-6")
    b.append(t["assign"], "assignment_changed", {"from": "agent:x", "to": "agent:y"})  # stale
    b.show(t["assign"], assigned_to="agent:z")

    t["fields"] = b.task("Stale fields", "LAT-7")
    b.append(t["fields"], "field_updated", {"field": "tags", "from": ["a"], "to": ["b"]})
    b.append(
        t["fields"], "field_updated", {"field": "custom_fields.k", "from": "old", "to": "new"}
    )
    b.show(t["fields"], tags=["b", "c"], custom__k=ABSENT)

    t["archived"] = b.task("Archived stale", "LAT-8")
    b.append(t["archived"], "status_changed", {"from": "backlog", "to": "in_planning"})
    b.archive(t["archived"])
    b.append(t["archived"], "status_changed", {"from": "backlog", "to": "planned"})  # stale
    b.show(t["archived"], status="in_planning")

    t["erased"] = b.task("Erased stale", "LAT-9")
    b.append(t["erased"], "status_changed", {"from": "planned", "to": "review"})  # stale
    b.show(t["erased"], status="blocked")
    b.erase(t["erased"])

    t["old7"] = b.task("Out of prefix", "OLD-7")
    t["old9a"] = b.task("Out of prefix dup A", "OLD-9")
    t["old9b"] = b.task("Out of prefix dup B", "OLD-9")

    b.ids_map = {
        "LAT-1": t["done"],
        "LAT-2": t["m2"],
        "LAT-4": t["t1"],
        "LAT-5": t["back"],
        "LAT-6": t["assign"],
        "LAT-7": t["fields"],
        "LAT-8": t["archived"],
        "LAT-9": t["erased"],
    }
    b.next_seq = 10
    b.write_ids()
    return b, t
