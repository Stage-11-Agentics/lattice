"""Core review logic: diff resolution, agent spawning, artifact storage.

The agent-spawning primitive lives in ``lattice.core.agent_spawn`` (with the
``HeadlessBackend`` in ``lattice.storage.agent_spawn`` and detached backends
under ``lattice.integrations``). This module composes that primitive into
the review-specific orchestration (single + triple + merge) and keeps the
backward-compatible ``spawn_agent`` shim for any out-of-tree callers.
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
    spawn_many,
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
    """Create a needs_human task for investigating persistent agent failures.

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
        # Move to needs_human
        subprocess.run(
            [
                "lattice",
                "status",
                new_task_id,
                "needs_human",
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
    their own per-agent scratch dirs and don't expect a cmux/terminal pane)
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
    *,
    headless: bool = False,
    backend_force: str | None = None,
) -> tuple[bool, str, str | None]:
    """Run a single-agent review via ``agent_spawn.spawn_one``.

    Returns ``(success, message, output_text_or_None)`` — unchanged from the
    pre-LAT-205 contract. ``headless`` / ``backend_force`` are pass-through
    overrides for the new spawn primitive; defaults preserve today's auto
    selection behavior.
    """
    started_at = _now_iso()
    state: dict[str, Any] = {
        "task_id": task_id,
        "mode": "single",
        "review_type": review_type,
        "started_at": started_at,
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
            force=backend_force,
            headless=headless,
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


def run_triple_review(
    lattice_dir: Path,
    task_id: str,
    review_type: str,
    prompt_content: str,
    actor: str | dict,
    timeout: int = DEFAULT_AGENT_TIMEOUT,
    *,
    headless: bool = False,
    backend_force: str | None = None,
) -> tuple[bool, str, list[tuple[str, bool, str]]]:
    """Run a triple-agent review (Claude, Codex, Gemini) in parallel via ``spawn_many``.

    Returns ``(overall_success, message, [(agent_name, success, text), ...])``
    — unchanged from the pre-LAT-205 contract. ``headless`` / ``backend_force``
    are pass-through overrides for the new spawn primitive.
    """
    agents = ["claude", "codex", "gemini"]
    overall_started = _now_iso()
    state: dict[str, Any] = {
        "task_id": task_id,
        "mode": "triple",
        "review_type": review_type,
        "started_at": overall_started,
        "agents": [
            {"name": a, "status": "running", "started_at": overall_started, "artifact_id": None}
            for a in agents
        ],
    }
    write_review_state(lattice_dir, state)

    tmp = _make_prompt_dir(lattice_dir, prefix=f"{review_type}-")
    try:
        requests: list[SpawnRequest] = []
        for agent in agents:
            agent_dir = tmp / agent
            agent_dir.mkdir()
            prompt_file = agent_dir / "prompt.md"
            output_file = agent_dir / "output.md"
            prompt_file.write_text(prompt_content, encoding="utf-8")
            requests.append(
                SpawnRequest(
                    agent=agent,
                    prompt_file=prompt_file,
                    output_file=output_file,
                    label=f"{review_type} :: {agent}",
                    timeout_seconds=timeout,
                )
            )

        spawn_results = spawn_many(
            requests,
            workspace_label=f"{review_type}-{task_id}",
            force=backend_force,
            headless=headless,
        )

        # Preserve legacy result ordering (claude, codex, gemini).
        by_agent = {r.agent: r for r in spawn_results}
        results: list[tuple[str, bool, str]] = []
        finished_at = _now_iso()
        for idx, agent in enumerate(agents):
            r = by_agent.get(agent)
            if r is None:
                results.append((agent, False, "no result returned"))
                state["agents"][idx]["status"] = "failed"
            elif r.success:
                results.append((agent, True, r.output_text))
                state["agents"][idx]["status"] = "done"
            else:
                results.append((agent, False, r.error))
                state["agents"][idx]["status"] = "failed"
            state["agents"][idx]["finished_at"] = finished_at
        write_review_state(lattice_dir, state)

        actor_str = _extract_actor_str(actor)
        for agent, success, _text in results:
            if not success:
                _handle_agent_failure(lattice_dir, agent, task_id, actor_str)

        any_success = any(r[1] for r in results)
        clear_review_state(lattice_dir, task_id)
        return any_success, "Triple review complete.", results
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def build_merge_prompt(
    task_id: str,
    reviews: list[tuple[str, bool, str]],
    review_type: str,
) -> str:
    """Build the merge prompt for the Opus consolidation agent."""
    sections = []
    for agent, success, text in reviews:
        if success:
            sections.append(f"## Review from {agent}\n\n{text}")
        else:
            sections.append(f"## Review from {agent}\n\n*(failed or timed out)*")

    reviews_text = "\n\n---\n\n".join(sections)

    return f"""# Merge Review: {task_id}

You are consolidating three independent code reviews into one authoritative summary.
Your job is to synthesize the findings, surface the most important issues, and produce
a clear verdict. Do not simply concatenate — identify patterns, prioritize by severity,
and resolve any contradictions between reviewers.

## Individual Reviews

{reviews_text}

## Output Format

Produce a merged review with these sections:
1. **Verdict**: PASS / FAIL (implementation-level) / FAIL (plan-level)
2. **Synthesis**: 3-5 sentences covering overall quality, patterns across reviews, key findings
3. **Issues**: Consolidated list ordered by severity. For issues found by multiple reviewers, merge them.
4. **Positive Observations**: What all or most reviewers praised.
5. **Reviewer Agreement**: Brief note on where reviewers agreed and disagreed.

Write the merged review to: {{output_path}}
"""


def run_merge_agent(
    lattice_dir: Path,
    task_id: str,
    reviews: list[tuple[str, bool, str]],
    review_type: str,
    *,
    headless: bool = False,
    backend_force: str | None = None,
) -> tuple[bool, str]:
    """Run the Claude Opus merge agent via ``agent_spawn.spawn_one``.

    Returns ``(success, merged_text_or_error)`` — unchanged contract.
    ``headless`` / ``backend_force`` pass through to the spawn primitive.
    """
    prompt = build_merge_prompt(task_id, reviews, review_type)

    tmp = _make_prompt_dir(lattice_dir, prefix="merge-")
    try:
        agent_dir = tmp / "merge"
        agent_dir.mkdir()
        prompt_file = agent_dir / "prompt.md"
        output_file = agent_dir / "output.md"

        filled = prompt.replace("{output_path}", str(output_file))
        prompt_file.write_text(filled, encoding="utf-8")

        request = SpawnRequest(
            agent="claude",
            prompt_file=prompt_file,
            output_file=output_file,
            label=f"{review_type} :: merge",
            timeout_seconds=DEFAULT_AGENT_TIMEOUT,
        )
        result = spawn_one(
            request,
            workspace_label=f"merge-{task_id}",
            force=backend_force,
            headless=headless,
        )
        if result.success:
            return True, result.output_text
        return False, _format_legacy_error("claude", result, DEFAULT_AGENT_TIMEOUT)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


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
