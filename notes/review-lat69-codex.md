Summary
This patch fixes key concerns from prior review by adding transition-aware claiming and moving the claim write path under lock. `compute_claim_transitions()` is sound for shortest-path discovery and is well covered by new unit tests. The major remaining issue is a race-window behavior in `--claim` that can reassign an already-claimed in-progress task to a second actor.

Findings

Critical
- Concurrent claim can steal ownership from another actor instead of failing or re-selecting. In `next --claim`, after lock acquisition the code re-reads the snapshot but does not re-validate that the task is still claimable by the caller; it only checks `current_assigned != actor` and emits an `assignment_changed` event to force ownership (`src/lattice/cli/query_cmds.py:347`, `src/lattice/cli/query_cmds.py:349`, `src/lattice/cli/query_cmds.py:357`). If another actor claimed the task first, a second claimer can overwrite assignee while status is already `in_progress` (`src/lattice/cli/query_cmds.py:361`). This is a correctness regression under contention and can corrupt task ownership semantics.

Important
- Claim path always acquires the global lifecycle lock even when no lifecycle events are written (`src/lattice/cli/query_cmds.py:337`). `write_task_event()` only includes `events__lifecycle` when lifecycle events exist (`src/lattice/storage/operations.py:68`, `src/lattice/storage/operations.py:70`). This change serializes all claims through one global lock and reduces concurrency unnecessarily.

Minor
- Redundant/unused import in the lock-held write block (`src/lattice/cli/query_cmds.py:389`). `execute_hooks` is imported there but only used after lock release (`src/lattice/cli/query_cmds.py:408`).

Positive
- `compute_claim_transitions()` uses BFS and correctly returns shortest valid paths with bounded depth; behavior is explicitly tested for direct, multi-hop, shortest-path, and no-path cases (`src/lattice/core/next.py:119`, `tests/test_core/test_next.py:330`).
- The direct-under-lock write sequence preserves canonical ordering (event log -> lifecycle log -> atomic snapshot), matching the expected durability model (`src/lattice/cli/query_cmds.py:391`, `src/lattice/cli/query_cmds.py:401`, `src/lattice/storage/operations.py:79`, `src/lattice/storage/operations.py:94`).
- Theme lane-color additions appear complete for all themes present in this map block, including `in_progress`, `review`, `blocked`, and `needs_human` plus legacy aliases (`src/lattice/dashboard/static/index.html:2815`, `src/lattice/dashboard/static/index.html:3009`).

Test Coverage Assessment
- Strong additions for pure transition logic (`tests/test_core/test_next.py`) and nominal CLI claim flows (`tests/test_cli/test_next_cmd.py`).
- Missing coverage for the critical concurrent-claim race: no test asserts behavior when snapshot eligibility changes between initial selection and locked re-read.
- Missing coverage for lock-key behavior parity with `write_task_event()` (specifically, lifecycle lock acquisition only when lifecycle events are present).
- Local execution note: `PYTHONPATH=src pytest -q tests/test_core/test_next.py tests/test_cli/test_next_cmd.py` ran unit coverage in `test_core/test_next.py` but CLI tests errored in this environment due missing dependency `ulid`.

Verdict: Ship with fixes
- Fix required before full ship: re-validate claim eligibility under lock and reject/retry when task was claimed by another actor.
- Recommended follow-up: align claim lock-key selection with `write_task_event()` to avoid global lifecycle-lock contention.
