"""Core review logic: diff resolution, agent spawning, artifact storage.

The agent-spawning primitive lives in ``lattice.core.agent_spawn`` (with the
``HeadlessBackend`` in ``lattice.storage.agent_spawn`` and detached backends
under ``lattice.integrations``). This module composes that primitive into
the review-specific orchestration:

- Single mode (``run_single_review``): one headless ``claude -p`` subprocess.
- Triple mode (``run_triple_review``, LAT-218): one new c11 pane sibling to
  the caller, running ``/trident-{code|plan}-review``. Fire-and-forget — the
  pane owns trident, triage, and the task-status advance.

The legacy ``spawn_agent`` shim is kept for any out-of-tree callers that
still expect the pre-LAT-205 contract.
"""

from __future__ import annotations

import glob as glob_mod
import json
import os
import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from lattice.core.agent_spawn import (
    SpawnRequest,
    SpawnResult,
    spawn_one,
)


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

DEFAULT_AGENT_TIMEOUT = 600  # 10 minutes
FAILURE_THRESHOLD = 2  # auto-create diagnostic task after this many failures

#: Default ceiling on diff lines embedded in a review prompt. Generous on
#: purpose — a real large change still gets a full review; this only guards
#: against a pathologically large diff (e.g. a resolution fallback that sweeps
#: in unrelated merged work) ballooning the prompt and cost. Configurable via
#: ``review_max_diff_lines``.
DEFAULT_MAX_DIFF_LINES = 5000

#: Default ceiling on diff *characters* embedded in a review prompt. The line
#: cap alone does not bound prompt size — 5000 lines of a wide diff (lockfiles,
#: minified assets, generated code) measured at ~200k-500k characters in the
#: field, and prompt size is the dominant term in review wall-clock: a 14.5k
#: prompt returned in 45s where a 209k prompt took 232s of the 600s budget.
#: Configurable via ``review_max_diff_chars``; non-positive disables the cap.
DEFAULT_MAX_DIFF_CHARS = 120_000

REVIEW_STATE_DIR = "review_state"
TMP_PROMPTS_DIR = "tmp-prompts"
FAILURES_FILE = "failures.jsonl"


# ---------------------------------------------------------------------------
# Prompt temp directory helpers
# ---------------------------------------------------------------------------


def _make_prompt_dir(lattice_dir: Path, prefix: str) -> Path:
    """Create a unique prompt directory inside ``.lattice/tmp-prompts/``.

    Using a directory inside the project tree (rather than system temp) ensures
    sub-agents can read/write the files regardless of sandbox restrictions.
    The caller is responsible for cleanup (see ``cleanup_prompt_dirs``).
    """
    base = lattice_dir / TMP_PROMPTS_DIR
    base.mkdir(exist_ok=True)
    return Path(tempfile.mkdtemp(prefix=prefix, dir=base))


def cleanup_prompt_dirs(lattice_dir: Path) -> int:
    """Remove all directories under ``.lattice/tmp-prompts/``.

    Returns the number of directories removed.
    """
    import shutil

    base = lattice_dir / TMP_PROMPTS_DIR
    if not base.exists():
        return 0
    removed = 0
    for child in base.iterdir():
        if child.is_dir():
            shutil.rmtree(child, ignore_errors=True)
            removed += 1
    return removed


# ---------------------------------------------------------------------------
# In-flight state helpers
# ---------------------------------------------------------------------------
#
# ``review_state`` lifecycle for the auto-fire path (LAT-211).
#
# The ``review_state/<task_id>.json`` record is the single source of truth for
# "is a review in flight for this task?". For the auto-fire workflow it is
# written and re-written at three sites; understanding the order matters
# because ``auto_fired`` (and ``started_by_pid``) shifts at each step.
#
# 1. **Parent (``status_cmd`` → ``cli.auto_review.auto_fire_review``).**  When
#    the operator runs ``lattice status <id> review`` (or ``planned``), the
#    parent process synchronously calls ``claim_review_state`` *before* it
#    spawns the detached ``lattice code-review`` / ``plan-review`` subprocess.
#    On success the record carries ``auto_fired=True`` and
#    ``started_by_pid=<parent pid>``. The parent then ``Popen``s the child and
#    exits.
#
# 2. **Child CLI body (``code_review`` / ``plan_review``).**  The detached
#    review subprocess starts and reads the existing record. Two paths:
#
#    * **Adoption (parent-still-alive edge).**  If ``--triggered-by`` was
#      passed AND the existing record has ``auto_fired=True`` AND
#      ``started_by_pid == os.getppid()`` (the parent is still alive on the
#      same machine), the child writes a new record directly with
#      ``auto_fired=True`` and ``started_by_pid=os.getpid()``. This bypasses
#      ``claim_review_state``'s live-PID refusal — the child is taking over
#      the parent's claim, not contending with a stranger.
#
#    * **Normal claim.**  Otherwise (no record, stale parent PID, or no
#      ``--triggered-by``) the child calls ``claim_review_state`` with
#      ``auto_fired=(triggered_by is not None)``. The standard stale-PID
#      reclaim path overwrites the parent's dead record with the child's
#      live PID, and ``--triggered-by`` — passed only by the auto-fire path —
#      carries the provenance across the handoff. The durable audit signal
#      still lives in the ``auto_review_spawned`` event; this keeps the
#      transient record from contradicting it.
#
# 3. **``run_single_review`` / ``run_triple_review``.**  Once inside the
#    review orchestrator the existing in-place ``write_review_state`` calls
#    update ``agents[*].status`` and timestamps as agents progress, then
#    ``clear_review_state`` removes the record on exit. These calls preserve
#    whatever ``started_by_pid`` and ``auto_fired`` the CLI body wrote in
#    step 2 — they never recompute them.
#
# Manual ``lattice code-review`` / ``plan-review`` invocations follow the
# normal-claim path with ``auto_fired=False`` from the start.


