"""The optional issue log's files (LAT-361).

Everything lives in ``.lattice/issues/``, created by the first ``issue file``::

    issues/ids.json                   the issue-only sequence: next_seq and seq -> iss_ ID
    issues/events/iss_<ULID>.jsonl    one issue's event log (the authority)
    issues/iss_<ULID>.json            its snapshot, a full replay of that log

Nothing else in Lattice enumerates ``issues/``: task rebuilds, doctor, stats,
archive, the dashboard and the short-ID floor never see it. The readers here
call no writer, so ``show`` can use them.
"""

from __future__ import annotations

import contextlib
import json
from collections.abc import Callable, Generator, Iterable
from dataclasses import dataclass, field
from pathlib import Path

from lattice.core.errors import OpError
from lattice.core.issues import (
    TaskInfo,
    issue_view,
    parse_issue_ref,
    replay_issue,
    serialize_issue_snapshot,
)
from lattice.core.visibility import is_tombstoned
from lattice.storage.fs import atomic_write, ensure_dir
from lattice.storage.locks import lattice_lock
from lattice.storage.operations import (
    AuthoritativeLogError,
    read_task_authority,
    write_issue_events,
)

ISSUES_DIR = "issues"
IDS_LOCK = "issues_ids"

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------


def issues_dir(lattice_dir: Path) -> Path:
    return Path(lattice_dir) / ISSUES_DIR


def _snapshot_path(lattice_dir: Path, issue_id: str) -> Path:
    return issues_dir(lattice_dir) / f"{issue_id}.json"


def _events_path(lattice_dir: Path, issue_id: str) -> Path:
    return issues_dir(lattice_dir) / "events" / f"{issue_id}.jsonl"


# ``write_issue_events`` lives in ``storage/operations.py`` beside
# ``write_resource_event``: it is the one appender of an issue log.


def _ids_path(lattice_dir: Path) -> Path:
    return issues_dir(lattice_dir) / "ids.json"


# ---------------------------------------------------------------------------
# Readers
# ---------------------------------------------------------------------------


def _load_json(path: Path) -> dict:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise OpError("INTEGRITY_ERROR", f"Cannot read {path}: {exc}.") from exc


#: Called with the unreadable file and the error when a list skips an issue.
OnUnreadable = Callable[[Path, OpError], None]


def _replayed(lattice_dir: Path, issue_id: str) -> dict | None:
    """The issue built in memory from its log, or ``None`` with no log. Writes nothing."""
    path = _events_path(lattice_dir, issue_id)
    if not path.exists():
        return None
    try:
        return replay_issue(read_issue_events(lattice_dir, issue_id))
    except (ValueError, KeyError) as exc:
        raise OpError("INTEGRITY_ERROR", f"Cannot replay {path}: {exc}.") from exc


def read_issue_snapshot(lattice_dir: Path, issue_id: str) -> dict | None:
    """The issue's snapshot; replayed from its log when the file is missing or
    unreadable (a crash between the event and the snapshot, a merge conflict).

    ``None`` when there is neither. An unreadable snapshot with no log raises
    ``INTEGRITY_ERROR``.
    """
    path = _snapshot_path(lattice_dir, issue_id)
    if path.exists():
        try:
            return _load_json(path)
        except OpError:
            replayed = _replayed(lattice_dir, issue_id)
            if replayed is None:
                raise
            return replayed
    return _replayed(lattice_dir, issue_id)


def list_issue_snapshots(
    lattice_dir: Path, *, on_unreadable: OnUnreadable | None = None
) -> list[dict]:
    """Every issue, by sequence number.

    An issue whose log has no snapshot file (a crash after the first event) is
    replayed in memory; only the names are compared, so the other logs are not
    read. With *on_unreadable*, a file that cannot be read is skipped and
    reported to it; without, the ``INTEGRITY_ERROR`` propagates.
    """
    directory = issues_dir(lattice_dir)
    if not directory.is_dir():
        return []
    snapshot_paths = sorted(directory.glob("iss_*.json"))
    have = {p.stem for p in snapshot_paths}
    events_dir = directory / "events"
    missing = (
        sorted(p.stem for p in events_dir.glob("iss_*.jsonl") if p.stem not in have)
        if events_dir.is_dir()
        else []
    )
    snapshots: list[dict] = []
    for path, load in [
        *((p, lambda p=p: _load_json(p)) for p in snapshot_paths),
        *((_events_path(lattice_dir, i), lambda i=i: _replayed(lattice_dir, i)) for i in missing),
    ]:
        try:
            snapshot = load()
        except OpError as exc:
            if on_unreadable is None:
                raise
            on_unreadable(path, exc)
            continue
        if snapshot is not None:
            snapshots.append(snapshot)
    return sorted(snapshots, key=lambda s: (s.get("seq") or 0, s.get("id", "")))


