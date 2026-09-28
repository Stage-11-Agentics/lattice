"""History repair by appending events: ``lattice doctor --fix --actor`` (SPEC §11).

Boards written by v1's concurrent writers carry three kinds of history damage
that strict doctor refuses and import does not repair:

1. **Stale ``from`` values.** One ``task_history_reconciled``
   ``{event_ids, reason}`` per task names every event whose recorded ``from``
   disagrees with the replayed state; replay then applies exactly those as
   recorded (``storage.operations._parse_authoritative_log``). For each business
   field a named event touched, an ordinary event restores the value the
   task's existing snapshot file shows, with ``from`` equal to the reconciled
   value. A ``custom_fields`` key the snapshot lacks is restored to ``null``,
   the closest state an event can reach (``field_updated`` only sets keys),
   and so is a top-level field the snapshot lacks. A snapshot file that is
   missing, not JSON, not UTF-8, or not an object is no readable snapshot:
   the task is reconciled, nothing is restored, and the rebuild replaces it.
2. **Short IDs outside the configured prefix**, reassigned first.
3. **Duplicate short IDs**, then: the holder ``ids.json`` maps the ID to keeps
   it if it is a current, non-tombstoned holder; otherwise the non-tombstoned
   holder with the earliest issuing event (ties by event ID), or the earliest
   holder when all are tombstoned. Every other holder moves.

A reassignment is ``task_short_id_assigned {short_id, supersedes}`` with the
next free in-prefix ID above the log floor (SPEC §5).

Everything runs under one lock epoch (every task lock, the lifecycle log, and
``ids.json``): plan every task, build each planned log in memory, and run the
checks doctor and the derived rebuild run against those planned authorities.
Any error outside the three kinds, on any task, refuses the run with zero
appends. Only then does each task get one append, at the log its authority
replayed (never another path): restoring events, the reassignment, and the
reconciliation last, so a torn tail loses the reconciliation first and a
re-run plans the task again. Complete events are never rewritten, reordered,
removed, or moved between active and archive. The derived files (snapshots,
``ids.json``, ``events/_lifecycle.jsonl``) are rebuilt last, as import
rebuilds them. No board hooks run.

Erased tasks take the reconciliation and reassignment only: the tombstone
stays and nothing is restored, since an erased task is not shown.
"""

from __future__ import annotations

import copy
import json
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path

from lattice.core.config import configured_event_prefix
from lattice.core.events import create_event, serialize_event
from lattice.core.origin import origin_scope
from lattice.core.tasks import HISTORY_RECONCILED, apply_event_to_snapshot
from lattice.storage.integrity import (
    DoctorReport,
    _build_rebuilt_id_index,
    _collect_task_ids,
    _historical_short_id_duplicates,
    _lifecycle_events,
    _repair_task_derived_files_unlocked,
    _require_valid_short_ids,
    _task_file_findings,
    _task_paths,
)
from lattice.storage.locks import all_task_locks
from lattice.storage.operations import (
    AuthoritativeLogError,
    ResolvedTaskAuthority,
    _load_strict_id_index,
    append_repair_events,
    parse_project_short_id,
    resolve_task_authority,
)
from lattice.storage.short_ids import SHORT_ID_EVENT_TYPES, max_observed_short_ids, next_short_id

REASON = "v1 concurrent writers recorded a stale from value"

#: Doctor error tags (``finding["repair"]``) this repair resolves.
REPAIRABLE = frozenset({"stale_from", "prefix", "duplicate", "historical_duplicate"})

#: The actor planned events carry when none was given; they are never written.
_DRY_RUN_ACTOR = "human:doctor-dry-run"


@dataclass
class TaskRepair:
    """The events history repair appends to one task's log, in append order."""

    task_id: str
    title: str | None
    short_id: str | None
    path: Path
    events: list[dict] = field(default_factory=list)
    reconciled: list[str] = field(default_factory=list)
    restored: list[dict] = field(default_factory=list)
    reassigned: tuple[str, str] | None = None


