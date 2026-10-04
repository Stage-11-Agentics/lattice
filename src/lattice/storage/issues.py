"""The optional issue log's files (LAT-361).

Everything lives in ``.lattice/issues/``, created by the first ``issue file``::

    issues/ids.json                   the issue-only sequence: next_seq and seq -> iss_ ID
    issues/events/iss_<ULID>.jsonl    one issue's event log (the authority)
    issues/iss_<ULID>.json            its snapshot, a full replay of that log
    issues/media/<iss_ULID>/          its photos and videos (LAT-366, storage/issue_media.py)

Nothing else in Lattice enumerates ``issues/``: task rebuilds, doctor, stats,
archive, the dashboard and the short-ID floor never see it. The readers here
call no writer, so ``show`` can use them.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import stat
from collections.abc import Callable, Generator, Iterable
from dataclasses import dataclass, field
from pathlib import Path

from lattice.core.errors import OpError
from lattice.core.issues import (
    ISSUE_STATES,
    TaskInfo,
    actor_matches,
    issue_activity,
    issue_comments,
    issue_origin,
    issue_view,
    parse_issue_ref,
    redact_removed_media_names,
    replay_issue,
    serialize_issue_snapshot,
    validate_issue_media_hashes,
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


def has_issue_metadata(lattice_dir: Path) -> bool:
    """Whether actual issue metadata exists, excluding empty directory scaffolds.

    Event logs and snapshots count by their names. The ID index counts only
    when it contains at least one mapping, so a reset-created empty cache index
    does not make a disabled log claim that prior issues are kept.
    """
    root = issues_dir(lattice_dir)
    try:
        if not stat.S_ISDIR(os.lstat(root).st_mode):
            return False
    except OSError:
        return False

    def contains_file(directory: Path, prefix: str, suffix: str) -> bool:
        try:
            if not stat.S_ISDIR(os.lstat(directory).st_mode):
                return False
            with os.scandir(directory) as entries:
                return any(
                    entry.name.startswith(prefix)
                    and entry.name.endswith(suffix)
                    and entry.is_file(follow_symlinks=False)
                    for entry in entries
                )
        except OSError:
            return False

    if contains_file(root, "iss_", ".json") or contains_file(root / "events", "iss_", ".jsonl"):
        return True

    ids_path = root / "ids.json"
    try:
        if not stat.S_ISREG(os.lstat(ids_path).st_mode):
            return False
        data = json.loads(ids_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False
    mapping = data.get("map") if isinstance(data, dict) else None
    return isinstance(mapping, dict) and bool(mapping)


def has_synced_issue_files(lattice_dir: Path) -> bool:
    """Whether any synced path exists under ``issues/`` (everything except ``issues/media``).

    This is the version-gate rule: a 0.2.1 client cannot accept any synced file
    under ``issues/``, including an ``ids.json`` with an empty map. An empty
    directory scaffold holds no file and does not count.
    """
    root = issues_dir(lattice_dir)
    try:
        if not stat.S_ISDIR(os.lstat(root).st_mode):
            return False
    except OSError:
        return False

    def walk(directory: Path, top: bool) -> bool:
        try:
            with os.scandir(directory) as entries:
                for entry in entries:
                    if top and entry.name == "media":
                        continue
                    if entry.is_dir(follow_symlinks=False):
                        if walk(Path(entry.path), False):
                            return True
                    else:
                        return True
        except OSError:
            return False
        return False

    return walk(root, True)


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


def read_issue_snapshot(
    lattice_dir: Path, issue_id: str, *, on_unreadable: OnUnreadable | None = None
) -> dict | None:
    """The issue, from its snapshot, or rebuilt in memory from its log when the
    snapshot is missing (a crash after the first event) or unreadable (a merge
    conflict). Writes nothing.

    An unreadable snapshot that the log replaces is reported to
    *on_unreadable*. ``None`` when there is neither file; ``INTEGRITY_ERROR``
    (the snapshot's) when the snapshot is unreadable and the log is unreadable
    or absent.
    """
    path = _snapshot_path(lattice_dir, issue_id)
    if not path.exists():
        return _replayed(lattice_dir, issue_id)
    try:
        snapshot = _load_json(path)
        try:
            validate_issue_media_hashes(snapshot)
        except ValueError as exc:
            raise OpError("INTEGRITY_ERROR", f"Cannot read {path}: {exc}.") from exc
        return snapshot
    except OpError as exc:
        try:
            replayed = _replayed(lattice_dir, issue_id)
        except OpError:
            replayed = None
        if replayed is None:
            raise
        if on_unreadable is not None:
            on_unreadable(path, exc)
        return replayed


def list_issue_snapshots(
    lattice_dir: Path, *, on_unreadable: OnUnreadable | None = None
) -> list[dict]:
    """Every issue with a readable snapshot or log, by sequence number.

    An issue whose snapshot is missing or unreadable is rebuilt in memory from
    its log; missing snapshots are found by comparing file names, so the other
    logs are not read. Every unreadable file is reported to *on_unreadable*:
    the issue is still listed when its log replaces the snapshot, and skipped
    only when the log is unreadable or absent too. Without *on_unreadable*,
    an issue that cannot be read raises ``INTEGRITY_ERROR``.
    """
    directory = issues_dir(lattice_dir)
    if not directory.is_dir():
        return []
    snapshot_ids = sorted(p.stem for p in directory.glob("iss_*.json"))
    events_dir = directory / "events"
    missing = (
        sorted(p.stem for p in events_dir.glob("iss_*.jsonl") if p.stem not in set(snapshot_ids))
        if events_dir.is_dir()
        else []
    )
    snapshots: list[dict] = []
    for path, load in [
        *(
            (
                _snapshot_path(lattice_dir, i),
                lambda i=i: read_issue_snapshot(lattice_dir, i, on_unreadable=on_unreadable),
            )
            for i in snapshot_ids
        ),
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
    """The ``--json`` views of *snapshots*, each linked task read once; each
    view's ``media`` carries its files' paths (LAT-366)."""
    from lattice.storage.issue_media import media_views

    task_ids = [link["task_id"] for s in snapshots for link in s.get("links", [])]
    info = task_info_for(lattice_dir, task_ids)
    views = []
    for snapshot in snapshots:
        try:
            filed_event = next(
                (
                    e
                    for e in read_issue_events(lattice_dir, snapshot["id"])
                    if e.get("type") == "issue_filed"
                ),
                None,
            )
        except OpError:
            filed_event = None
        filed_origin = issue_origin(filed_event.get("origin")) if filed_event else None
        view = issue_view(snapshot, info, filed_origin)
        view["media"] = media_views(lattice_dir, snapshot)
        views.append(view)
    return views


def issues_by(
    lattice_dir: Path,
    actor: str,
    *,
    states: Iterable[str] | None = None,
    on_unreadable: OnUnreadable | None = None,
) -> list[dict]:
    """The issue views filed or commented on by *actor*.

    Each result adds ``activity`` (``filed`` takes precedence). With no state
    filter, every state is searched; ``on_unreadable`` reports and skips logs
    that cannot be read.
    """
    wanted = set(ISSUE_STATES if states is None else states)
    snapshots = list_issue_snapshots(lattice_dir, on_unreadable=on_unreadable)
    views = issue_views(lattice_dir, snapshots)
    order = {state: index for index, state in enumerate(ISSUE_STATES)}
    matches = []
    for view in views:
        if view["state"] not in wanted:
            continue
        activity = "filed" if actor_matches(view.get("filed_by"), actor) else None
        if activity is None:
            path = _events_path(lattice_dir, view["id"])
            try:
                activity = issue_activity(read_issue_events(lattice_dir, view["id"]), actor)
            except OpError as exc:
                if on_unreadable is None:
                    raise
                on_unreadable(path, exc)
                continue
        if activity is not None:
            matches.append({**view, "activity": activity})
    return sorted(matches, key=lambda view: (order[view["state"]], view.get("seq") or 0))


def issue_detail(
    lattice_dir: Path,
    issue_id: str,
    *,
    on_unreadable: OnUnreadable | None = None,
) -> dict | None:
    """The full issue detail used by ``issue show`` and dashboard readers.

    The log is kept in event form, except that removed media names are
    redacted from the returned history. ``None`` means the issue has no
    readable snapshot or log.
    """
    resolved = resolve_issue(lattice_dir, issue_id)
    snapshot = read_issue_snapshot(lattice_dir, resolved, on_unreadable=on_unreadable)
    if snapshot is None:
        return None
    events = read_issue_events(lattice_dir, resolved)
    view = issue_views(lattice_dir, [snapshot])[0]
    return {
        **view,
        "comments": issue_comments(events),
        "events": redact_removed_media_names(events, snapshot),
    }


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


@contextlib.contextmanager
def source_ref_lock(
    lattice_dir: Path, source: str, source_ref: str
) -> Generator[None, None, None]:
    """Serialize one normalized source/ref pair before any issue or sequence lock."""
    pair = json.dumps([source, source_ref], ensure_ascii=False, separators=(",", ":"))
    key = hashlib.sha256(pair.encode("utf-8")).hexdigest()
    locks_dir = Path(lattice_dir) / "locks"
    ensure_dir(locks_dir)
    with lattice_lock(locks_dir, f"issue_source_ref_{key}"):
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
    with lattice_lock(locks_dir, IDS_LOCK):
        return _allocate_issue_seq_locked(lattice_dir, issue_id)


def _allocate_issue_seq_locked(lattice_dir: Path, issue_id: str) -> int:
    ensure_dir(issues_dir(lattice_dir) / "events")
    data = load_issue_ids(lattice_dir)
    used = [int(k) for k in data["map"] if str(k).isdigit()]
    seq = max(int(data.get("next_seq") or 1), 1 + max(used, default=0))
    data["map"][str(seq)] = issue_id
    data["next_seq"] = seq + 1
    data["schema_version"] = 1
    atomic_write(_ids_path(lattice_dir), _serialize_ids(data))
    return seq


@contextlib.contextmanager
def issue_seq_reservation(
    lattice_dir: Path, issue_id: str
) -> Generator[tuple[int, Callable[[], None]], None, None]:
    """Reserve an issue number until its filing event is committed.

    The issue sequence lock remains held through the caller's transaction so
    an aborted filing can restore the exact previous index without a gap.
    Call the yielded commit function after the filing event is durable.
    """
    locks_dir = Path(lattice_dir) / "locks"
    ensure_dir(locks_dir)
    with lattice_lock(locks_dir, IDS_LOCK):
        previous = _serialize_ids(load_issue_ids(lattice_dir))
        seq = _allocate_issue_seq_locked(lattice_dir, issue_id)
        committed = False

        def commit() -> None:
            nonlocal committed
            committed = True

        try:
            yield seq, commit
        except BaseException:
            if not committed:
                atomic_write(_ids_path(lattice_dir), previous)
            raise
        else:
            if not committed:
                atomic_write(_ids_path(lattice_dir), previous)


def current_issue(lattice_dir: Path, issue_id: str) -> dict | None:
    """The issue replayed from its log (the snapshot file may be stale)."""
    path = _events_path(lattice_dir, issue_id)
    try:
        return replay_issue(read_issue_events(lattice_dir, issue_id))
    except (ValueError, KeyError) as exc:
        raise OpError("INTEGRITY_ERROR", f"Cannot replay {path}: {exc}.") from exc


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
    "has_issue_metadata",
    "issue_views",
    "issue_detail",
    "issue_write_context",
    "source_ref_lock",
    "issues_by",
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