def _state_path(lattice_dir: Path, task_id: str) -> Path:
    return lattice_dir / REVIEW_STATE_DIR / f"{task_id}.json"


def write_review_state(lattice_dir: Path, state: dict) -> None:
    """Persist in-flight review state atomically."""
    state_dir = lattice_dir / REVIEW_STATE_DIR
    state_dir.mkdir(exist_ok=True)
    path = _state_path(lattice_dir, state["task_id"])
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, indent=2) + "\n", encoding="utf-8")
    tmp.replace(path)


def read_review_state(lattice_dir: Path, task_id: str) -> dict | None:
    """Read in-flight review state, or None if not found."""
    path = _state_path(lattice_dir, task_id)
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None


def clear_review_state(lattice_dir: Path, task_id: str) -> None:
    """Remove in-flight review state after completion."""
    path = _state_path(lattice_dir, task_id)
    path.unlink(missing_ok=True)


def pid_alive(pid: int) -> bool:
    """Return True if ``pid`` refers to a live process on this machine.

    Uses ``os.kill(pid, 0)`` (signal 0) which performs the kernel's existence
    check without delivering a signal. ``PermissionError`` is treated as
    "alive" — the process exists but we lack permission to signal it
    (different uid, sandbox boundary). Non-positive PIDs are never alive.
    """
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


def claim_review_state(
    lattice_dir: Path,
    task_id: str,
    *,
    mode: str,
    review_type: str,
    started_by_pid: int,
    auto_fired: bool,
) -> tuple[bool, dict | None]:
    """Best-effort claim of the in-flight review slot for ``task_id``.

    Reads the existing record. If it carries a different live ``started_by_pid``
    the claim is refused and the existing record is returned unchanged. If
    no record exists, the holder PID is dead, or the holder is the caller
    itself, the slot is reclaimed: a fresh record is written with the supplied
    ``mode``, ``review_type``, ``started_by_pid``, and ``auto_fired`` values
    (and an empty ``agents`` list — the orchestrator fills it in later).

    Returns ``(True, written_state)`` on success or ``(False, existing_state)``
    on contention.

    Note: this is *not* a true compare-and-swap. ``write_review_state`` does
    an atomic temp-file replace, but the read-decide-write window is not
    locked. Two callers passing the read check within microseconds will both
    write — last writer wins. The realistic race is handled in §5 of the
    LAT-211 plan (see module-level docstring); the residual race spawns
    duplicate work but never corrupts state.
    """
    existing = read_review_state(lattice_dir, task_id)
    if existing is not None:
        holder = existing.get("started_by_pid")
        if isinstance(holder, int) and holder != started_by_pid and pid_alive(holder):
            return False, existing
        # Otherwise: stale (no PID field, dead PID, or our own PID) — reclaim.

    new_state: dict[str, Any] = {
        "task_id": task_id,
        "mode": mode,
        "review_type": review_type,
        "started_at": _now_iso(),
        "started_by_pid": started_by_pid,
        "auto_fired": auto_fired,
        "agents": [],
    }
    write_review_state(lattice_dir, new_state)
    return True, new_state


# ---------------------------------------------------------------------------
# Persistent failure tracking
# ---------------------------------------------------------------------------


def _failures_path(lattice_dir: Path) -> Path:
    return lattice_dir / REVIEW_STATE_DIR / FAILURES_FILE


def record_agent_failure(
    lattice_dir: Path,
    agent_type: str,
    task_id: str,
    *,
    detail: dict | None = None,
) -> int:
    """Record that an agent failed a review. Returns the total failure count.

    ``detail`` carries diagnostic fields captured from the failed run — the
    error message, returncode, duration, the resolved command, prompt size,
    and a tail of the agent's stderr/stdout. Without this the record is just
    ``{agent, task_id, timestamp}``, which is why six identical timeouts were
    never diagnosable. Unknown/None detail values are dropped so the line
    stays compact. ``agent``/``task_id``/``timestamp`` are always present and
    win over any same-named detail key.
    """
    state_dir = lattice_dir / REVIEW_STATE_DIR
    state_dir.mkdir(exist_ok=True)
    path = _failures_path(lattice_dir)
    record: dict[str, Any] = {}
    if detail:
        record.update({k: v for k, v in detail.items() if v not in (None, "")})
    record["agent"] = agent_type
    record["task_id"] = task_id
    record["timestamp"] = _now_iso()
    entry = json.dumps(record, sort_keys=True, separators=(",", ":"))
    with open(path, "a", encoding="utf-8") as f:
        f.write(entry + "\n")
    return count_agent_failures(lattice_dir, agent_type)


def count_agent_failures(lattice_dir: Path, agent_type: str) -> int:
    """Count how many times an agent has failed reviews on this board."""
    path = _failures_path(lattice_dir)
    if not path.exists():
        return 0
    count = 0
    try:
        for line in path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            try:
                entry = json.loads(line)
                if entry.get("agent") == agent_type:
                    count += 1
            except json.JSONDecodeError:
                continue
    except OSError:
        return 0
    return count


