## Critical Code Review
- **Date:** 2026-02-16 23:50 EST
- **Model:** Gemini 2.0 Flash
- **Branch:** main
- **Latest Commit:** ad31c96
- **Linear Story:** LAT-93
- **Review Type:** Critical/Adversarial
---

**The Ugly Truth**
This implementation effectively delivers the feature but introduces severe data integrity risks. The `worker run` command contains a textbook Time-of-Check Time-of-Use (TOCTOU) race condition that will cause data loss in a multi-user or concurrent environment. Furthermore, the error handling during the startup phase is "optimistic" — it commits the "started" state to the database before verifying that the worker can actually run. If `git worktree` fails (which it will), the system enters an inconsistent zombie state that requires manual database surgery to fix.

**What Will Break**
1. **Data Loss (Snapshot Reversion):** If User A runs `lattice worker run` and User B runs `lattice comment` at the same time, User A's process will overwrite User B's changes. The snapshot read happens outside the lock, so User A writes a stale snapshot back to disk.
2. **Zombie Tasks:** If `git worktree add` fails (e.g., disk full, lock file exists, git error), the `process_started` event is already committed. The CLI crashes, but the task remains "active" forever. The UI will show a spinning worker that never completes.
3. **Worktree Corruption:** Relying on `/tmp` for git worktrees is fragile. If an aggressive temp cleaner (like `tmpreaper`) runs, or if the machine reboots, the directory vanishes but git's internal metadata (`.git/worktrees`) remains. Subsequent `git worktree add` calls will fail, and `git prune` is never called to recover.
4. **Silent Failures:** There is no monitoring of the detached process. If the Python process fails to spawn (e.g., OOM immediately after fork) or the agent crashes before writing a failure event, the system drifts into a permanently active state.

**What's Missing**
- **Transactional Integrity:** The read-modify-write cycle in `worker_run` must be fully locked.
- **Compensating Transactions:** If the setup phase (worktree creation, process spawning) fails, the `process_started` event must be compensated with a `process_failed` event.
- **Garbage Collection:** No mechanism to reap stuck workers or prune stale git worktrees.
- **Type Safety:** The codebase is riddled with `mypy` errors (300+), and this PR adds more potential dynamic typing issues by relying on loose dictionary schemas for process tracking.

**The Nits**
- `subprocess.run` is used without `check=True` in `_create_worktree` cleanup, which is fine, but explicit error suppression is cleaner.
- The `env` stripping for `CLAUDE_` vars is a good hack but fragile; it should probably be a configurable list.
- `timeout_minutes` is recorded but never enforced.

---

### 1. Blockers (Must Fix)

1. **Race Condition in `worker_run`**
   - **File:** `src/lattice/cli/worker_cmds.py`:270-348
   - **Issue:** The snapshot is loaded at line 278. The lock is acquired at line 341. Any changes made to the task (comments, status changes) between these lines by other actors are **permanently lost** when `atomic_write` overwrites the file at line 347.
   - **Fix:** Move the snapshot load *inside* the `multi_lock` context manager, or verify the `last_event_id` hasn't changed before writing.

2. **Zombie State on Setup Failure**
   - **File:** `src/lattice/cli/worker_cmds.py`:330-336
   - **Issue:** `apply_event_to_snapshot` and the event write happen *before* `_create_worktree`. If `_create_worktree` raises `ClickException` (line 335), the program exits immediately (via `output_error`). The task is marked as running a process, but the process never started.
   - **Fix:** Wrap the setup in a `try/except` block that catches failures *after* the event is written and emits a `process_failed` event before exiting.

3. **Insecure Temp Directory Usage**
   - **File:** `src/lattice/cli/worker_cmds.py`:68
   - **Issue:** `worktree_path = Path(f"/tmp/lattice-worker-{short_task}-{short_sha}-{short_eid}")`. While the ULID makes it fairly unique, creating fixed paths in `/tmp` is generally insecure. More importantly, `git worktree remove --force` is called on this path. If an attacker can predict the ID (difficult but not impossible) and place a symlink, you might delete arbitrary files.
   - **Fix:** Use `tempfile.mkdtemp` or a dedicated user-owned directory (e.g., `~/.lattice/worktrees`) to ensure safety and persistence.

### 2. Important (Should Fix)

4. **Missing Git Prune**
   - **File:** `src/lattice/cli/worker_cmds.py`
   - **Issue:** If worktrees are removed manually or by OS cleaning, `git worktree add` may fail because git thinks the branch is already checked out.
   - **Fix:** Run `git worktree prune` before adding a new worktree.

5. **Silent Process Failure**
   - **File:** `src/lattice/cli/worker_cmds.py`:190
   - **Issue:** `subprocess.Popen` starts the process. If the executable (e.g., `claude`) is missing or fails immediately, `Popen` might raise `FileNotFoundError` (caught in `worker_run`), but if it fails *soon* after start, nothing catches it.
   - **Fix:** Use a small wrapper script or double-fork checking, or accept this risk. But at least the `FileNotFoundError` case in `worker_run` (line 358) *does* catch the spawn failure, which is good. The risk is runtime crash.

### 3. Potential (Refactoring)

6. **Unenforced Timeout**
   - **Issue:** `timeout_minutes` is stored but unused.
   - **Fix:** Pass it to the worker agent as a hard limit, or implement a "watchdog" command that kills stale processes.

## Validation Pass

- ✅ **Race Condition**: Verified by code inspection. `snapshot = json.loads(...)` occurs lines before `with multi_lock(...)`.
- ✅ **Zombie State**: Verified by code inspection. `_create_worktree` raises `ClickException` which bubbles up to `output_error` (exit), bypassing any cleanup logic for the already-persisted event.
- ❓ **Insecure Temp**: Theoretical. Requires ID prediction. Low probability but bad practice.
- ✅ **Missing Prune**: Git behavior is well-known. Stale worktree metadata locks branches.

## Verdict
**NOT READY FOR PRODUCTION.**

The race condition alone disqualifies this from merge. It corrupts the primary data store (task snapshots) in a way that is subtle and hard to debug. The setup failure handling ensures that any git glitch results in a stuck system. Fix the locking scope and the error handling flow before merging.
