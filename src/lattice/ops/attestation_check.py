"""Check a caller's ``reachable_review_commits`` attestation against the board (SPEC §3.4)."""

from __future__ import annotations

from lattice.core.attestations import (
    STALE_ATTESTATION,
    check_entries_shape,
    review_marker_shas,
    stale_reason,
)
from lattice.core.errors import OpError, task_state_snapshot
from lattice.ops.base import OpContext


def attested_review_commits(
    ctx: OpContext,
    snapshot: dict,
    policy: dict,
    *,
    prospective: list[str] | None = None,
) -> list[dict] | None:
    """The caller's ``reachable_review_commits`` when *policy* needs them, else ``None``.

    ``None`` too when the caller sent none (it had no worktree); the policy
    then reports that it lacks repository context, as it always has. A
    malformed attestation is ``VALIDATION_ERROR``. Freshness is checked
    always, ``--force`` included (force bypasses only the policy verdict):
    entries that do not name the task's current latest branch link, or that do not
    cover exactly the marker SHAs the board holds, one entry each (plus *prospective*
    payloads the operation is about to attach), are ``COMPLETION_BLOCKED``
    with ``details.reason`` ``STALE_ATTESTATION``: the client recomputes and
    retries once.
    """
    if not policy.get("require_reachable_review_commit"):
        return None
    raw = ctx.caller.attestations.get("reachable_review_commits")
    if raw is None:
        return None
    entries = check_entries_shape(raw)
    if entries is None:
        raise OpError(
            "VALIDATION_ERROR",
            "Malformed reachable_review_commits attestation: expected a list of "
            "{sha, branch, exists, reachable}.",
        )
    reason = stale_reason(
        entries, snapshot, review_marker_shas(snapshot, ctx.lattice_dir, prospective)
    )
    if reason is not None:
        raise OpError(
            "COMPLETION_BLOCKED",
            f"Stale reachable_review_commits attestation: {reason}. Re-read the task and retry.",
            {"reason": STALE_ATTESTATION, "snapshot": task_state_snapshot(snapshot)},
        )
    return entries
