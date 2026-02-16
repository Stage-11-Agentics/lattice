"""Read-only git integration for the Lattice dashboard.

Shells out to ``git`` CLI via :mod:`subprocess` — no Python git dependencies.
All operations are strictly read-only.  When git is unavailable or the
``.lattice/`` directory is not inside a git repository, the module degrades
gracefully and returns structured ``available=False`` payloads.
"""

from __future__ import annotations

import hashlib
import re
import shutil
import subprocess
import time
from pathlib import Path
from typing import Any

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

GIT_COMMAND_TIMEOUT = 10  # seconds per subprocess call
CACHE_TTL_SECONDS = 30  # how long the summary cache is valid

# Patterns for extracting Lattice task references from commit messages.
# Matches short IDs like LAT-42, PROJ-7, or any UPPER-ID pattern:
_SHORT_ID_RE = re.compile(r"\b([A-Z][A-Z0-9]+-\d+)\b")
# Matches ULID-style task IDs like task_01HQ...:
_ULID_RE = re.compile(r"\b(task_[0-9A-Z]{26})\b")


# ---------------------------------------------------------------------------
# Low-level git helpers
# ---------------------------------------------------------------------------


def _run_git(
    args: list[str],
    cwd: Path,
    *,
    timeout: int = GIT_COMMAND_TIMEOUT,
) -> subprocess.CompletedProcess[str]:
    """Run a git command and return the CompletedProcess.

    Never uses ``shell=True``.  Raises on timeout.
    """
    return subprocess.run(
        ["git", *args],
        cwd=str(cwd),
        capture_output=True,
        text=True,
        timeout=timeout,
    )


def git_available() -> bool:
    """Return True if the ``git`` executable is on PATH."""
    return shutil.which("git") is not None


def find_git_root(start: Path) -> Path | None:
    """Return the git repo root containing *start*, or None."""
    try:
        result = _run_git(
            ["rev-parse", "--show-toplevel"],
            cwd=start,
        )
        if result.returncode == 0:
            root = result.stdout.strip()
            if root:
                return Path(root)
    except (subprocess.TimeoutExpired, OSError):
        pass
    return None


# ---------------------------------------------------------------------------
# Data retrieval
# ---------------------------------------------------------------------------


def get_current_branch(repo_root: Path) -> str | None:
    """Return the current branch name, or None if in detached HEAD state."""
    try:
        result = _run_git(["rev-parse", "--abbrev-ref", "HEAD"], cwd=repo_root)
        if result.returncode == 0:
            name = result.stdout.strip()
            if name and name != "HEAD":
                return name
    except (subprocess.TimeoutExpired, OSError):
        pass
    return None


def get_branches(repo_root: Path) -> list[dict[str, Any]]:
    """Return a list of local branch metadata dicts.

    Each dict has keys: ``name``, ``is_current``, ``commit_hash``,
    ``commit_subject``, ``commit_date``, ``author_name``, ``author_email``.
    """
    # Format: refname:short, HEAD indicator, objectname:short, subject, authordate:iso-strict, authorname, authoremail
    fmt = "%(refname:short)%09%(HEAD)%09%(objectname:short)%09%(subject)%09%(authordate:iso-strict)%09%(authorname)%09%(authoremail)"
    try:
        result = _run_git(
            ["for-each-ref", "--sort=-authordate", f"--format={fmt}", "refs/heads/"],
            cwd=repo_root,
        )
        if result.returncode != 0:
            return []
    except (subprocess.TimeoutExpired, OSError):
        return []

    branches: list[dict[str, Any]] = []
    for line in result.stdout.strip().splitlines():
        parts = line.split("\t")
        if len(parts) < 7:
            continue
        branches.append(
            {
                "name": parts[0],
                "is_current": parts[1].strip() == "*",
                "commit_hash": parts[2],
                "commit_subject": parts[3],
                "commit_date": parts[4],
                "author_name": parts[5],
                "author_email": parts[6],
            }
        )
    return branches


