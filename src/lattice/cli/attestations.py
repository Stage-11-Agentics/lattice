"""Client side of SPEC §3.4: compute attestations in the caller's worktree.

The operation validates what this computes against the board's current state;
a stale attestation is refused and the command recomputes and retries once.
"""

from __future__ import annotations

from pathlib import Path

from lattice.boards import LocalBoard, git_worktree
from lattice.core.attestations import compute_reachable_review_commits, review_marker_shas
from lattice.core.ids import is_short_id, validate_id
from lattice.storage.operations import AuthoritativeLogError, read_task_authority


def caller_worktree() -> Path | None:
    """The git worktree the command runs in (the checkout a review gate inspects)."""
    return git_worktree(Path.cwd())


def _read_snapshot(lattice_dir: Path, raw_id: str) -> dict | None:
    """The task's current snapshot, or ``None`` when it cannot be read.

    Errors are the operation's to report, in their proper order; an
    attestation for a task that cannot be read is simply not sent.
    """
    from lattice.storage.short_ids import resolve_short_id

    task_id: str | None = raw_id if validate_id(raw_id, "task") else None
    if task_id is None and is_short_id(raw_id):
        task_id = resolve_short_id(lattice_dir, raw_id.upper())
    if task_id is None:
        return None
    try:
        authority = read_task_authority(lattice_dir, task_id, allow_missing=True)
    except AuthoritativeLogError:
        return None
    return authority.snapshot if authority is not None else None


def completion_attestations(
    board: LocalBoard,
    config: dict,
    raw_task: str,
    target_status: str,
    *,
    prospective: list[str] | None = None,
    worktree: Path | None = None,
) -> dict:
    """``{"reachable_review_commits": [...]}`` when the policy for *target_status*
    requires a reachable review commit and the command runs in a git worktree;
    ``{}`` otherwise (the policy then reports it lacks repository context).

    *worktree* defaults to the cwd's; the MCP server passes the one its tool
    call's ``lattice_root`` names."""
    policy = config.get("workflow", {}).get("completion_policies", {}).get(target_status, {})
    if not policy.get("require_reachable_review_commit"):
        return {}
    worktree = worktree if worktree is not None else caller_worktree()
    if worktree is None:
        return {}
    lattice_dir = board.lattice_dir
    snapshot = _read_snapshot(lattice_dir, raw_task)
    if snapshot is None:
        return {}
    shas = review_marker_shas(snapshot, lattice_dir, prospective)  # the cache reads
    board.end_read_phase()  # before git runs (SPEC §9.4)
    return {
        "reachable_review_commits": compute_reachable_review_commits(
            snapshot, lattice_dir, worktree, prospective, shas=shas
        )
    }
