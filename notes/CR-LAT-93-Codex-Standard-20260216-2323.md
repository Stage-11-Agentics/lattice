## Code Review
- **Date:** 2026-02-16 23:23 EST
- **Model:** Codex (GPT-5)
- **Branch:** main
- **Latest Commit:** ad31c96
- **Linear Story:** LAT-93 (context-provided; branch name `main` has no `AUT-###` token)
---

### Quality Gate
- **Blocker:** Required Phase 2 command sequence stopped at step 1.
- `npm run type-check` failed with `ENOENT` (no `package.json`).
- **Failure counts:** 1 command failure, 0 type errors reported (tooling did not run), lint/tests not executed due stop-on-first-failure rule.

### Architectural Assessment
This commit introduces a strong foundation for worker lifecycle tracking (`process_started` / `process_completed` / `process_failed`) and commit-isolated execution via detached worktrees. The main architectural risks are around lifecycle closure and concurrency safety: started processes can remain active indefinitely, and concurrent `worker run` calls can race and overwrite each other's snapshot state.

iOS/Android parity is not applicable for this backend/CLI-only change.

### Tactical Assessment
Core task/event mutation coverage is good for the new process event types. The largest implementation gaps are in orchestration (`src/lattice/cli/worker_cmds.py`) where there are no direct tests, and where process finalization paths depend on behavior not currently enforced by the prompt/runtime wiring.

### Change Flow
1. `lattice worker run` resolves task + worker definition and checks `active_processes` (`src/lattice/cli/worker_cmds.py:271`, `src/lattice/cli/worker_cmds.py:297`).
2. It creates a `process_started` event and optionally a detached worktree (`src/lattice/cli/worker_cmds.py:321`, `src/lattice/cli/worker_cmds.py:335`).
3. It appends the event and writes updated snapshot (`src/lattice/cli/worker_cmds.py:342`, `src/lattice/cli/worker_cmds.py:349`).
4. It spawns a detached worker process with cleaned env (`src/lattice/cli/worker_cmds.py:359`, `src/lattice/cli/worker_cmds.py:362`).
5. Only synchronous spawn exceptions post `process_failed`; normal completion depends on worker-side follow-up (`src/lattice/cli/worker_cmds.py:364`, `src/lattice/cli/worker_cmds.py:255`).

### Findings
1. **[Blocker] ✅ Confirmed** `CodeReviewHeavy` is declared but cannot run in this commit because required files are missing.
   - Evidence: `workers/code-review-heavy.json:6` references `./workers/code-review-heavy.sh` and `workers/code-review-heavy.json:7` references `prompts/workers/code-review-heavy.md`, but neither file exists in `ad31c96`.
   - Validation: `git cat-file -e ad31c96:workers/code-review-heavy.sh` and `git cat-file -e ad31c96:prompts/workers/code-review-heavy.md` both fail.
   - Risk: The worker starts as a detached process that immediately fails at runtime, leaving lifecycle state ambiguous.

2. **[Blocker] ✅ Confirmed** Process lifecycle cannot reliably transition from started to completed/failed for successful spawns, causing stuck `active_processes` entries.
   - Evidence: `process_started` is always persisted before spawn (`src/lattice/cli/worker_cmds.py:321`, `src/lattice/cli/worker_cmds.py:349`), but `process_failed` is only emitted on synchronous spawn exceptions (`src/lattice/cli/worker_cmds.py:364`).
   - Evidence: default worker prompt instructs artifact attach but no command to emit `process_completed`/`process_failed` (`prompts/workers/code-review-lite.md:70`, `prompts/workers/code-review-lite.md:82`).
   - Impact: dedup logic blocks future runs when stale active entries remain (`src/lattice/cli/worker_cmds.py:297`).

3. **[Important] ✅ Confirmed** `worker run` has a race condition that can allow duplicate starts and snapshot lost updates under concurrency.
   - Evidence: dedup check and `updated_snapshot` are computed from a pre-lock snapshot (`src/lattice/cli/worker_cmds.py:277`, `src/lattice/cli/worker_cmds.py:297`, `src/lattice/cli/worker_cmds.py:342`), then written inside lock without re-reading latest state (`src/lattice/cli/worker_cmds.py:345`).
   - Impact: two concurrent invocations can both pass dedup and one write can overwrite the other's active process list.

4. **[Important] ✅ Confirmed** Worktree isolation is only partial because prompt resolution comes from the mutable main checkout, not the frozen worktree.
   - Evidence: prompt path is built from `project_root` (`src/lattice/cli/worker_cmds.py:164`) while command `cwd` is worktree (`src/lattice/cli/worker_cmds.py:189`).
   - Impact: if prompt files change after launch, worker behavior can drift from the commit being reviewed.

5. **[Important] ✅ Confirmed** Test coverage does not exercise `worker_cmds` orchestration paths.
   - Evidence: this commit adds a 460-line new CLI module (`src/lattice/cli/worker_cmds.py`) but tests only cover event/task mutation schemas (`tests/test_core/test_events.py:41`, `tests/test_core/test_tasks.py:892`, `tests/test_core/test_properties.py:60`).
   - Missing cases: spawn success/failure lifecycle completion, worktree creation/removal failures, dedup under concurrency, `worker ps` output correctness.

6. **[Potential] ❓ Uncertain / needs discussion** `timeout_minutes` is stored in `process_started` events (`src/lattice/cli/worker_cmds.py:328`) but omitted from `active_processes` entries (`src/lattice/core/tasks.py:281`), which may complicate timeout-driven stale detection without event-log reads.

7. **[Potential] ⬇️ Lower priority, valid non-blocking** Worktrees are cleaned up on spawn failure (`src/lattice/cli/worker_cmds.py:383`) but not on normal completion, so `/tmp/lattice-worker-*` directories can accumulate.
