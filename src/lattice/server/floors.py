"""The short-ID floor a server keeps in memory (SPEC §5, "Server").

Each project keeps ``max_observed[prefix]``: the highest short-ID sequence any
task log (active or archived) holds for that prefix, in any event's
``data.short_id``. It is computed from the logs once, at project load
(:func:`lattice.storage.short_ids.max_observed_short_ids`), updated from every
committed operation's appended events, and passed to allocation as
``execute(short_id_floor=...)``, so a server create never rescans.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from pathlib import Path
from types import MappingProxyType

from lattice.storage.short_ids import max_observed_short_ids, split_short_id


class ShortIdFloors:
    """``max_observed`` per short-ID prefix for one project: an immutable value.

    Observing a committed line's events returns a new value (:meth:`with_events`),
    which the project publishes with the rest of its finalized memory.
    """

    __slots__ = ("max_observed",)

    def __init__(self, max_observed: Mapping[str, int] | None = None) -> None:
        self.max_observed: Mapping[str, int] = MappingProxyType(dict(max_observed or {}))

    @classmethod
    def from_board(cls, board: Path) -> ShortIdFloors:
        """Scan every task log, active and archived, once."""
        return cls(max_observed_short_ids(Path(board)))

    def with_events(self, events: Iterable[dict]) -> ShortIdFloors:
        """The floors after *events*; this value is unchanged."""
        observed = dict(self.max_observed)
        for event in events:
            data = event.get("data")
            if not isinstance(data, dict):
                continue
            parsed = split_short_id(data.get("short_id"))
            if parsed is not None and parsed[1] > observed.get(parsed[0], 0):
                observed[parsed[0]] = parsed[1]
        return ShortIdFloors(observed)

    def floor_for(self, prefix: str) -> int:
        """The lowest sequence allocation may issue for *prefix*: 1 + max observed."""
        return self.max_observed.get(prefix, 0) + 1

    def __eq__(self, other: object) -> bool:
        return isinstance(other, ShortIdFloors) and dict(self.max_observed) == dict(
            other.max_observed
        )

    def __hash__(self) -> int:
        return hash(tuple(sorted(self.max_observed.items())))