def last_failure_for_task(lattice_dir: Path, task_id: str) -> dict | None:
    """Return the most recent ``failures.jsonl`` entry for ``task_id``, or None.

    Lets ``review-status`` surface a failed review even when the ``review_state``
    record is gone — e.g. a failure recorded by an older code path that cleared
    state, or a record overwritten by a later attempt. The file is append-only
    and chronological, so the last matching line is the most recent failure.
    """
    path = _failures_path(lattice_dir)
    if not path.exists():
        return None
    latest: dict | None = None
    try:
        for line in path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            try:
                entry = json.loads(line)
            except json.JSONDecodeError:
                continue
            if entry.get("task_id") == task_id:
                latest = entry
    except OSError:
        return None
    return latest


#: Stable title prefix for auto-filed diagnostic tasks. Dedup keys on this so
#: one root cause yields one open ticket instead of a new one per failure.
DIAGNOSTIC_TITLE_PREFIX = "Investigate"
_DIAGNOSTIC_TITLE_SUFFIX = "review failures"


def _open_diagnostic_task_exists(lattice_dir: Path, agent_type: str) -> bool:
    """Return True if an unresolved diagnostic task for ``agent_type`` already exists.

    Scans the needs-human queue (every diagnostic task is filed flagged) for a
    title matching ``Investigate <agent> review failures``. Best-effort: any
    error means "assume none" so a transient failure never *suppresses* a real
    escalation — at worst it files one extra ticket, which is the safe default.
    """
    try:
        result = subprocess.run(
            ["lattice", "list", "--needs-human", "--json"],
            capture_output=True,
            text=True,
            cwd=str(lattice_dir.parent),
        )
        if result.returncode != 0:
            return False
        payload = json.loads(result.stdout or "{}")
    except (OSError, json.JSONDecodeError):
        return False
    data = payload.get("data", payload)
    tasks = data.get("tasks", data) if isinstance(data, dict) else data
    if not isinstance(tasks, list):
        return False
    needle = f"{DIAGNOSTIC_TITLE_PREFIX} {agent_type} {_DIAGNOSTIC_TITLE_SUFFIX}"
    for task in tasks:
        if isinstance(task, dict) and str(task.get("title", "")).startswith(needle):
            return True
    return False


def create_failure_diagnostic_task(
    lattice_dir: Path,
    agent_type: str,
    failure_count: int,
    actor: str,
) -> str | None:
    """Create a needs_human-flagged task for investigating persistent agent failures.

    Returns the created task ID, or None on failure (or when a diagnostic task
    for this agent is already open — dedup, so one root cause doesn't escalate
    into a ladder of near-identical tickets).
    """
    if _open_diagnostic_task_exists(lattice_dir, agent_type):
        return None
    title = (
        f"{DIAGNOSTIC_TITLE_PREFIX} {agent_type} {_DIAGNOSTIC_TITLE_SUFFIX} "
        f"— failed {failure_count} times"
    )
    try:
        result = subprocess.run(
            [
                "lattice",
                "create",
                title,
                "--actor",
                actor,
                "--quiet",
            ],
            capture_output=True,
            text=True,
            cwd=str(lattice_dir.parent),
        )
        if result.returncode != 0:
            return None
        new_task_id = result.stdout.strip()
        if not new_task_id:
            return None
        # Flag for human attention (the task stays in backlog)
        subprocess.run(
            [
                "lattice",
                "needs-human",
                new_task_id,
                f"{agent_type} review agent failed {failure_count} times — investigate",
                "--actor",
                actor,
            ],
            capture_output=True,
            text=True,
            cwd=str(lattice_dir.parent),
        )
        return new_task_id
    except OSError:
        return None


def _handle_agent_failure(
    lattice_dir: Path,
    agent_type: str,
    task_id: str,
    actor: str,
    *,
    detail: dict | None = None,
) -> str | None:
    """Record failure and create diagnostic task if threshold exceeded.

    Returns the diagnostic task ID if one was created.
    """
    count = record_agent_failure(lattice_dir, agent_type, task_id, detail=detail)
    if count >= FAILURE_THRESHOLD:
        return create_failure_diagnostic_task(lattice_dir, agent_type, count, actor)
    return None


# ---------------------------------------------------------------------------
# Temp file cleanup
# ---------------------------------------------------------------------------


def cleanup_temp_files(task_id: str | None = None, lattice_dir: Path | None = None) -> int:
    """Remove lattice review temp files from both system temp and ``.lattice/tmp-prompts/``.

    If task_id is provided, only removes files whose content contains the task_id.
    Otherwise removes all matching files.

    Returns the number of items removed.
    """
    import shutil

    removed = 0

    # Legacy: clean system temp (may still have leftovers from older runs)
    tmp_root = tempfile.gettempdir()
    patterns = [
        os.path.join(tmp_root, "lattice-review-*"),
        os.path.join(tmp_root, "lattice-merge-*"),
    ]
    for agent in ("claude", "codex", "gemini"):
        patterns.append(os.path.join(tmp_root, f"lattice-{agent}-*"))

    for pattern in patterns:
        for path_str in glob_mod.glob(pattern):
            path = Path(path_str)
            try:
                if path.is_dir():
                    shutil.rmtree(path, ignore_errors=True)
                else:
                    path.unlink(missing_ok=True)
                removed += 1
            except OSError:
                continue

    # New: clean .lattice/tmp-prompts/
    if lattice_dir is not None:
        removed += cleanup_prompt_dirs(lattice_dir)

    return removed


