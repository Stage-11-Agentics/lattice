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


def format_origin_line(event: dict) -> str | None:
    """``actor · user@machine · worktree (branch)`` for an event that has an origin.

    ``user@machine`` comes from ``authenticated`` when a server stamped it,
    else from ``reported``; a browser write shows ``browser`` in place of the
    worktree. Missing parts are left out. ``None`` for an event written
    before origins existed.
    """
    origin = event.get("origin")
    if not isinstance(origin, dict):
        return None
    reported = origin.get("reported") if isinstance(origin.get("reported"), dict) else {}
    authenticated = (
        origin.get("authenticated") if isinstance(origin.get("authenticated"), dict) else {}
    )
    actor = event.get("actor", "?")
    parts = [actor.get("name", str(actor)) if isinstance(actor, dict) else str(actor)]
    if authenticated:
        user, machine = authenticated.get("user"), authenticated.get("machine")
    else:
        user, machine = reported.get("os_user"), reported.get("host")
    if user and machine:
        parts.append(f"{user}@{machine}")
    elif user or machine:
        parts.append(str(user or machine))
    if reported.get("source") == "browser":
        parts.append("browser")
    elif reported.get("worktree"):
        branch = reported.get("branch")
        parts.append(f"{reported['worktree']} ({branch})" if branch else reported["worktree"])
    return " · ".join(parts)