@dataclass
class HistoryRepair:
    """One run's plan, its refusals, and whether it was appended."""

    repairs: list[TaskRepair]
    refused: list[str]
    applied: bool = False

    @property
    def counts(self) -> dict[str, int]:
        return {
            "events": sum(len(r.events) for r in self.repairs),
            "reconciliations": sum(1 for r in self.repairs if r.reconciled),
            "reassignments": sum(1 for r in self.repairs if r.reassigned),
            "restores": sum(len(r.restored) for r in self.repairs),
        }

    def to_json(self) -> dict:
        return {
            "applied": self.applied,
            "counts": self.counts,
            "events": [
                {
                    "task_id": r.task_id,
                    "short_id": r.short_id,
                    "title": r.title,
                    "type": event["type"],
                    "data": event["data"],
                    **({"id": event["id"]} if self.applied else {}),
                }
                for r in self.repairs
                for event in r.events
            ],
            "reassigned": [
                {
                    "task_id": r.task_id,
                    "title": r.title,
                    "old": r.reassigned[0],
                    "new": r.reassigned[1],
                }
                for r in self.repairs
                if r.reassigned
            ],
            "restored": [
                {"task_id": r.task_id, "short_id": r.short_id, "title": r.title, **restore}
                for r in self.repairs
                for restore in r.restored
            ],
            "refused": list(self.refused),
        }


def repair_history(
    lattice_dir: Path,
    report: DoctorReport,
    *,
    actor: str | dict | None,
    origin: dict | None = None,
) -> HistoryRepair | None:
    """Plan history repair and, with an *actor* and no refusal, append it.

    *report* is doctor's scan after ``--fix``'s existing repairs; its errors
    must all be of the three kinds. Returns ``None`` when there is nothing to
    repair. *origin* is stamped on every appended event.
    """
    with all_task_locks(lattice_dir / "locks", ["events__lifecycle", "ids_json"]):
        plan = _plan(lattice_dir, report, actor if actor is not None else _DRY_RUN_ACTOR)
        if plan is None or plan.refused or actor is None:
            return plan
        with origin_scope(origin or {"op": "doctor.fix"}):
            for repair in plan.repairs:
                append_repair_events(repair.path, repair.events)
        plan.applied = True
        _repair_task_derived_files_unlocked(lattice_dir, reconcile_placement=False)
        return plan


# ---------------------------------------------------------------------------
# Planning (the caller holds every lock)
# ---------------------------------------------------------------------------