# ---------------------------------------------------------------------------
# Diff resolution
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class DiffResolution:
    """What a review actually diffed — the range, the SHAs, and the tree.

    Every field describes the *resolved* range, not the caller's cwd, so the
    evidence headers a review carries can be derived from this object alone.
    ``source`` records which rung of head selection won: ``"explicit"`` (an
    explicit ``--head``), ``"linked_branch"`` (the task's last branch link), or
    ``"head"`` (the ambient ``HEAD``, only when the task has no branch link).
    """

    success: bool
    diff: str = ""
    error: str | None = None
    error_code: str | None = None
    base_ref: str | None = None
    head_ref: str | None = None
    base_sha: str | None = None
    head_sha: str | None = None
    worktree: Path | None = None
    source: str | None = None
    warning: str | None = None

    @property
    def range_desc(self) -> str | None:
        """``<base>...<head>`` when both refs are known, else ``None``."""
        if self.base_ref and self.head_ref:
            return f"{self.base_ref}...{self.head_ref}"
        return None


def resolve_diff(
    lattice_dir: Path,
    task_id: str,
    snapshot: dict,
    base: str | None = None,
    head: str | None = None,
    worktree: Path | None = None,
) -> DiffResolution:
    """Resolve the git diff for a task, naming exactly what was diffed.

    The review must see the *branch's* changes even when it runs from a
    checkout whose ``HEAD`` is not the branch under review — the common case
    in a worktree-per-ticket model, where ``.lattice/`` lives in the main
    checkout (``HEAD`` == ``main``) while the ticket's code lives on a feature
    branch in a separate worktree. Because worktrees share the object store,
    a ref-based three-dot diff ``<base>...<branch>`` resolves the real changes
    from any checkout.

    **Head selection — the linked branch is authoritative.**

    1. ``--head`` when given.
    2. Else the task's last linked branch. If a branch link exists but does
       not resolve in this repo, this **fails loudly**: reviewing a different
       tree is worse than reviewing nothing.
    3. Else — no ``--head`` and no branch link at all — the ambient ``HEAD``.

    There is no scan-for-something-plausible fallback. A ladder that silently
    substitutes another ticket's commits produces confident PASS verdicts on
    code nobody read; the error message is the feature.

    **Base selection — the remote default branch, never a bare local branch.**

    A local ``main`` nobody pulls is routinely behind ``origin/main``, and a
    three-dot diff against it drags in every sibling ticket merged since the
    last pull. Candidates are ``origin/HEAD`` > ``origin/main`` >
    ``origin/master`` > local ``main``/``master``; the one whose merge-base
    with the head is the *descendant* of the others wins, so an unfetched
    remote degrades gracefully instead of over-including. No ``git fetch`` is
    ever run — a review must not mutate refs or block on the network.

    An **empty** diff is never accepted as success.
    """
    repo_root = worktree if worktree is not None else _find_git_root(lattice_dir)
    if repo_root is None:
        return DiffResolution(
            success=False,
            error="Not inside a git repository.",
            error_code="NO_GIT_REPO",
        )

    # An explicit --base/--head that doesn't resolve is a caller error worth
    # naming precisely, rather than burying it in a generic failure.
    if base is not None and not _ref_exists(repo_root, base):
        return DiffResolution(
            success=False,
            error=f"Base ref '{base}' does not resolve in {repo_root}. Check the ref name.",
            error_code="BASE_REF_UNRESOLVABLE",
            worktree=repo_root,
        )
    if head is not None and not _ref_exists(repo_root, head):
        return DiffResolution(
            success=False,
            error=f"Head ref '{head}' does not resolve in {repo_root}. Check the ref name.",
            error_code="HEAD_REF_UNRESOLVABLE",
            worktree=repo_root,
        )

    # --- head ----------------------------------------------------------------
    linked = _linked_branch(snapshot)
    if head is not None:
        head_ref, source = head, "explicit"
    elif linked:
        if not _ref_exists(repo_root, linked):
            display_id = snapshot.get("short_id") or task_id
            return DiffResolution(
                success=False,
                error=(
                    f"HEAD_REF_UNRESOLVABLE: task {display_id} is linked to branch "
                    f"'{linked}', which does not resolve in {repo_root}. Fetch it, pass "
                    f"--worktree <path> to diff from a checkout that has it, or pass "
                    f"--head <ref>. Refusing to review a different tree."
                ),
                error_code="HEAD_REF_UNRESOLVABLE",
                worktree=repo_root,
            )
        head_ref, source = linked, "linked_branch"
    else:
        head_ref, source = "HEAD", "head"

    # --- base ----------------------------------------------------------------
    base_ref, base_sha, warning = _resolve_base_ref(repo_root, head_ref, base)
    head_sha = _rev_parse(repo_root, head_ref)

    ref_range = f"{base_ref}...{head_ref}"
    diff = _git_diff(repo_root, ref_range)
    common = {
        "base_ref": base_ref,
        "head_ref": head_ref,
        "base_sha": base_sha,
        "head_sha": head_sha,
        "worktree": repo_root,
        "source": source,
        "warning": warning,
    }
    if diff is None:
        return DiffResolution(
            success=False,
            error=(
                f"git diff failed for range '{ref_range}' in {repo_root}. "
                f"Pass --base/--head to name the range explicitly."
            ),
            error_code="DIFF_FAILED",
            **common,
        )
    if not diff.strip():
        return DiffResolution(
            success=False,
            error=(
                f"Diff for '{ref_range}' is empty — no changes on this range. "
                f"The head is most likely already merged into the base (or identical to it); "
                f"pass --base <merge-base> to review it anyway. "
                f"If the code under review lives elsewhere, pass --base/--head to name "
                f"the range, or --worktree <path> to diff from that checkout. "
                f"Refusing to review an empty diff."
            ),
            error_code="EMPTY_DIFF",
            **common,
        )

    return DiffResolution(success=True, diff=diff, **common)