def read_issue_events(lattice_dir: Path, issue_id: str) -> list[dict]:
    path = _events_path(lattice_dir, issue_id)
    if not path.exists():
        return []
    events = []
    for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip():
            continue
        try:
            events.append(json.loads(line))
        except json.JSONDecodeError as exc:
            raise OpError("INTEGRITY_ERROR", f"Cannot read {path} line {number}: {exc}.") from exc
    return events


def load_issue_ids(lattice_dir: Path) -> dict:
    """``issues/ids.json``, or an empty index when there is none yet."""
    path = _ids_path(lattice_dir)
    if not path.exists():
        return {"schema_version": 1, "next_seq": 1, "map": {}}
    data = _load_json(path)
    data.setdefault("map", {})
    data.setdefault("next_seq", 1)
    return data


def resolve_issue(lattice_dir: Path, raw: str) -> str:
    """The ``iss_`` ID *raw* names: ``iss_<ULID>``, ``I<n>`` or ``<CODE>-I<n>``.

    ``INVALID_ID`` when *raw* is none of those; ``NOT_FOUND`` when no issue
    has that ID, or the prefixed form differs from the issue's display ID.
    """
    parsed = parse_issue_ref(raw)
    if parsed is None:
        raise OpError(
            "INVALID_ID",
            f"Invalid issue ID format: '{raw}'. Expected an issue ID such as I3, "
            "<CODE>-I3, or iss_<ULID>.",
        )
    if parsed[0] == "ulid":
        issue_id = parsed[1]
        if read_issue_snapshot(lattice_dir, issue_id) is None and not (
            _events_path(lattice_dir, issue_id).exists()
        ):
            raise OpError("NOT_FOUND", f"Issue '{raw}' not found.")
        return issue_id
    _kind, prefix, seq = parsed
    issue_id = load_issue_ids(lattice_dir)["map"].get(str(seq))
    snapshot = read_issue_snapshot(lattice_dir, issue_id) if issue_id else None
    if snapshot is None:  # a stale index: look for the number itself
        readable = list_issue_snapshots(lattice_dir, on_unreadable=lambda _p, _e: None)
        snapshot = next((s for s in readable if s.get("seq") == seq), None)
    if snapshot is None:
        raise OpError("NOT_FOUND", f"Issue '{raw}' not found.")
    if prefix is not None and str(snapshot.get("short_id", "")).upper() != raw.strip().upper():
        raise OpError("NOT_FOUND", f"Issue '{raw}' not found.")
    return snapshot["id"]


def task_info_for(lattice_dir: Path, task_ids: Iterable[str]) -> dict[str, TaskInfo | None]:
    """Each task's current status, erased and archived flags; ``None`` when it is gone.

    One log replay per distinct task.
    """
    info: dict[str, TaskInfo | None] = {}
    for task_id in dict.fromkeys(task_ids):
        try:
            authority = read_task_authority(lattice_dir, task_id, allow_missing=True)
        except AuthoritativeLogError:
            authority = None
        if authority is None:
            info[task_id] = None
            continue
        snap = authority.snapshot
        info[task_id] = TaskInfo(
            status=snap.get("status"),
            erased=is_tombstoned(snap),
            archived=authority.location == "archived",
            short_id=snap.get("short_id"),
            title=snap.get("title"),
        )
    return info


def issue_views(lattice_dir: Path, snapshots: list[dict]) -> list[dict]:
    """The ``--json`` views of *snapshots*, each linked task read once."""
    task_ids = [link["task_id"] for s in snapshots for link in s.get("links", [])]
    info = task_info_for(lattice_dir, task_ids)
    return [issue_view(s, info) for s in snapshots]


def issues_linked_to(
    lattice_dir: Path, task_id: str, *, on_unreadable: OnUnreadable | None = None
) -> list[dict]:
    """The views of every issue linked to *task_id*, by sequence number."""
    linked = [
        s
        for s in list_issue_snapshots(lattice_dir, on_unreadable=on_unreadable)
        if any(link.get("task_id") == task_id for link in s.get("links", []))
    ]
    return issue_views(lattice_dir, linked)


