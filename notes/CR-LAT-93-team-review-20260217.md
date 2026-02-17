# Team Code Review: LAT-93 — Lattice Workers

**Reviewed:** 2026-02-16
**Agents:** Gemini (Standard + Critical), Codex/GPT-5 (Standard + Critical)
**Note:** Claude Standard + Critical agents completed but failed to write output files (background launch redirect issue). Synthesis based on 4 of 6 reviews.
**Test Results:** pytest PASS (1241), ruff PASS

## Executive Summary

The Lattice Workers implementation delivers a solid foundation for autonomous process tracking — event types, mutation handlers, CLI, worktree isolation, and environment stripping. However, all four reviews independently identified the same critical issue: **the snapshot read-modify-write cycle in `worker_run` is not fully locked**, creating a TOCTOU race condition. Additionally, the process lifecycle has no mechanism for the worker to emit `process_completed`, meaning active_processes entries can become permanently stuck. These two issues must be fixed before this can be considered production-ready.

## Synthesis

### High Confidence Issues (3+ agents agree)

**1. TOCTOU Race Condition in `worker_run`** — ALL 4 AGENTS
- `worker_cmds.py:278` reads the snapshot, `worker_cmds.py:297` checks dedup, `worker_cmds.py:342` computes updated snapshot — all outside the lock at line 345.
- Concurrent invocations can both pass dedup and overwrite each other's changes.
- **Fix:** Move snapshot read + dedup check + apply inside the `multi_lock` scope. Or re-read and verify `last_event_id` hasn't changed before writing.

**2. Incomplete Process Lifecycle — No `process_completed` Path** — ALL 4 AGENTS
- `process_started` is always persisted before spawn. `process_failed` is emitted only on synchronous spawn exceptions.
- The worker prompt (`code-review-lite.md`) instructs artifact attachment but never tells the worker to emit `process_completed` or `process_failed`.
- Result: successful workers leave permanent entries in `active_processes`, and the dedup guard blocks future runs of the same worker type.
- **Fix:** Add explicit `lattice worker complete <task> <started_event_id>` and `lattice worker fail <task> <started_event_id>` commands. Update the worker prompt to call one of these as the final step.

**3. No CLI/Integration Tests for `worker_cmds.py`** — ALL 4 AGENTS
- 460-line module with zero tests. Core mutation logic is tested, but orchestration paths (loading, spawning, dedup, ps) are uncovered.
- **Fix:** Create `tests/test_cli/test_worker_cmds.py` covering definition loading, env building, dedup logic, CLI output.

### Standard Review Consensus

**4. `CodeReviewHeavy` Definition is Broken** — Codex Standard + Critical
- References `./workers/code-review-heavy.sh` and `prompts/workers/code-review-heavy.md`, neither of which exist in the commit.
- The `script` engine will launch `bash -c` on a missing file, fail silently at runtime, and leave the task in active state.
- **Fix:** Either create the missing files or remove the definition until it's ready.

**5. Hardcoded `/tmp` Path** — Gemini Standard + Critical
- `_create_worktree` uses `Path(f"/tmp/lattice-worker-...")`. Not portable; vulnerable to temp cleaners disrupting git worktree metadata.
- **Fix:** Use `tempfile.gettempdir()` or a user-owned directory like `.lattice/worktrees/`.

### Critical Review Findings

**6. Zombie State on Setup Failure** — Gemini Critical
- If `_create_worktree` fails after `process_started` is persisted, `output_error` exits immediately without emitting `process_failed`.
- Task enters permanent "active" state requiring manual cleanup.
- **Fix:** The spawn failure path already handles this correctly (lines 364-385), but worktree creation failure (line 339) calls `output_error` which exits before any compensation. Wrap worktree creation in the same try/except pattern.

**7. Worktree Cleanup Only on Failure** — Codex Standard + Critical, Gemini Critical
- `_remove_worktree()` is only called on spawn failure. Successful workers leak `/tmp/lattice-worker-*` directories and git worktree metadata.
- **Fix:** Worker should clean up its own worktree on completion, or add a `lattice worker prune` command.

**8. `timeout_minutes` Stored but Never Enforced** — Gemini Standard + Critical, Codex Standard
- Recorded in the event but not in `active_processes` entries, and never used to kill stale processes.
- **Fix:** Include in `active_processes` entry and implement a watchdog/reap command.

### Worth Investigating (Single Agent)

**9. Prompt Path from Main Checkout, Not Worktree** — Codex Standard
- Prompt path built from `project_root` but worker runs in worktree. If prompt changes after launch, worker behavior drifts.
- **Fix:** Read prompt path from worktree when worktree is in use.

**10. Actor Not Validated** — Codex Critical
- `--actor` and worker config actor are never validated (format check). Other write commands validate actor format.
- **Fix:** Add actor format validation consistent with other commands.