def _resolve_base_ref(
    repo_root: Path, head_ref: str, explicit_base: str | None = None
) -> tuple[str, str | None, str | None]:
    """Pick the base ref for ``<base>...<head_ref>``.

    Returns ``(base_ref, base_sha, warning)`` where ``base_sha`` is the SHA of
    the merge-base actually used. ``explicit_base`` wins unconditionally.
    """
    if explicit_base is not None:
        return explicit_base, _merge_base(repo_root, explicit_base, head_ref), None

    candidates: list[str] = []
    origin_head = _origin_head_ref(repo_root)
    if origin_head:
        candidates.append(origin_head)
    for name in ("origin/main", "origin/master", "main", "master"):
        if name not in candidates:
            candidates.append(name)

    best_ref: str | None = None
    best_sha: str | None = None
    for candidate in candidates:
        if not _ref_exists(repo_root, candidate):
            continue
        merge_base = _merge_base(repo_root, candidate, head_ref)
        if merge_base is None:
            continue
        if best_sha is None:
            best_ref, best_sha = candidate, merge_base
        elif merge_base != best_sha and _is_ancestor(repo_root, best_sha, merge_base):
            # This candidate's merge-base is a descendant of the incumbent's —
            # a tighter, still-honest range.
            best_ref, best_sha = candidate, merge_base

    warning = _stale_remote_warning(repo_root)
    if best_ref is None:
        # No candidate shares history with the head (or no refs at all).
        return _find_base_branch(repo_root), None, warning
    return best_ref, best_sha, warning


def _stale_remote_warning(repo_root: Path) -> str | None:
    """Report when the remote-tracking default branch has drifted from the local one.

    Fires whenever ``origin/<default>`` is not an ancestor of local
    ``<default>`` — behind *or* diverged. Diverged is the common shape on a
    board checkout whose local ``main`` carries commits the remote never saw
    while the remote moved on independently, and it is exactly where a reader
    wants to know how old the ref is.

    The base comes from the remote-tracking ref, so this states the observed
    fact rather than prescribing a fix. ``resolve_diff`` never fetches; a
    ``git fetch`` is what refreshes the ref the base is taken from.
    """
    remote = _origin_head_ref(repo_root) or "origin/main"
    local = remote.split("/", 1)[1] if "/" in remote else "main"
    if not _ref_exists(repo_root, remote) or not _ref_exists(repo_root, local):
        return None
    remote_sha = _rev_parse(repo_root, remote)
    local_sha = _rev_parse(repo_root, local)
    if not remote_sha or not local_sha or remote_sha == local_sha:
        return None
    if _is_ancestor(repo_root, remote_sha, local_sha):
        return (
            f"{remote} is behind local {local} — the base is read from {remote}, "
            f"which review never fetches; 'git fetch' refreshes it."
        )
    if not _is_ancestor(repo_root, local_sha, remote_sha):
        return (
            f"{remote} and local {local} have diverged — the base is read from "
            f"{remote}, which review never fetches; 'git fetch' refreshes it."
        )
    return None