# ---------------------------------------------------------------------------
# Writers (operations and rebuild only)
# ---------------------------------------------------------------------------


@contextlib.contextmanager
def issue_write_context(lattice_dir: Path, issue_id: str) -> Generator[None, None, None]:
    """Hold the issue's lock for a read, decide, write sequence."""
    locks_dir = Path(lattice_dir) / "locks"
    ensure_dir(locks_dir)
    with lattice_lock(locks_dir, f"issues_{issue_id}"):
        yield


def _serialize_ids(data: dict) -> str:
    return json.dumps(data, sort_keys=True, indent=2) + "\n"


def allocate_issue_seq(lattice_dir: Path, issue_id: str) -> int:
    """Reserve the next issue number for *issue_id* in ``issues/ids.json``.

    Never touches the task sequence in ``.lattice/ids.json``. A ``next_seq``
    behind the map (a hand edit, a merge) is skipped past.
    """
    locks_dir = Path(lattice_dir) / "locks"
    ensure_dir(locks_dir)
    ensure_dir(issues_dir(lattice_dir) / "events")
    with lattice_lock(locks_dir, IDS_LOCK):
        data = load_issue_ids(lattice_dir)
        used = [int(k) for k in data["map"] if str(k).isdigit()]
        seq = max(int(data.get("next_seq") or 1), 1 + max(used, default=0))
        data["map"][str(seq)] = issue_id
        data["next_seq"] = seq + 1
        data["schema_version"] = 1
        atomic_write(_ids_path(lattice_dir), _serialize_ids(data))
    return seq


def current_issue(lattice_dir: Path, issue_id: str) -> dict | None:
    """The issue replayed from its log (the snapshot file may be stale)."""
    return replay_issue(read_issue_events(lattice_dir, issue_id))


@dataclass
class IssueRebuild:
    """What :func:`rebuild_issue_snapshots` did."""

    rebuilt: list[str] = field(default_factory=list)
    #: Issue numbers filed twice (two clones of a board merged): the number
    #: maps to the lower ID; both issues stay readable by their ``iss_`` IDs.
    collisions: list[dict] = field(default_factory=list)


def rebuild_issue_snapshots(lattice_dir: Path) -> IssueRebuild:
    """Rewrite every issue snapshot and ``issues/ids.json`` from the issue logs."""
    result = IssueRebuild()
    events_dir = issues_dir(lattice_dir) / "events"
    if not events_dir.is_dir():
        return result
    by_seq: dict[int, list[str]] = {}
    for path in sorted(events_dir.glob("iss_*.jsonl")):
        issue_id = path.stem
        with issue_write_context(lattice_dir, issue_id):
            snapshot = current_issue(lattice_dir, issue_id)
            if snapshot is None:
                continue
            atomic_write(_snapshot_path(lattice_dir, issue_id), serialize_issue_snapshot(snapshot))
        result.rebuilt.append(issue_id)
        if isinstance(snapshot.get("seq"), int):
            by_seq.setdefault(snapshot["seq"], []).append(issue_id)

    locks_dir = Path(lattice_dir) / "locks"
    with lattice_lock(locks_dir, IDS_LOCK):
        current = load_issue_ids(lattice_dir)
        mapping: dict[str, str] = {}
        for seq, ids in sorted(by_seq.items()):
            ids.sort()
            mapping[str(seq)] = ids[0]
            if len(ids) > 1:
                result.collisions.append({"seq": seq, "issues": ids, "mapped_to": ids[0]})
        next_seq = max(int(current.get("next_seq") or 1), 1 + max(by_seq, default=0))
        data = {"schema_version": 1, "next_seq": next_seq, "map": mapping}
        atomic_write(_ids_path(lattice_dir), _serialize_ids(data))
    return result


__all__ = [
    "ISSUES_DIR",
    "IssueRebuild",
    "allocate_issue_seq",
    "current_issue",
    "issue_views",
    "issue_write_context",
    "issues_dir",
    "issues_linked_to",
    "list_issue_snapshots",
    "load_issue_ids",
    "read_issue_events",
    "read_issue_snapshot",
    "rebuild_issue_snapshots",
    "resolve_issue",
    "task_info_for",
    "write_issue_events",
]
