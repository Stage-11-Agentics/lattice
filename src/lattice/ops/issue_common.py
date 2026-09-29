"""What the ``issue.*`` operations share (LAT-361). Not an operation module.

Every issue operation first checks :func:`require_issue_log`: the issue log
works only on a local board (``LOCAL_ONLY``), and only when the board turned
it on (``ISSUES_DISABLED``). Writes go through :func:`append`, which replays
the issue's log under its lock, lets the caller decide, and writes the event
and the new snapshot.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from lattice.core.config import issues_enabled
from lattice.core.events import create_issue_event
from lattice.core.issues import apply_issue_event, issues_disabled_message
from lattice.core.visibility import require_not_tombstoned
from lattice.ops.base import CommonParams, OpContext, OpError, OpResult
from lattice.storage.issues import (
    current_issue,
    issue_views,
    issue_write_context,
    issues_dir,
    resolve_issue,
    write_issue_events,
)
from lattice.storage.operations import read_task_authority
from lattice.storage.ownership import board_state

LOCAL_ONLY_MESSAGE = "The issue log works only on local boards for now; this board is {state}."


def require_issue_log(ctx: OpContext) -> None:
    """``LOCAL_ONLY`` on a hosted board or a cache; ``ISSUES_DISABLED`` when it is off."""
    state = board_state(ctx.lattice_dir)
    if state != "local":
        raise OpError("LOCAL_ONLY", LOCAL_ONLY_MESSAGE.format(state=state), {"board": state})
    if not issues_enabled(ctx.config):
        raise OpError(
            "ISSUES_DISABLED", issues_disabled_message(issues_dir(ctx.lattice_dir).is_dir())
        )


@dataclass(frozen=True, kw_only=True)
class IssueParams(CommonParams):
    """``issue``: an issue ID as the caller gave it (``LAT-I3``, ``I3`` or ``iss_...``)."""

    issue: str


#: ``decide(snapshot)`` returns the event to append as ``(type, data)``, or
#: ``None`` when there is nothing to do; it raises ``OpError`` to refuse.
Decide = Callable[[dict], "tuple[str, dict] | None"]


def append(
    ctx: OpContext,
    issue_id: str,
    decide: Decide,
    p: CommonParams,
    *,
    reason: bool = True,
) -> tuple[dict, list[dict]]:
    """Under the issue's lock: replay, decide, append, snapshot.

    Returns the issue's snapshot and the events written (empty when *decide*
    had nothing to do).
    """
    with issue_write_context(ctx.lattice_dir, issue_id):
        snapshot = current_issue(ctx.lattice_dir, issue_id)
        if snapshot is None:
            raise OpError("NOT_FOUND", f"Issue {issue_id} not found.")
        decision = decide(snapshot)
        if decision is None:
            return snapshot, []
        event_type, data = decision
        event = create_issue_event(
            event_type, issue_id, ctx.actor, data, **p.provenance(reason=reason)
        )
        snapshot = apply_issue_event(snapshot, event)
        write_issue_events(ctx.lattice_dir, issue_id, [event], snapshot)
    return snapshot, [event]


def display(snapshot: dict) -> str:
    return snapshot.get("short_id") or snapshot["id"]


def refuse_closed(snapshot: dict, doing: str) -> None:
    """``CONFLICT`` when the issue is dismissed or a duplicate."""
    closure = snapshot.get("closure")
    if closure:
        name = display(snapshot)
        kind = "a duplicate" if closure["kind"] == "duplicate" else "dismissed"
        raise OpError(
            "CONFLICT",
            f"Issue {name} is {kind}; run 'lattice issue reopen {name}' before you {doing}.",
            {"issue": snapshot["id"], "closure": closure},
        )


def linkable_task(ctx: OpContext, raw_task: str) -> dict:
    """The task *raw_task* names, active or archived; ``NOT_FOUND`` / ``TASK_ERASED``."""
    task_id = ctx.resolve_task(raw_task)
    authority = read_task_authority(ctx.lattice_dir, task_id, allow_missing=True)
    if authority is None:
        raise OpError("NOT_FOUND", f"Task {raw_task} not found.")
    require_not_tombstoned(authority.snapshot)
    return authority.snapshot


def link_one(
    ctx: OpContext, issue_id: str, task_id: str, p: CommonParams
) -> tuple[dict, list[dict]]:
    """Link one issue to one task; idempotent when already linked."""

    def decide(snapshot: dict) -> tuple[str, dict] | None:
        refuse_closed(snapshot, "linking it")
        if any(link["task_id"] == task_id for link in snapshot["links"]):
            return None
        return "issue_linked", {"task_id": task_id}

    return append(ctx, issue_id, decide, p)


def resolve(ctx: OpContext, raw: str) -> str:
    return resolve_issue(ctx.lattice_dir, raw)


def view(ctx: OpContext, snapshot: dict) -> dict:
    return issue_views(ctx.lattice_dir, [snapshot])[0]


def result(ctx: OpContext, snapshot: dict, events: list[dict]) -> OpResult:
    """The ``OpResult`` of an issue operation: the issue's view is its ``--json`` data."""
    return OpResult(events=events, value=view(ctx, snapshot), idempotent=not events)
