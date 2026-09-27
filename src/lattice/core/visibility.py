"""Which tasks the default views show (SPEC §7).

An erased task (``tombstoned`` in its snapshot) keeps every file and every
event, but ``list``, ``next``, stats, and the dashboard boards leave it out
unless asked. ``show`` still finds it and says it is erased.
"""

from __future__ import annotations

from collections.abc import Iterable

from lattice.core.errors import TaskErased


def is_tombstoned(snapshot: dict | None) -> bool:
    """True when *snapshot* is an erased task."""
    return bool(snapshot and snapshot.get("tombstoned"))


def visible(snapshots: Iterable[dict], *, include_tombstoned: bool = False) -> list[dict]:
    """The snapshots a default view shows: every one but the erased, unless included."""
    if include_tombstoned:
        return list(snapshots)
    return [snap for snap in snapshots if not is_tombstoned(snap)]


def erased_line(snapshot: dict) -> str:
    """The line ``show`` prints for an erased task."""
    return f"ERASED: {snapshot.get('tombstone_reason') or ''}".rstrip()


def require_not_tombstoned(snapshot: dict | None) -> None:
    """Raise ``TASK_ERASED`` (with the task's snapshot) when *snapshot* is erased."""
    if is_tombstoned(snapshot):
        assert snapshot is not None
        raise TaskErased(snapshot)