def _plan(lattice_dir: Path, report: DoctorReport, actor: str | dict) -> HistoryRepair | None:
    refused: list[str] = []
    authorities: dict[str, ResolvedTaskAuthority] = {}
    for task_id in sorted(_collect_task_ids(lattice_dir)):
        try:
            authority = resolve_task_authority(
                lattice_dir, task_id, allow_missing=True, lenient=True
            )
        except AuthoritativeLogError as exc:
            refused.append(f"{task_id}: {exc}")
            continue
        if authority is not None:
            authorities[task_id] = authority

    repairs: dict[str, TaskRepair] = {}

    def repair_for(task_id: str) -> TaskRepair:
        authority = authorities[task_id]
        if task_id not in repairs:
            repairs[task_id] = TaskRepair(
                task_id=task_id,
                title=authority.snapshot.get("title"),
                short_id=authority.snapshot.get("short_id"),
                path=authority.event_path or authority.active_event_path,
            )
        return repairs[task_id]

    # 1. Stale ``from`` values: restoring events now, the reconciliation last.
    reconciliations: dict[str, dict] = {}
    for task_id, authority in authorities.items():
        if not authority.stale:
            continue
        repair = repair_for(task_id)
        repair.reconciled = list(authority.stale)
        try:
            _plan_restores(lattice_dir, authority, repair, actor)
        except (KeyError, TypeError, ValueError) as exc:
            refused.append(f"{task_id}: cannot restore the pre-repair snapshot: {exc}")
        reconciliations[task_id] = create_event(
            HISTORY_RECONCILED,
            task_id,
            actor,
            {"event_ids": list(authority.stale), "reason": REASON},
        )

    # 2 and 3. Short IDs: prefix, then duplicates.
    try:
        config = json.loads((lattice_dir / "config.json").read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        config = None
        refused.append(f"config.json: {exc}")
    prefix = configured_event_prefix(config) if isinstance(config, dict) else None
    if prefix is not None:
        _plan_short_ids(lattice_dir, authorities, prefix, actor, repair_for, refused)

    for task_id, event in reconciliations.items():
        repairs[task_id].events.append(event)

    if not repairs:
        return None
    plan = HistoryRepair(repairs=[repairs[t] for t in sorted(repairs)], refused=refused)
    _preflight(lattice_dir, report, plan, prefix)
    return plan


def _snapshot_file(lattice_dir: Path, authority: ResolvedTaskAuthority) -> dict | None:
    """The task's existing snapshot file, where authority places it, else at
    the other placement; ``None`` when neither is a readable JSON object."""
    archived = authority.location == "archived"
    for placement in (archived, not archived):
        path = _task_paths(lattice_dir, authority.task_id, placement)["snapshot"]
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if isinstance(data, dict):
            return data
    return None


def _touched_fields(authority: ResolvedTaskAuthority) -> list[str]:
    """The business fields the stale events set, in first-touch order."""
    stale = set(authority.stale)
    fields: list[str] = []
    for event in authority.events:
        if event["id"] not in stale:
            continue
        name = {"status_changed": "status", "assignment_changed": "assigned_to"}.get(
            event["type"], event["data"].get("field")
        )
        if name not in fields:
            fields.append(name)
    return fields


def _field_value(snapshot: dict, name: str) -> object:
    if name.startswith("custom_fields."):
        return (snapshot.get("custom_fields") or {}).get(name[len("custom_fields.") :])
    return snapshot.get(name)


def _plan_restores(
    lattice_dir: Path, authority: ResolvedTaskAuthority, repair: TaskRepair, actor: str | dict
) -> None:
    """Restore each touched field whose reconciled value differs from the
    existing snapshot file. Nothing for an erased task or with no readable
    snapshot file: the reconciled replay stands."""
    if authority.snapshot.get("tombstoned"):
        return
    shown = _snapshot_file(lattice_dir, authority)
    if shown is None:
        return
    working = copy.deepcopy(authority.snapshot)
    for name in _touched_fields(authority):
        reconciled, wanted = _field_value(working, name), _field_value(shown, name)
        if reconciled == wanted:
            continue
        if name == "status":
            event = create_event(
                "status_changed", authority.task_id, actor, {"from": reconciled, "to": wanted}
            )
        elif name == "assigned_to":
            event = create_event(
                "assignment_changed", authority.task_id, actor, {"from": reconciled, "to": wanted}
            )
        else:
            event = create_event(
                "field_updated",
                authority.task_id,
                actor,
                {"field": name, "from": reconciled, "to": wanted},
            )
        working = apply_event_to_snapshot(working, event)
        repair.events.append(event)
        repair.restored.append({"field": name, "from": reconciled, "to": wanted})


def _issuing_key(authority: ResolvedTaskAuthority, short_id: str) -> tuple[str, str]:
    """``(ts, event id)`` of the last event that issued *short_id* to the task."""
    key = ("", "")
    for event in authority.events:
        data = event.get("data")
        if (
            event.get("type") in SHORT_ID_EVENT_TYPES
            and isinstance(data, dict)
            and data.get("short_id") == short_id
        ):
            key = (str(event.get("ts", "")), str(event["id"]))
    return key


def _plan_short_ids(
    lattice_dir: Path,
    authorities: dict[str, ResolvedTaskAuthority],
    prefix: str,
    actor: str | dict,
    repair_for,  # noqa: ANN001
    refused: list[str],
) -> None:
    out_of_prefix: list[str] = []
    holders: dict[str, list[str]] = {}
    for task_id, authority in authorities.items():
        short_id = authority.snapshot.get("short_id")
        if short_id is None:
            refused.append(
                f"task {task_id} has no short ID under project code {prefix!r}; "
                "run lattice backfill-ids"
            )
            continue
        if not isinstance(short_id, str):
            refused.append(f"task {task_id} has malformed short ID {short_id!r}")
            continue
        try:
            parse_project_short_id(short_id, prefix)
        except AuthoritativeLogError:
            out_of_prefix.append(task_id)
            continue
        holders.setdefault(short_id, []).append(task_id)

    try:
        index = _load_strict_id_index(lattice_dir)
    except AuthoritativeLogError as exc:
        if out_of_prefix or any(len(tasks) > 1 for tasks in holders.values()):
            refused.append(f"ids.json: {exc}")
        return
    floor: Mapping[str, int] = max_observed_short_ids(lattice_dir)
    mapping = dict(index["map"])

    def erased(task_id: str) -> bool:
        return bool(authorities[task_id].snapshot.get("tombstoned"))

    def by_issue(task_id: str) -> tuple[str, str]:
        return _issuing_key(authorities[task_id], authorities[task_id].snapshot["short_id"])

    moving = sorted(out_of_prefix, key=by_issue)
    for short_id in sorted(holders):
        group = holders[short_id]
        if len(group) < 2:
            continue
        mapped = mapping.get(short_id)
        if mapped in group and not erased(mapped):
            keeper = mapped
        else:
            pool = [t for t in group if not erased(t)] or group
            keeper = min(pool, key=by_issue)
        moving.extend(sorted((t for t in group if t != keeper), key=by_issue))

    # Prefix repair first, then duplicates in ID order (SPEC §11).
    for task_id in moving:
        old = authorities[task_id].snapshot["short_id"]
        new = next_short_id(index, prefix, task_id, floor)
        repair = repair_for(task_id)
        repair.events.append(
            create_event(
                "task_short_id_assigned", task_id, actor, {"short_id": new, "supersedes": old}
            )
        )
        repair.reassigned = (old, new)


# ---------------------------------------------------------------------------
# Preflight: every check against the planned authorities, before any append
# ---------------------------------------------------------------------------


def _preflight(
    lattice_dir: Path, report: DoctorReport, plan: HistoryRepair, prefix: str | None
) -> None:
    refused = plan.refused
    for finding in report.findings:
        if finding["level"] == "error" and finding.get("repair") not in REPAIRABLE:
            refused.append(finding["message"])
    if refused:
        return

    override: dict[Path, bytes] = {}
    for repair in plan.repairs:
        current = repair.path.read_bytes() if repair.path.exists() else b""
        override[repair.path] = current + "".join(
            serialize_event(event) for event in repair.events
        ).encode("utf-8")

    planned: dict[str, ResolvedTaskAuthority] = {}
    for task_id in sorted(_collect_task_ids(lattice_dir)):
        try:
            authority = resolve_task_authority(
                lattice_dir, task_id, allow_missing=True, override=override
            )
        except AuthoritativeLogError as exc:
            refused.append(f"{task_id}: after repair: {exc}")
            continue
        if authority is None:
            refused.append(f"{task_id}: no valid authoritative event log")
            continue
        planned[task_id] = authority
        refused.extend(
            f["message"]
            for f in _task_file_findings(lattice_dir, task_id, authority)
            if f["level"] == "error"
        )
    try:
        _lifecycle_events(planned)
    except AuthoritativeLogError as exc:
        refused.append(str(exc))
    # The derived rebuild's index checks, exactly as it runs them, so it
    # cannot fail once history has been appended.
    try:
        _build_rebuilt_id_index(
            _load_strict_id_index(lattice_dir),
            _require_valid_short_ids(planned, prefix),
            max_observed_short_ids(lattice_dir),
        )
    except (AuthoritativeLogError, ValueError) as exc:
        refused.append(f"after repair, the derived rebuild would fail: {exc}")
    if prefix is not None:
        refused.extend(
            message
            for level, message in _historical_short_id_duplicates(planned)
            if level == "error"
        )
