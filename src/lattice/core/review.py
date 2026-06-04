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
#      ``--triggered-by``) the child calls
#      ``claim_review_state(..., auto_fired=False)``. The standard stale-PID
#      reclaim path overwrites the parent's dead record with the child's
#      live PID. ``auto_fired=False`` here is intentional: ``review_state``
#      is transient coordination state; the durable "this review was
#      auto-fired" signal lives in the ``auto_review_spawned`` event in the
#      task's event log.
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


def record_agent_failure(lattice_dir: Path, agent_type: str, task_id: str) -> int:
    """Record that an agent failed a review. Returns the total failure count for this agent."""
    state_dir = lattice_dir / REVIEW_STATE_DIR
    state_dir.mkdir(exist_ok=True)
    path = _failures_path(lattice_dir)
    entry = json.dumps(
        {
            "agent": agent_type,
            "task_id": task_id,
            "timestamp": _now_iso(),
        },
        sort_keys=True,
        separators=(",", ":"),
    )
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


def create_failure_diagnostic_task(
    lattice_dir: Path,
    agent_type: str,
    failure_count: int,
    actor: str,
) -> str | None:
    """Create a needs_human-flagged task for investigating persistent agent failures.

    Returns the created task ID, or None on failure.
    """
    title = f"Investigate {agent_type} review failures — failed {failure_count} times"
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
) -> str | None:
    """Record failure and create diagnostic task if threshold exceeded.

    Returns the diagnostic task ID if one was created.
    """
    count = record_agent_failure(lattice_dir, agent_type, task_id)
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


def resolve_diff(
    lattice_dir: Path,
    task_id: str,
    snapshot: dict,
    base: str | None = None,
) -> tuple[bool, str]:
    """Resolve the git diff for a task.

    Returns (success, diff_or_error_message).

    Resolution chain:
    1. --base provided → git diff <base>...HEAD
    2. Branch-link exists → git diff <base_branch>...HEAD
    3. Scan git log for task short ID in commit messages → diff that range
    4. Commits by assigned actor since task moved to in_progress → diff
    5. Error suggesting --base
    """
    repo_root = _find_git_root(lattice_dir)
    if repo_root is None:
        return False, "Not inside a git repository."

    # Step 0: explicit --base
    if base is not None:
        diff = _git_diff(repo_root, f"{base}...HEAD")
        if diff is not None:
            return True, diff
        return False, f"git diff {base}...HEAD failed. Check that '{base}' is a valid ref."

    # Step 1: branch-link
    branch_links = snapshot.get("branch_links", [])
    if branch_links:
        branch = branch_links[-1]["branch"]
        # Try to find merge-base with main/master
        base_branch = _find_base_branch(repo_root)
        diff = _git_diff(repo_root, f"{base_branch}...{branch}")
        if diff is not None:
            return True, diff

    # Step 2: task short ID in git log
    short_id = _get_short_id(snapshot)
    if short_id:
        commit_range = _find_commits_by_message(repo_root, short_id)
        if commit_range:
            diff = _git_diff(repo_root, commit_range)
            if diff is not None:
                return True, diff

    # Step 3: commits by assigned actor since in_progress
    assigned_to = snapshot.get("assigned_to")
    in_progress_time = _find_status_change_time(snapshot, "in_progress")
    if assigned_to and in_progress_time:
        actor_name = _extract_actor_name(assigned_to)
        diff = _git_diff_by_author(repo_root, actor_name, since=in_progress_time)
        if diff is not None:
            return True, diff

    return False, (
        "Could not resolve diff automatically. "
        "Use --base <ref> to specify the base commit or branch."
    )


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
        result = subprocess.run(
            ["git", "rev-parse", "--verify", branch],
            cwd=str(repo_root),
            capture_output=True,
            text=True,
        )
        if result.returncode == 0:
            return branch
    return "main"


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


def _find_commits_by_message(repo_root: Path, short_id: str) -> str | None:
    """Find git commit range where messages contain short_id."""
    result = subprocess.run(
        ["git", "log", "--oneline", f"--grep={short_id}", "--format=%H"],
        cwd=str(repo_root),
        capture_output=True,
        text=True,
    )
    if result.returncode != 0 or not result.stdout.strip():
        return None
    commits = result.stdout.strip().splitlines()
    if not commits:
        return None
    # Diff from the oldest matching commit's parent to HEAD
    oldest = commits[-1]
    return f"{oldest}^...HEAD"


def _git_diff_by_author(repo_root: Path, author: str, since: str) -> str | None:
    """Get diff of commits by author since a given ISO timestamp."""
    result = subprocess.run(
        [
            "git",
            "log",
            "--format=%H",
            f"--author={author}",
            f"--since={since}",
        ],
        cwd=str(repo_root),
        capture_output=True,
        text=True,
    )
    if result.returncode != 0 or not result.stdout.strip():
        return None
    commits = result.stdout.strip().splitlines()
    if not commits:
        return None
    oldest = commits[-1]
    return _git_diff(repo_root, f"{oldest}^...HEAD")


def _get_short_id(snapshot: dict) -> str | None:
    """Extract the short ID from task snapshot."""
    return snapshot.get("short_id")


def _find_status_change_time(snapshot: dict, target_status: str) -> str | None:
    """Find the timestamp when a task last entered target_status."""
    # Not available directly in snapshot — return updated_at as fallback
    # The caller uses this for git --since, so updated_at is a reasonable proxy
    return snapshot.get("updated_at")


def _extract_actor_name(actor: str | dict) -> str:
    """Extract a usable name from an actor string or dict."""
    if isinstance(actor, dict):
        return actor.get("name", "")
    # actor is "prefix:identifier", extract identifier
    if ":" in actor:
        return actor.split(":", 1)[1]
    return actor


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
            _handle_agent_failure(lattice_dir, "claude", task_id, actor_str)
            clear_review_state(lattice_dir, task_id)
            return False, _format_legacy_error("claude", result, timeout), None

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
) -> str:
    """Build the prompt handed to the claude session running inside the c11 pane.

    The pane's job: run ``/trident-{code|plan}-review``, read the resulting
    artifact, triage findings, and advance the task. See the Review Verdict
    Routing section in CLAUDE.md for the triage protocol.
    """
    review_short = "code" if review_type == "code-review" else "plan"
    base_line = base_branch or "main"
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
| PASS, fixes done, PR exists        | pr_open                               |
| PASS, no PR yet                    | open PR (`gh pr create`), then pr_open|
| FAIL impl-level                    | in_progress (rework, then re-review)  |
| FAIL plan-level                    | in_planning                           |
| Complex finding(s)                 | keep status, set needs-human flag     |
| 3-cycle safety valve tripped       | keep status, set needs-human flag     |

Use `lattice status {task_short_id} <new_status> --actor agent:trident-pane-{task_short_id}`,
or `lattice needs-human {task_short_id} "<what you need>"` for the flag rows.

## Identity

- Actor: `agent:trident-pane-{task_short_id}`
- Cwd: `{worktree}` (you share the delegator's worktree)
- Base branch: `{base_line}`

When you've advanced the task to its terminal state for this cycle, exit cleanly.
"""


def run_triple_review(
    lattice_dir: Path,
    task_id: str,
    review_type: str,
    actor: str | dict,
    *,
    base: str | None = None,
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
