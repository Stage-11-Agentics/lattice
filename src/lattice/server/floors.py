"""The short-ID floor a server keeps in memory (SPEC §5, "Server").

Each project keeps ``max_observed[prefix]``: the highest short-ID sequence
any task log (active or archived) holds for that prefix, from creation events
and short-ID assignments. It is computed from the logs once, at project load,
and updated from every committed operation's appended events, so a server
create never rescans.

The allocator that consumes the floor is H-6's (``storage/operations.py``).
Until it lands this module only keeps the numbers; ``floor_for`` is the value
the server passes to that allocator.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

_SHORT_ID_RE = re.compile(r"^([A-Z][A-Z0-9]*(?:-[A-Z][A-Z0-9]*)?)-(\d+)$")
_SHORT_ID_EVENTS = ("task_created", "task_short_id_assigned")


def _observe(short_id: object, into: dict[str, int]) -> None:
    if not isinstance(short_id, str):
        return
    match = _SHORT_ID_RE.match(short_id)
    if match is None:
        return
    prefix, seq = match.group(1), int(match.group(2))
    if seq > into.get(prefix, 0):
        into[prefix] = seq


class ShortIdFloors:
    """``max_observed`` per short-ID prefix for one project."""

    def __init__(self) -> None:
        self.max_observed: dict[str, int] = {}

    @classmethod
    def from_board(cls, board: Path) -> ShortIdFloors:
        """Scan every task log, active and archived, once."""
        floors = cls()
        for events_dir in (Path(board) / "events", Path(board) / "archive" / "events"):
            if not events_dir.is_dir():
                continue
            for log in sorted(events_dir.glob("task_*.jsonl")):
                floors._scan_log(log)
        return floors

    def _scan_log(self, log: Path) -> None:
        try:
            with open(log, encoding="utf-8") as fh:
                for line in fh:
                    if not any(t in line for t in _SHORT_ID_EVENTS):
                        continue
                    try:
                        event = json.loads(line)
                    except ValueError:
                        continue
                    self.observe_event(event)
        except OSError:
            return

    def observe_event(self, event: dict) -> None:
        if event.get("type") in _SHORT_ID_EVENTS:
            data = event.get("data")
            if isinstance(data, dict):
                _observe(data.get("short_id"), self.max_observed)

    def observe_events(self, events: list[dict]) -> None:
        for event in events:
            self.observe_event(event)

    def floor_for(self, prefix: str) -> int:
        """The lowest sequence allocation may issue for *prefix*: 1 + max observed."""
        return self.max_observed.get(prefix, 0) + 1
