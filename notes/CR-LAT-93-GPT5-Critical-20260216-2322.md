## Critical Code Review
- **Date:** 2026-02-16 23:22 EST
- **Model:** Codex (GPT-5) — `gpt-5`
- **Branch:** main
- **Latest Commit:** ad31c96
- **Linear Story:** LAT-93
- **Review Type:** Critical/Adversarial
---

**The Ugly Truth**
This implementation is not production-safe. The worker lifecycle is incomplete, concurrency control is unsafe, and one of the shipped worker definitions is broken on day one. The first successful run can leave tasks permanently "running," and concurrent invocations can race into duplicate starts and stale snapshot writes. Also, Phase 2 command execution failed immediately (`npm run type-check`) because this repository has no `package.json`, so required type/lint/test gates were not executed.

**Phase 2 Execution (Required Commands)**
- `npm run type-check` -> failed (`ENOENT`), 1 command failure, 9 `npm error` lines.
- `npm run lint` -> not run (stopped on first failure per instructions).
- `npm test` -> not run (stopped on first failure per instructions).

**What Will Break**
- Running `CodeReviewHeavy` will fail immediately because it references files that do not exist (`workers/code-review-heavy.json:6`, `workers/code-review-heavy.json:7`).
- A worker that exits without explicitly writing `process_completed`/`process_failed` will remain in `active_processes` forever, and reruns of that worker type will be blocked (`src/lattice/cli/worker_cmds.py:297`).
- Two `lattice worker run` calls at nearly the same time can both pass dedup checks and both write competing snapshots due to pre-lock reads (`src/lattice/cli/worker_cmds.py:278`, `src/lattice/cli/worker_cmds.py:297`, `src/lattice/cli/worker_cmds.py:342`).
- Successful worker runs leak git worktrees because cleanup runs only on spawn failure (`src/lattice/cli/worker_cmds.py:384`).

**What's Missing**
- No CLI/integration tests for `lattice worker run/list/ps` paths (there are only core mutation tests in `tests/test_core/test_tasks.py`).
- No test proving active process lifecycle closes correctly on success/failure.
- No test for concurrent `worker run` dedup behavior under lock contention.
- No validation test for missing `prompt_file`/`command` assets in worker definitions.
- No test for worktree cleanup after normal completion.

**The Nits**
- `_load_worker_def` docstring says filename matching, but implementation matches JSON `name` field (`src/lattice/cli/worker_cmds.py:29`).
- `command` exists in non-`script` worker JSON but is ignored by code (`workers/code-review-lite.json:6` vs `src/lattice/cli/worker_cmds.py:166`).

1. **Blockers**
- ✅ Confirmed — `CodeReviewHeavy` definition is broken at launch. `workers/code-review-heavy.json` points to `./workers/code-review-heavy.sh` and `prompts/workers/code-review-heavy.md`, but neither file exists in this commit (`workers/code-review-heavy.json:6`, `workers/code-review-heavy.json:7`). Execution trace: `_spawn_worker()` launches `bash -c ./workers/code-review-heavy.sh` (`src/lattice/cli/worker_cmds.py:183`-`src/lattice/cli/worker_cmds.py:185`), shell exits non-zero, but run already reported as started.
- ✅ Confirmed — worker lifecycle cannot close on normal success. Code only emits `process_started` and spawn-time `process_failed` (`src/lattice/cli/worker_cmds.py:321`, `src/lattice/cli/worker_cmds.py:365`); there is no built-in path emitting `process_completed` after detached process exit. The lite prompt instructs artifact attach only, not completion event (`prompts/workers/code-review-lite.md:69`-`prompts/workers/code-review-lite.md:74`). Result: `active_processes` entry remains and dedup blocks rerun (`src/lattice/cli/worker_cmds.py:297`-`src/lattice/cli/worker_cmds.py:309`).
- ✅ Confirmed — race condition in dedup + snapshot update. Snapshot is read and dedup checked before lock (`src/lattice/cli/worker_cmds.py:278`-`src/lattice/cli/worker_cmds.py:309`), then updated snapshot is computed before lock (`src/lattice/cli/worker_cmds.py:342`) and written under lock later (`src/lattice/cli/worker_cmds.py:345`). Two concurrent runners can both pass dedup and persist conflicting state. This diverges from safe in-repo pattern that reads/checks/writes inside lock (`src/lattice/cli/link_cmds.py:36`-`src/lattice/cli/link_cmds.py:57`).

2. **Important**
- ✅ Confirmed — `--actor` and worker config actor are never validated before event write (`src/lattice/cli/worker_cmds.py:248`, `src/lattice/cli/worker_cmds.py:312`). Other write commands validate actor format, so this opens invalid provenance records and inconsistent event data.
- ✅ Confirmed — successful worker runs do not clean up worktrees. `_remove_worktree()` is only called in spawn exception handling (`src/lattice/cli/worker_cmds.py:384`), never after successful detached completion. This will accumulate `/tmp/lattice-worker-*` paths and git worktree metadata over time.

3. **Potential**
- ❓ Likely but hard to verify — broad env stripping of all `CODEX_`/`CLAUDE_` vars (`src/lattice/cli/worker_cmds.py:135`) may remove required auth/config variables for some engines in certain deployments.
- ❓ Likely but hard to verify — review prompt hardcodes `git diff main..HEAD` (`prompts/workers/code-review-lite.md:30`), which can produce misleading diffs when reviewing detached historical commits after `main` advances.

## Closing
This is not ready for production. I would not mass deploy this to 100k users. Minimum required fixes before rollout:
1. Implement authoritative process finalization (monitor child exit or provide explicit completion command and enforce it).
2. Move dedup check + snapshot read/apply/write into the same lock scope.
3. Validate worker definitions at startup/run (existence of `prompt_file`/script), and fail fast before writing `process_started`.
4. Add deterministic worktree cleanup on success/failure paths.
5. Add CLI/integration tests covering run lifecycle, races, and cleanup.