def _origin_head_ref(repo_root: Path) -> str | None:
    """Return e.g. ``origin/main`` from ``refs/remotes/origin/HEAD``, or None."""
    result = subprocess.run(
        ["git", "symbolic-ref", "--quiet", "--short", "refs/remotes/origin/HEAD"],
        cwd=str(repo_root),
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        return None
    ref = result.stdout.strip()
    return ref or None


def _rev_parse(repo_root: Path, ref: str) -> str | None:
    """Resolve ``ref`` to a 40-char commit SHA, or None."""
    result = subprocess.run(
        ["git", "rev-parse", "--verify", "--quiet", f"{ref}^{{commit}}"],
        cwd=str(repo_root),
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        return None
    return result.stdout.strip() or None


def _merge_base(repo_root: Path, a: str, b: str) -> str | None:
    """Return the merge-base SHA of two refs, or None when they share none."""
    result = subprocess.run(
        ["git", "merge-base", a, b],
        cwd=str(repo_root),
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        return None
    return result.stdout.strip() or None


def _is_ancestor(repo_root: Path, ancestor: str, descendant: str) -> bool:
    """True when ``ancestor`` is reachable from ``descendant``."""
    return (
        subprocess.run(
            ["git", "merge-base", "--is-ancestor", ancestor, descendant],
            cwd=str(repo_root),
            capture_output=True,
        ).returncode
        == 0
    )


def _range_suffix(range_desc: str | None) -> str:
    return f" (range: {range_desc})" if range_desc else ""


def cap_diff(
    diff: str,
    max_lines: int = DEFAULT_MAX_DIFF_LINES,
    range_desc: str | None = None,
) -> tuple[str, bool, int]:
    """Cap a diff to ``max_lines``, truncating with a visible marker if over.

    Returns ``(possibly_truncated_diff, was_capped, original_line_count)``.

    A non-positive ``max_lines`` disables the cap (returns the diff unchanged).
    When truncated, a clear marker is appended so the review agent — and any
    human reading the artifact — knows the diff was cut, by how much, and over
    *which range*. A truncation warning on a small ticket is the cheapest
    available signal that the range is wrong, which is only legible if the
    marker names the range.
    """
    if max_lines <= 0:
        lines = diff.count("\n") + (1 if diff and not diff.endswith("\n") else 0)
        return diff, False, lines
    lines = diff.splitlines()
    original = len(lines)
    if original <= max_lines:
        return diff, False, original
    kept = "\n".join(lines[:max_lines])
    omitted = original - max_lines
    marker = (
        f"\n\n[diff truncated by Lattice: showing first {max_lines} of {original} "
        f"lines; {omitted} lines omitted{_range_suffix(range_desc)}. Review the most "
        f"significant changes above; if the change is genuinely this large, narrow the "
        f"diff with --base or raise review_max_diff_lines.]\n"
    )
    return kept + marker, True, original


def cap_diff_chars(
    diff: str,
    max_chars: int = DEFAULT_MAX_DIFF_CHARS,
    range_desc: str | None = None,
) -> tuple[str, bool, int]:
    """Cap a diff to ``max_chars``, truncating with a visible marker if over.

    Returns ``(possibly_truncated_diff, was_capped, original_char_count)``.

    Complements :func:`cap_diff`: a line cap bounds how many hunks the reviewer
    sees, a character cap bounds how big the prompt actually gets. Only the
    second one bounds review latency, so both are applied. Truncation happens on
    a line boundary where one exists inside the budget, so the agent never reads
    half a diff line. A non-positive ``max_chars`` disables the cap.
    """
    original = len(diff)
    if max_chars <= 0 or original <= max_chars:
        return diff, False, original
    kept = diff[:max_chars]
    boundary = kept.rfind("\n")
    if boundary > 0:
        kept = kept[:boundary]
    omitted = original - len(kept)
    marker = (
        f"\n\n[diff truncated by Lattice: showing first {len(kept)} of {original} "
        f"characters; {omitted} characters omitted{_range_suffix(range_desc)}. Review the "
        f"most significant changes above; if the change is genuinely this large, narrow "
        f"the diff with --base or raise review_max_diff_chars.]\n"
    )
    return kept + marker, True, original


def is_review_abandoned(state: dict) -> bool:
    """True when an in-flight review record's owning process is gone.

    A review subprocess that is killed — machine sleep, terminal closed, an
    orchestrator reaping its children — never reaches its own failure handler,
    so it writes no terminal ``status`` and no ``failures.jsonl`` line. The
    record it leaves behind says ``agents[0].status == "running"`` forever, and
    every reader reports the review as still in flight. Detecting the dead
    holder PID is what turns that silence back into a signal.
    """
    if not isinstance(state, dict):
        return False
    if state.get("status") in ("failed", "done", "abandoned"):
        return False
    holder = state.get("started_by_pid")
    if not isinstance(holder, int):
        return False
    return not pid_alive(holder)


def _find_git_root(lattice_dir: Path) -> Path | None:
    """Walk up from lattice_dir to find the git root."""
    current = lattice_dir.parent
    for _ in range(20):
        if (current / ".git").exists():
            return current
        parent = current.parent
        if parent == current:
            return None
        current = parent
    return None


def _find_base_branch(repo_root: Path) -> str:
    """Return the likely base branch (main or master)."""
    for branch in ("main", "master"):
        if _ref_exists(repo_root, branch):
            return branch
    return "main"


def _ref_exists(repo_root: Path, ref: str) -> bool:
    """Return True if ``ref`` resolves to a commit in ``repo_root``.

    Uses ``rev-parse --verify <ref>^{commit}`` so a branch name, tag, or SHA
    all validate, while a path or bogus string does not. Worktrees share the
    object store, so a feature branch checked out in a *sibling* worktree still
    resolves from the main checkout — which is exactly what lets a ref-based
    diff see the branch's changes from any checkout.
    """
    result = subprocess.run(
        ["git", "rev-parse", "--verify", "--quiet", f"{ref}^{{commit}}"],
        cwd=str(repo_root),
        capture_output=True,
        text=True,
    )
    return result.returncode == 0


def _linked_branch(snapshot: dict) -> str | None:
    """Return the most recently linked branch for a task, or None."""
    branch_links = snapshot.get("branch_links") or []
    if not branch_links:
        return None
    last = branch_links[-1]
    if isinstance(last, dict):
        return last.get("branch")
    return last if isinstance(last, str) else None


def _git_diff(repo_root: Path, ref: str) -> str | None:
    """Run git diff and return the output, or None on failure."""
    result = subprocess.run(
        ["git", "diff", ref],
        cwd=str(repo_root),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    if result.returncode == 0:
        return result.stdout
    return None


# ---------------------------------------------------------------------------
# Agent spawning
# ---------------------------------------------------------------------------


def spawn_agent(
    agent_type: str,
    prompt_file: Path,
    output_file: Path,
    timeout: int = DEFAULT_AGENT_TIMEOUT,
) -> tuple[bool, str]:
    """Backwards-compatible shim that delegates to ``agent_spawn.spawn_one``.

    Returns ``(success, output_text_or_error)`` to preserve the legacy
    contract for any out-of-tree callers. New code should call
    ``lattice.core.agent_spawn.spawn_one`` directly.

    Always uses the headless backend so existing call sites (which build
    their own per-agent scratch dirs and don't expect a c11/terminal pane)
    behave identically to the legacy implementation.
    """
    from lattice.storage.agent_spawn import HeadlessBackend

    request = SpawnRequest(
        agent=agent_type,
        prompt_file=prompt_file,
        output_file=output_file,
        label=f"shim :: {agent_type}",
        timeout_seconds=timeout,
    )
    result = spawn_one(
        request,
        workspace_label=f"shim-{agent_type}",
        backend=HeadlessBackend(),
    )
    if result.success:
        return True, result.output_text
    return False, _format_legacy_error(agent_type, result, timeout)


def _format_legacy_error(agent_type: str, result: SpawnResult, timeout: int) -> str:
    """Match the message shapes that legacy callers parse on failure."""
    err = result.error or ""
    if "timed out" in err:
        return f"Agent '{agent_type}' timed out after {timeout}s"
    if err.startswith("Unknown agent type"):
        return err
    if "produced no output" in err or "no output" in err:
        return f"Agent '{agent_type}' produced no output."
    return f"Agent '{agent_type}': {err}"


# ---------------------------------------------------------------------------
# Review orchestration
# ---------------------------------------------------------------------------


def run_single_review(
    lattice_dir: Path,
    task_id: str,
    review_type: str,
    prompt_content: str,
    actor: str | dict,
    timeout: int = DEFAULT_AGENT_TIMEOUT,
    worktree: Path | None = None,
) -> tuple[bool, str, str | None]:
    """Run a single-agent review via ``agent_spawn.spawn_one``.

    Always headless: single-mode reviews never claim a c11 surface or a
    terminal window. The agent runs in a ``subprocess.run`` and the CLI
    blocks until it finishes. Returns ``(success, message,
    output_text_or_None)``.
    """
    from lattice.storage.agent_spawn import HeadlessBackend

    started_at = _now_iso()
    # Preserve fields written by an earlier ``claim_review_state`` call (e.g.
    # by the CLI body — see module docstring). If no record exists or the
    # caller never went through ``claim_review_state``, fall back to defaults.
    existing = read_review_state(lattice_dir, task_id) or {}
    state: dict[str, Any] = {
        "task_id": task_id,
        "mode": "single",
        "review_type": review_type,
        "started_at": started_at,
        "started_by_pid": existing.get("started_by_pid", os.getpid()),
        "auto_fired": existing.get("auto_fired", False),
        "agents": [
            {"name": "claude", "status": "running", "started_at": started_at, "artifact_id": None}
        ],
    }
    write_review_state(lattice_dir, state)

    tmp = _make_prompt_dir(lattice_dir, prefix="review-")
    agent_dir = tmp / "claude"
    agent_dir.mkdir()
    prompt_file = agent_dir / "prompt.md"
    output_file = agent_dir / "output.md"
    prompt_file.write_text(prompt_content, encoding="utf-8")

    try:
        request = SpawnRequest(
            agent="claude",
            prompt_file=prompt_file,
            output_file=output_file,
            label=f"{review_type} :: claude",
            timeout_seconds=timeout,
            cwd=worktree,
        )
        result = spawn_one(
            request,
            workspace_label=f"{review_type}-{task_id}",
            backend=HeadlessBackend(),
        )

        finished_at = _now_iso()
        state["agents"][0]["status"] = "done" if result.success else "failed"
        state["agents"][0]["finished_at"] = finished_at
        write_review_state(lattice_dir, state)

        if not result.success:
            actor_str = _extract_actor_str(actor)
            message = _format_legacy_error("claude", result, timeout)
            detail = {
                "error": result.error or message,
                "review_type": review_type,
                "returncode": result.returncode,
                "duration_seconds": round(result.duration_seconds, 1),
                "command": result.command,
                "prompt_chars": len(prompt_content),
                "stderr_tail": result.stderr_tail,
            }
            _handle_agent_failure(lattice_dir, "claude", task_id, actor_str, detail=detail)
            # Leave a durable, observable record instead of clearing it: a failed
            # review must surface in `review-status`, not vanish into "no review
            # found". We intentionally store NO review artifact — a failed review
            # must not satisfy the `done` completion gate. A lingering failed
            # record never blocks the next review: its started_by_pid is the dead
            # child, so claim_review_state reclaims the slot (dead-PID → stale).
            state["status"] = "failed"
            state["error"] = result.error or message
            state["finished_at"] = finished_at
            state["detail"] = {
                "returncode": result.returncode,
                "duration_seconds": round(result.duration_seconds, 1),
                "stderr_tail": result.stderr_tail,
            }
            write_review_state(lattice_dir, state)
            return False, message, None

        clear_review_state(lattice_dir, task_id)
        return True, "Review complete.", result.output_text
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def build_trident_handoff_prompt(
    task_short_id: str,
    review_type: str,
    *,
    worktree: Path,
    base_branch: str | None,
    head_ref: str | None = None,
    head_sha: str | None = None,
) -> str:
    """Build the prompt handed to the claude session running inside the c11 pane.

    The pane's job: run ``/trident-{code|plan}-review``, read the resulting
    artifact, triage findings, and advance the task. See the Review Verdict
    Routing section in CLAUDE.md for the triage protocol.

    The prompt names the resolved range, base *and* head. The pane's cwd is the
    caller's checkout, whose ``HEAD`` is frequently not the branch under review
    — left to infer, the pane diffs the wrong tree.
    """
    review_short = "code" if review_type == "code-review" else "plan"
    base_line = base_branch or "main"
    head_line = head_ref or "HEAD"
    if head_sha:
        head_line = f"{head_line} ({head_sha})"
    range_line = f"{base_line}...{head_ref}" if head_ref else f"{base_line}...HEAD"
    return f"""# Triple {review_type} for {task_short_id}

You're the agent running inside a c11 pane spawned by the LAT-218 review
primitive. Your job: run the trident review, triage findings, advance the
task. When you're done, exit cleanly.

## Step 1 — Run trident

Type at the claude prompt:

    /trident-{review_short}-review {task_short_id}

The trident skill will spawn several agents in parallel, merge their
findings, and store an artifact attached to {task_short_id}. Wait for it
to complete.

## Step 2 — Read the result

When trident reports done, find the merged artifact under
`.lattice/artifacts/payload/<id>.md`. Read it — it gives a verdict (PASS,
FAIL implementation-level, FAIL plan-level) and a list of findings.

## Step 3 — Triage per Review Verdict Routing

Per the Lattice skill section `## Review Verdict Routing`, every finding
goes into one of three buckets:

  - **Obvious** (missing AC, plan bugs, trivial fixes) → fix inline with
    Edit/Write.
  - **Evolutionary** (scope creep, "while we're at it") → skip with
    `lattice comment {task_short_id} "Skipping [finding]: [reason]" \
--actor agent:trident-pane-{task_short_id}`.
  - **Complex** (real design questions) → flag for a human:
    `lattice needs-human {task_short_id} "<what you need>" \
--actor agent:trident-pane-{task_short_id}` (task keeps its status).

## Step 4 — Advance task

| Outcome                            | Move task to                          |
| ---------------------------------- | ------------------------------------- |
| PASS, fixes done                   | in_validation (run e2e validation,    |
|                                    | record `--role validation` evidence,  |
|                                    | then open the PR and move to pr_open) |
| FAIL impl-level                    | in_progress (rework, then re-review)  |
| FAIL plan-level                    | in_planning                           |
| Complex finding(s)                 | keep status, set needs-human flag     |
| 3-cycle safety valve tripped       | keep status, set needs-human flag     |

Use `lattice status {task_short_id} <new_status> --actor agent:trident-pane-{task_short_id}`,
or `lattice needs-human {task_short_id} "<what you need>"` for the flag rows.

## Identity

- Actor: `agent:trident-pane-{task_short_id}`
- Cwd: `{worktree}` (you share the delegator's worktree)
- Base ref: `{base_line}`
- Head ref: `{head_line}`

## The range under review

Diff exactly `{range_line}` — this range is already resolved for you. Do not
diff the cwd's `HEAD`: on a board checkout it is not the branch under review,
and reviewing it is how a review ends up reading the wrong tree.

When you've advanced the task to its terminal state for this cycle, exit cleanly.
"""


def run_triple_review(
    lattice_dir: Path,
    task_id: str,
    review_type: str,
    actor: str | dict,
    *,
    base: str | None = None,
    head: str | None = None,
    head_sha: str | None = None,
    short_id: str | None = None,
    worktree: Path | None = None,
) -> tuple[bool, str]:
    """Spawn a c11 pane that runs /trident-{type}-review and applies fixes inline.

    Fire-and-forget. The spawned pane owns the trident run, the artifact
    storage, finding triage, and the task-status advance — this function
    returns as soon as the pane is up.

    Returns ``(True, message)`` after the pane has been spawned, or
    ``(False, error_message)`` if anything prevented the spawn (most
    commonly: not running inside c11).
    """
    from lattice.cli.c11_bridge import c11_available
    from lattice.integrations.c11 import spawn_one_in_current_workspace

    if not c11_available():
        return (
            False,
            "triple mode requires c11 — run from inside a c11 surface, or use --mode single.",
        )

    display_id = short_id or task_id
    wt = worktree if worktree is not None else lattice_dir.parent

    prompt_text = build_trident_handoff_prompt(
        display_id,
        review_type,
        worktree=wt,
        base_branch=base,
        head_ref=head,
        head_sha=head_sha,
    )
    tab_title = f"{display_id} :: trident {review_type}"
    description = (
        f"Trident {review_type} for {display_id}. The pane drives /trident-"
        f"{'code' if review_type == 'code-review' else 'plan'}-review, triages findings, "
        f"and advances task status. Sibling pane spawned by Lattice (LAT-218)."
    )

    ok, ref = spawn_one_in_current_workspace(
        prompt_text,
        tab_title=tab_title,
        description=description,
        cwd=wt,
    )
    if not ok:
        return False, f"failed to spawn c11 pane: {ref}"

    started_at = _now_iso()
    existing = read_review_state(lattice_dir, task_id) or {}
    state: dict[str, Any] = {
        "task_id": task_id,
        "mode": "triple",
        "review_type": review_type,
        "started_at": started_at,
        "started_by_pid": existing.get("started_by_pid", os.getpid()),
        "started_by_actor": _extract_actor_str(actor),
        "auto_fired": existing.get("auto_fired", False),
        "pane_ref": ref,
        "agents": [
            {
                "name": "trident-pane",
                "status": "running",
                "started_at": started_at,
                "pane_ref": ref,
            }
        ],
    }
    write_review_state(lattice_dir, state)

    return (
        True,
        f"Triple review running in {ref} — task status is the sync primitive.",
    )


# ---------------------------------------------------------------------------
# Utilities
# ---------------------------------------------------------------------------


def _extract_actor_str(actor: str | dict) -> str:
    """Extract a flat actor string suitable for --actor flags."""
    if isinstance(actor, str):
        return actor
    if isinstance(actor, dict):
        return actor.get("name") or actor.get("base_name") or "system:lattice"
    return "system:lattice"


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()