def get_recent_commits(
    repo_root: Path,
    branch: str,
    *,
    limit: int = 20,
) -> list[dict[str, Any]]:
    """Return recent commits on *branch* as a list of dicts.

    Each dict has: ``hash``, ``short_hash``, ``subject``, ``body``,
    ``author_name``, ``author_email``, ``date``, ``task_refs``.
    """
    # Use %x00 as field separator, %x01 as record separator
    fmt = "%H%x00%h%x00%s%x00%b%x00%an%x00%ae%x00%aI%x01"
    try:
        result = _run_git(
            ["log", f"--max-count={limit}", f"--format={fmt}", branch, "--"],
            cwd=repo_root,
        )
        if result.returncode != 0:
            return []
    except (subprocess.TimeoutExpired, OSError):
        return []

    commits: list[dict[str, Any]] = []
    for record in result.stdout.split("\x01"):
        record = record.strip()
        if not record:
            continue
        fields = record.split("\x00")
        if len(fields) < 7:
            continue

        full_hash, short_hash, subject, body, author_name, author_email, date = (
            fields[0],
            fields[1],
            fields[2],
            fields[3].strip(),
            fields[4],
            fields[5],
            fields[6],
        )

        # Extract task references from subject + body
        full_message = f"{subject} {body}"
        task_refs = extract_task_refs(full_message)

        commits.append(
            {
                "hash": full_hash,
                "short_hash": short_hash,
                "subject": subject,
                "body": body if body else None,
                "author_name": author_name,
                "author_email": author_email,
                "date": date,
                "task_refs": task_refs,
            }
        )
    return commits


def get_commit_count(repo_root: Path) -> int | None:
    """Return total commit count, or None on error."""
    try:
        result = _run_git(["rev-list", "--count", "HEAD"], cwd=repo_root)
        if result.returncode == 0:
            return int(result.stdout.strip())
    except (subprocess.TimeoutExpired, OSError, ValueError):
        pass
    return None


def get_remote_url(repo_root: Path) -> str | None:
    """Return the URL of the 'origin' remote, or None."""
    try:
        result = _run_git(["remote", "get-url", "origin"], cwd=repo_root)
        if result.returncode == 0:
            url = result.stdout.strip()
            return url if url else None
    except (subprocess.TimeoutExpired, OSError):
        pass
    return None


# ---------------------------------------------------------------------------
# Task reference extraction
# ---------------------------------------------------------------------------


def extract_task_refs(text: str) -> list[str]:
    """Extract Lattice task references from a commit message.

    Finds short IDs (e.g., ``LAT-42``) and ULID task IDs
    (e.g., ``task_01HQABC...``).  Returns deduplicated list.
    """
    refs: list[str] = []
    seen: set[str] = set()

    for match in _SHORT_ID_RE.finditer(text):
        ref = match.group(1)
        if ref not in seen:
            refs.append(ref)
            seen.add(ref)

    for match in _ULID_RE.finditer(text):
        ref = match.group(1)
        if ref not in seen:
            refs.append(ref)
            seen.add(ref)

    return refs


# ---------------------------------------------------------------------------
# Summary & caching
# ---------------------------------------------------------------------------

# Module-level cache.  Keyed by repo root path string.
_summary_cache: dict[str, tuple[float, str, dict[str, Any]]] = {}
# Each entry: (timestamp, etag, summary_dict)


def _compute_etag(summary: dict[str, Any]) -> str:
    """Compute an ETag from the summary's branch data."""
    # Build a deterministic fingerprint from branch names + tip hashes
    parts: list[str] = []
    for b in summary.get("branches", []):
        parts.append(f"{b['name']}:{b['commit_hash']}")
    raw = "|".join(parts)
    return hashlib.md5(raw.encode()).hexdigest()[:12]


def get_git_summary(lattice_dir: Path) -> tuple[dict[str, Any], str]:
    """Build the full git summary payload for ``GET /api/git``.

    Returns ``(summary_dict, etag_string)``.

    Uses a module-level cache with a 30-second TTL.  The summary is
    re-computed only when the cache expires.
    """
    if not git_available():
        summary: dict[str, Any] = {
            "available": False,
            "reason": "git_not_installed",
        }
        return summary, ""

    # lattice_dir is .lattice/ — repo root is its parent
    repo_root = find_git_root(lattice_dir.parent)
    if repo_root is None:
        summary = {
            "available": False,
            "reason": "not_a_git_repo",
        }
        return summary, ""

    cache_key = str(repo_root)
    now = time.monotonic()

    # Check cache
    if cache_key in _summary_cache:
        cached_time, cached_etag, cached_summary = _summary_cache[cache_key]
        if now - cached_time < CACHE_TTL_SECONDS:
            return cached_summary, cached_etag

    # Build fresh summary
    branches = get_branches(repo_root)
    current_branch = get_current_branch(repo_root)
    commit_count = get_commit_count(repo_root)
    remote_url = get_remote_url(repo_root)

    summary = {
        "available": True,
        "repo_root": str(repo_root),
        "current_branch": current_branch,
        "branch_count": len(branches),
        "commit_count": commit_count,
        "remote_url": remote_url,
        "branches": branches,
    }

    etag = _compute_etag(summary)

    # Store in cache
    _summary_cache[cache_key] = (now, etag, summary)

    return summary, etag


def invalidate_cache() -> None:
    """Clear the summary cache.  Useful for testing."""
    _summary_cache.clear()
