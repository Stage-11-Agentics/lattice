"""The short-ID floor a server keeps in memory (SPEC §5, "Server").

Each project keeps ``max_observed[prefix]``: the highest short-ID sequence any
task log (active or archived) holds for that prefix, in any event's
``data.short_id``. It is computed from the logs once, at project load
(:func:`lattice.storage.short_ids.max_observed_short_ids`), updated from every
committed operation's appended events, and passed to allocation as
``execute(short_id_floor=...)``, so a server create never rescans.
"""

from __future__ import annotations

from pathlib import Path

from lattice.storage.short_ids import max_observed_short_ids, split_short_id


class ShortIdFloors:
    """``max_observed`` per short-ID prefix for one project."""

    def __init__(self, max_observed: dict[str, int] | None = None) -> None:
        self.max_observed: dict[str, int] = dict(max_observed or {})

    @classmethod
    def from_board(cls, board: Path) -> ShortIdFloors:
        """Scan every task log, active and archived, once."""
        return cls(max_observed_short_ids(Path(board)))

    def observe_event(self, event: dict) -> None:
        data = event.get("data")
        if not isinstance(data, dict):
            return
        parsed = split_short_id(data.get("short_id"))
        if parsed is not None and parsed[1] > self.max_observed.get(parsed[0], 0):
            self.max_observed[parsed[0]] = parsed[1]

    def observe_events(self, events: list[dict]) -> None:
        for event in events:
            self.observe_event(event)

    def floor_for(self, prefix: str) -> int:
        """The lowest sequence allocation may issue for *prefix*: 1 + max observed."""
        return self.max_observed.get(prefix, 0) + 1
