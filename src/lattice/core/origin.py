"""The origin context: where the change being written came from (SPEC §4).

``lattice.ops.execute`` sets the origin for the duration of one operation;
the storage write path stamps it onto every event it appends. Outside an
operation there is no origin and nothing is stamped.
"""

from __future__ import annotations

import contextlib
import copy
from collections.abc import Iterator
from contextvars import ContextVar

_ORIGIN: ContextVar[dict | None] = ContextVar("lattice_origin", default=None)


def current_origin() -> dict | None:
    """Return the origin of the operation in progress, or ``None``."""
    return _ORIGIN.get()


@contextlib.contextmanager
def origin_scope(origin: dict) -> Iterator[None]:
    """Make *origin* the current origin until the block exits."""
    token = _ORIGIN.set(origin)
    try:
        yield
    finally:
        _ORIGIN.reset(token)


def stamp_origin(event: dict) -> dict:
    """Give *event* a copy of the current origin unless it already has one."""
    origin = _ORIGIN.get()
    if origin is not None and "origin" not in event:
        event["origin"] = copy.deepcopy(origin)
    return event
