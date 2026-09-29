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
    worktree. When the token's user is not the actor, the pair reads ``via
    token user@machine``, so it is not taken for a second actor. Missing parts
    are left out. ``None`` for an event written before origins existed.
    """
    origin = event.get("origin")
    if not isinstance(origin, dict):
        return None
    reported = _reported(origin)
    actor = event.get("actor", "?")
    actor_name = actor.get("name", str(actor)) if isinstance(actor, dict) else str(actor)
    parts = [actor_name]
    user, machine = _user_and_machine(origin)
    if user and machine:
        parts.append(f"{user}@{machine}")
    elif user or machine:
        parts.append(str(user or machine))
    authenticated = origin.get("authenticated")
    if isinstance(authenticated, dict) and authenticated and len(parts) > 1:
        if authenticated.get("user") != actor_name:
            parts[1] = f"via token {parts[1]}"
    if reported.get("source") == "browser":
        parts.append("browser")
    elif reported.get("worktree"):
        branch = reported.get("branch")
        parts.append(f"{reported['worktree']} ({branch})" if branch else reported["worktree"])
    return " · ".join(parts)


def _reported(origin: dict) -> dict:
    reported = origin.get("reported")
    return reported if isinstance(reported, dict) else {}


def _user_and_machine(origin: dict) -> tuple[object, object]:
    """``authenticated`` user and machine when a server stamped them, else ``reported``."""
    authenticated = origin.get("authenticated")
    if isinstance(authenticated, dict) and authenticated:
        return authenticated.get("user"), authenticated.get("machine")
    reported = _reported(origin)
    return reported.get("os_user"), reported.get("host")


def origin_matches(
    event: dict,
    *,
    user: str | None = None,
    machine: str | None = None,
    worktrees: frozenset[str] | None = None,
) -> bool:
    """Whether *event*'s origin satisfies every filter given (``lattice list``).

    User and machine are the values ``format_origin_line`` shows (authenticated,
    else reported); the worktree is the reported one, matched against any of
    *worktrees*. An event written before origins existed matches nothing.
    """
    origin = event.get("origin")
    if not isinstance(origin, dict):
        return False
    event_user, event_machine = _user_and_machine(origin)
    if user is not None and event_user != user:
        return False
    if machine is not None and event_machine != machine:
        return False
    if worktrees is not None:
        reported = _reported(origin)
        if reported.get("source") == "browser" or reported.get("worktree") not in worktrees:
            return False
    return True
