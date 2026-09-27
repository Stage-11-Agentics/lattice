"""Client attestations (SPEC §3.4): facts only the caller's machine can observe.

``reachable_review_commits`` is a list of ``{sha, branch, exists, reachable}``,
one entry per ``Lattice-Reviewed-Commit`` marker in the task's review artifacts
and in any review payload the same operation attaches. The client computes it
in its own worktree (:func:`compute_reachable_review_commits`); the operation
checks it against the board's current state (:func:`stale_reason`) and the
completion policy passes when an entry both exists and is reachable
(:func:`attested_reachable`).

``review_head`` is the ``HEAD`` commit of the caller's worktree, which
``complete`` writes as the marker of the review payload it attaches.
"""

from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path

REVIEW_MARKER_RE = re.compile(r"\ALattice-Reviewed-Commit: ([0-9a-f]{40})\n")

#: ``details.reason`` of the ``COMPLETION_BLOCKED`` a stale attestation raises;
#: the client recomputes and retries once when it sees it.
STALE_ATTESTATION = "STALE_ATTESTATION"


def review_marker(payload: str) -> str | None:
    """The commit a review payload's first line names, or ``None``."""
    match = REVIEW_MARKER_RE.match(payload)
    return match.group(1) if match else None


def latest_branch(snapshot: dict) -> str | None:
    """The task's latest branch link, or ``None``."""
    branches = snapshot.get("branch_links", [])
    branch = branches[-1].get("branch") if branches else None
    return branch if isinstance(branch, str) and branch else None


def review_payloads(snapshot: dict, lattice_dir: Path) -> list[str]:
    """The text of every review-role artifact payload on the task, in evidence order."""
    payloads: list[str] = []
    for ref in snapshot.get("evidence_refs", []):
        if ref.get("source_type") != "artifact" or ref.get("role") != "review":
            continue
        artifact_id = ref.get("id")
        if not isinstance(artifact_id, str):
            continue
        meta_path = lattice_dir / "artifacts" / "meta" / f"{artifact_id}.json"
        try:
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
            payload_name = meta.get("payload", {}).get("file")
            if isinstance(payload_name, str):
                payloads.append(
                    (lattice_dir / "artifacts" / "payload" / payload_name).read_text(
                        encoding="utf-8"
                    )
                )
        except (OSError, json.JSONDecodeError):
            continue
    return payloads


def review_marker_shas(
    snapshot: dict, lattice_dir: Path, prospective: list[str] | None = None
) -> list[str]:
    """Every marker SHA in *prospective* payloads and the task's review artifacts.

    Ordered as the payloads are checked, without duplicates.
    """
    shas: list[str] = []
    for payload in [*(prospective or []), *review_payloads(snapshot, lattice_dir)]:
        sha = review_marker(payload)
        if sha is not None and sha not in shas:
            shas.append(sha)
    return shas


def _git_ok(repo_root: Path, *args: str) -> bool:
    return (
        subprocess.run(["git", "-C", str(repo_root), *args], capture_output=True).returncode == 0
    )


def compute_reachable_review_commits(
    snapshot: dict, lattice_dir: Path, repo_root: Path, prospective: list[str] | None = None
) -> list[dict]:
    """The client's attestation: each marker SHA checked in *repo_root*.

    ``exists``: ``git cat-file -e <sha>^{commit}``; ``reachable``:
    ``git merge-base --is-ancestor <sha> <branch>`` against the task's latest
    branch link (false when the task has none).
    """
    branch = latest_branch(snapshot)
    entries: list[dict] = []
    for sha in review_marker_shas(snapshot, lattice_dir, prospective):
        exists = _git_ok(repo_root, "cat-file", "-e", f"{sha}^{{commit}}")
        reachable = branch is not None and _git_ok(
            repo_root, "merge-base", "--is-ancestor", sha, branch
        )
        entries.append({"sha": sha, "branch": branch, "exists": exists, "reachable": reachable})
    return entries


def check_entries_shape(entries: object) -> list[dict] | None:
    """*entries* as a list of well-formed entries, or ``None`` when malformed."""
    if not isinstance(entries, list):
        return None
    for entry in entries:
        if (
            not isinstance(entry, dict)
            or set(entry) != {"sha", "branch", "exists", "reachable"}
            or not isinstance(entry["sha"], str)
            or not (entry["branch"] is None or isinstance(entry["branch"], str))
            or not isinstance(entry["exists"], bool)
            or not isinstance(entry["reachable"], bool)
        ):
            return None
    return entries


def stale_reason(entries: list[dict], snapshot: dict, marker_shas: list[str]) -> str | None:
    """Why *entries* do not describe the task as the board has it now, or ``None``.

    Every entry must name the task's current latest branch link, and the
    entries must cover exactly the marker SHAs the board holds.
    """
    branch = latest_branch(snapshot)
    for entry in entries:
        if entry["branch"] != branch:
            return (
                f"attestation names branch {entry['branch']!r}, but the task's latest "
                f"branch link is {branch!r}"
            )
    attested = [entry["sha"] for entry in entries]
    missing = [sha for sha in marker_shas if sha not in attested]
    if missing:
        return f"attestation omits review marker {missing[0]}"
    extra = [sha for sha in attested if sha not in marker_shas]
    if extra:
        return f"attestation lists {extra[0]}, which no review artifact of the task names"
    return None


def attested_reachable(entries: list[dict]) -> bool:
    """The ``require_reachable_review_commit`` policy: one entry exists and is reachable."""
    return any(entry["exists"] and entry["reachable"] for entry in entries)