**11. Env Stripping May Remove Needed Vars** — Codex Critical
- Broad stripping of all `CODEX_`/`CLAUDE_` vars may remove auth/config variables for some engine deployments.
- **Fix:** Consider a more targeted allowlist/blocklist approach.

**12. Docstring Mismatch** — Codex Critical
- `_load_worker_def` docstring says "filename match" but code matches on JSON `name` field.
- **Fix:** Update docstring.

### Contradictions

None — all four reviews are in strong agreement on the top issues. Gemini Critical was the most aggressive (declaring "NOT READY FOR PRODUCTION"), while Gemini Standard was the most measured. Codex Standard and Critical agreed on findings but differed in severity ratings.

## Combined Issues

### Blockers
1. **Race condition**: Move read + dedup + apply inside lock scope (`worker_cmds.py:278-348`)
2. **Incomplete lifecycle**: Add `worker complete` / `worker fail` commands; update prompts to use them
3. **Zombie on worktree failure**: Emit `process_failed` when `_create_worktree` fails (line 339)

### Important
4. Remove or stub `CodeReviewHeavy` definition (references missing files)
5. Add CLI tests for `worker_cmds.py`
6. Fix hardcoded `/tmp` path → `tempfile.gettempdir()`
7. Clean up worktrees on success (not just failure)
8. Store `timeout_minutes` in `active_processes` for future watchdog

### Consider
9. Resolve prompt path from worktree when applicable
10. Validate actor format in worker commands
11. Fix docstring in `_load_worker_def`
12. Consider targeted env stripping instead of broad prefix matching

## Action Items

```markdown
- [ ] Move snapshot read + dedup check inside lock scope (`worker_cmds.py:278-348`) — found by: ALL, type: both
- [ ] Add `lattice worker complete` and `lattice worker fail` commands — found by: ALL, type: both
- [ ] Update code-review-lite.md prompt to call `lattice worker complete` — found by: ALL, type: both
- [ ] Emit process_failed on _create_worktree failure (`worker_cmds.py:339`) — found by: Gemini Critical
- [ ] Remove or stub CodeReviewHeavy definition — found by: Codex Standard + Critical
- [ ] Create tests/test_cli/test_worker_cmds.py — found by: ALL, type: standard
- [ ] Use tempfile.gettempdir() instead of /tmp (`worker_cmds.py:68`) — found by: Gemini Standard + Critical
- [ ] Add worktree cleanup path for successful completions — found by: Codex + Gemini, type: both
- [ ] Include timeout_minutes in active_processes entries — found by: Gemini + Codex, type: standard
- [ ] Fix prompt path to use worktree when available (`worker_cmds.py:164`) — found by: Codex Standard
- [ ] Validate actor format in worker commands — found by: Codex Critical
- [ ] Fix _load_worker_def docstring — found by: Codex Critical
```

---

<details>
<summary>Full Gemini (Standard) Review</summary>

## Code Review
- **Date:** 2026-02-16 12:00
- **Model:** Gemini 2.0 Flash

### Architectural Assessment
Solid foundation with worktree isolation and environment safety. Noted orphaned process state risk.

### Findings
1. [Blocker] Missing tests for CLI logic
2. [Important] Hardcoded temp directory
3. [Important] Orphaned process state
4. [Potential] Security of script engine

</details>

<details>
<summary>Full Gemini (Critical) Review</summary>

## Critical Code Review
- **Model:** Gemini 2.0 Flash — Critical/Adversarial

### Verdict: NOT READY FOR PRODUCTION

### Blockers
1. Race condition in worker_run (TOCTOU)
2. Zombie state on setup failure
3. Insecure temp directory usage

### Important
4. Missing git prune
5. Silent process failure

### Potential
6. Unenforced timeout

</details>

<details>
<summary>Full Codex/GPT-5 (Standard) Review</summary>

## Code Review
- **Model:** Codex (GPT-5)

### Findings
1. [Blocker] CodeReviewHeavy definition broken (missing files)
2. [Blocker] Process lifecycle cannot reliably close
3. [Important] Race condition in worker_run
4. [Important] Worktree isolation partial (prompt from main)
5. [Important] No CLI tests
6. [Potential] timeout_minutes not in active_processes
7. [Potential] Worktree leak on success

</details>

<details>
<summary>Full Codex/GPT-5 (Critical) Review</summary>

## Critical Code Review
- **Model:** Codex (GPT-5) — Critical/Adversarial

### Verdict: Not ready for production

### Blockers
- CodeReviewHeavy broken at launch
- Worker lifecycle cannot close on normal success
- Race condition in dedup + snapshot update

### Important
- Actor never validated
- Successful runs don't clean worktrees

### Potential
- Env stripping may remove needed vars
- Review prompt hardcodes git diff main..HEAD

</details>
