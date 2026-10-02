"""The short-ID floor a server keeps in memory (SPEC §5, "Server").

Each project keeps the complete allocation floor and exact IDs already in
event history. The floor includes active and archived task logs, lifecycle,
and the ID map; map-only reservations are kept distinct so an interrupted
create can resume only its own reservation. Both values are computed once at
load and updated from every committed operation, so a hosted create never
rescans.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from pathlib import Path
from types import MappingProxyType

from lattice.storage.short_ids import short_id_inventory, split_short_id


class ShortIdFloors:
    """``max_observed`` per short-ID prefix for one project: an immutable value.

    Observing a committed line's events returns a new value (:meth:`with_events`),
    which the project publishes with the rest of its finalized memory.
    """

    __slots__ = ("max_observed", "event_short_ids")

    def __init__(
        self,
        max_observed: Mapping[str, int] | None = None,
        event_short_ids: Iterable[str] = (),
    ) -> None:
        self.max_observed: Mapping[str, int] = MappingProxyType(dict(max_observed or {}))
        self.event_short_ids = frozenset(event_short_ids)

    @classmethod
    def from_board(cls, board: Path) -> ShortIdFloors:
        """Scan all assigned-ID sources once."""
        inventory = short_id_inventory(Path(board), include_occurrences=False)
        return cls(inventory.max_observed, inventory.event_short_ids)

    def with_events(self, events: Iterable[dict]) -> ShortIdFloors:
        """The floors after *events*; this value is unchanged."""
        observed = dict(self.max_observed)
        event_short_ids = set(self.event_short_ids)
        for event in events:
            data = event.get("data")
            if not isinstance(data, dict):
                continue
            parsed = split_short_id(data.get("short_id"))
            if parsed is not None:
                event_short_ids.add(data["short_id"])
                if parsed[1] > observed.get(parsed[0], 0):
                    observed[parsed[0]] = parsed[1]
        return ShortIdFloors(observed, event_short_ids)

    def floor_for(self, prefix: str) -> int:
        """The lowest sequence allocation may issue for *prefix*: 1 + max observed."""
        return self.max_observed.get(prefix, 0) + 1

    def __eq__(self, other: object) -> bool:
        return (
            isinstance(other, ShortIdFloors)
            and dict(self.max_observed) == dict(other.max_observed)
            and self.event_short_ids == other.event_short_ids
        )

    def __hash__(self) -> int:
        return hash(
            (tuple(sorted(self.max_observed.items())), tuple(sorted(self.event_short_ids)))
        )
