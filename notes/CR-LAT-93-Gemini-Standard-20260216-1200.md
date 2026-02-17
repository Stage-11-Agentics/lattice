## Code Review
- **Date:** 2026-02-16 12:00
- **Model:** Gemini 2.0 Flash
- **Branch:** main (checked out ad31c96)
- **Latest Commit:** ad31c96
- **Linear Story:** LAT-93
---

### Architectural Assessment
The implementation of the Lattice Workers system introduces a solid foundation for autonomous agents.
- **Isolation:** Using detached processes and git worktrees (`git worktree add --detach`) is a robust pattern for ensuring workers don't interfere with the user's current index or working directory.
- **Environment Safety:** Explicitly stripping `CLAUDE_` and `CODEX_` environment variables addresses the specific risk of recursion when agents spawn agents, which shows good foresight.
- **Event-Driven:** The lifecycle (start/complete/fail) is tracked via events, which integrates well with the existing Lattice architecture.
- **Resilience:** The system attempts to clean up stale worktrees, but the lack of an external supervisor means that if a worker process crashes hard (segfault/OOM) without sending a `process_failed` event, the task will remain in an "active" state indefinitely in the snapshot. This is a known trade-off for a V1 but should be noted.

### Tactical Assessment
- **Test Coverage:** The core mutations and event types are well-tested in `test_core/`, but there are **no unit tests** for `src/lattice/cli/worker_cmds.py`. The logic for loading definitions, building environments, and CLI argument parsing is currently uncovered.
- **Path Handling:** Hardcoded `/tmp/lattice-worker-...` in `_create_worktree` is not portable and may fail on systems where `/tmp` is not the standard temporary directory or is restricted.
- **Process Management:** `subprocess.Popen` is used correctly with `start_new_session=True` to detach.

### Issues

#### Blockers
1.  **Missing Tests for CLI Logic:** `src/lattice/cli/worker_cmds.py` has no corresponding test file. The logic for `_build_clean_env`, `_find_workers_dir`, and definition loading should be tested to ensure robustness.
    - ✅ Confirmed: No test file found for `worker_cmds.py`.
    - *Action:* Create `tests/test_cli/test_worker_cmds.py` and add tests for non-subprocess logic.

#### Important
2.  **Hardcoded Temp Directory:** `_create_worktree` uses `/tmp/lattice-worker-...`.
    - ✅ Confirmed: Found literal `/tmp` in `worker_cmds.py`.
    - *File:* `src/lattice/cli/worker_cmds.py`
    - *Risk:* Portability issues (Windows, non-standard Linux setups).
    - *Fix:* Use `tempfile.gettempdir()` or `pathlib.Path(tempfile.gettempdir())`.

3.  **Orphaned Process State:** If a worker crashes silently (no `process_failed` event), the `active_processes` list in the task snapshot will be permanently stale.
    - ✅ Confirmed: No logic handles crashing workers (e.g. heartbeat or external supervisor).
    - *Fix:* Consider a "heartbeat" or a "reap" command that checks if the PIDs in `active_processes` are actually running.

#### Potential
4.  **Security of `script` engine:** The `script` engine executes commands via `bash -c`. While these come from committed JSON files, ensure that `worker_def["command"]` is validated or restricted if this feature is ever exposed to user-generated content.
    - ⬇️ Lower priority: Internal tool usage for now.