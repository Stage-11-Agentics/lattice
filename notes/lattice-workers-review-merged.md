# Lattice Workers Design Review — Merged Synthesis

**Source reviews:** Claude Opus (12 findings), Codex o3 (9 findings)
**Synthesized by:** Claude Opus | 2026-02-16

---

## Merged Findings (Deduplicated)

Items are ordered by priority. Where both reviewers raised the same issue, the stronger articulation is credited and the other noted as concurring.

---

### 1. Queue/Dispatcher is Over-Engineered — Simplify to Direct Spawning — [Critical]

**Raised by:** Claude (primary) | Codex concurs (raises queue claiming as a separate critical issue)

**Problem:** The design introduces a three-step indirection: event → hook writes to queue → dispatcher polls queue → spawns worker. This is a process supervisor, which Lattice explicitly lists as a non-goal. Users must run `lattice worker watch`, monitor it for crashes, and debug stale queue entries.

**Claude's suggestion (stronger):** `lattice worker run <name> <task>` as a standalone command that spawns the worker as a detached top-level process (`setsid`/`start_new_session=True`, cleaned env). The hook calls `lattice worker run` directly. No queue, no dispatcher, no daemon.

**My assessment: Agree strongly.** The queue/dispatcher is solving a problem that doesn't exist yet. The nesting issue is actually simpler than the design assumes — hooks pass through `os.environ.copy()` (verified in `hooks.py:159`), which includes `CLAUDECODE` vars that prevent nesting. The fix is: strip those vars when spawning the worker process. `lattice worker run` with `start_new_session=True` and a clean env is sufficient for v1. If we later need deferred/batched execution, the queue can be added without breaking the interface.

---

### 2. Worker Definitions Must Be Version-Controlled — [Critical]

**Raised by:** Both (independently)

**Problem:** `.lattice/workers/` is gitignored. Worker definitions are project configuration, not runtime state.

**Consensus suggestion:** `workers/` at repo root for definitions. `.lattice/queue/` (if kept) for runtime state only.

**My assessment: Agree.** This is straightforward. `workers/` at repo root, JSON format (see #4). Consistent with `scripts/` and `prompts/` already at repo root.

---

### 3. Process Correlation Must Use Unique ID, Not `process_type + commit_sha` — [Critical]

**Raised by:** Both (independently)

**Problem:** Matching `process_completed` to `process_started` by `process_type + commit_sha` breaks on retries, manual re-runs, and duplicate triggers.

**Claude's suggestion:** Match on `started_event_id` (carry the `ev_` ID of the `process_started` event in the completion event).

**Codex's suggestion:** Generate a dedicated `process_id` (ULID) at spawn time, include in all process events.

**My assessment: Claude's approach is better.** We already generate a unique `ev_` ID for every event. Using `started_event_id` in `process_completed`/`process_failed` events avoids introducing a new ID type. The event ID IS the process correlation key. No new `wq_` prefix needed (which both reviewers flagged as unnecessary complexity).

---

### 4. Use JSON, Not TOML — [Important]

**Raised by:** Claude

**Problem:** TOML introduces a new format and parser when everything in Lattice is JSON.

**My assessment: Agree.** `tomllib` is stdlib in 3.11+, but the consistency argument is stronger than the ergonomics argument for 15-line config files. JSON everywhere.

---

### 5. `active_processes` Mixes Ephemeral and Durable State in Snapshots — [Important]

**Raised by:** Claude (unique finding)

**Problem:** Every other field in a snapshot (`status`, `assigned_to`, `artifact_refs`) is durable state. `active_processes` is ephemeral runtime state that becomes stale when workers crash. Worse: `lattice rebuild` replays all events, resurrecting zombie process entries from months ago.

**Claude's two options:**
- (a) Keep in snapshot but match by `started_event_id` (fixes correlation, not the rebuild zombie problem)
- (b) Derive at read time by scanning for unmatched `process_started` events (eliminates stale state, but slower reads)

**My assessment:** Option (a) for v1 with a `lattice rebuild` handler that ignores process events older than 24h. This keeps reads fast while limiting zombie exposure. Option (b) is the philosophically pure answer but premature optimization of purity over practicality — per-task event files are small, but adding a scan on every read is a cost we shouldn't take without measuring.

Actually, there's a simpler answer: during `rebuild`, don't materialize `active_processes` at all. Rebuild regenerates durable state. Active processes are transient by definition — after a rebuild, nothing is running. Set `active_processes: []` during rebuild. Workers that are actually still running will have their events in the log and the dashboard can derive current state if needed.

---

### 6. Trigger Ownership: One Mechanism, Not Two — [Important]

**Raised by:** Both (Claude as "relationship between hooks and workers is muddled"; Codex as "trigger path ownership is ambiguous")

**Problem:** The design has two trigger mechanisms: hooks (in `config.json`) and trigger conditions (in worker definitions). Users must check both places to understand "what happens when a task enters review?"

**Consensus suggestion:** Workers define *what* and *how*. Hooks define *when*. The hook config triggers workers:
```json
{"hooks": {"transitions": {"* -> review": "lattice worker run CodeReviewLite $LATTICE_TASK_ID"}}}
```

**Codex also raised:** Write paths aren't fully centralized — archive operations in `query_cmds.py`, `archive_cmds.py`, `server.py`, and `mcp/tools.py` bypass `write_task_event()` (intentionally, to avoid lock re-acquisition). This means hooks (and thus worker triggers) only fire through the standard write path, not archive operations.

**My assessment: Agree on single trigger mechanism.** The archive bypass is intentional and correct — archiving a task shouldn't trigger workers. But this is worth documenting: workers only fire through the standard event write path.

---

### 7. Worktrees Must Be in Phase 2, Not Phase 5 — [Important]

**Raised by:** Claude (unique finding, with strong reasoning)

**Problem:** Without worktrees, every review produced in Phases 1-4 reviews the wrong code (whatever's on disk at the moment, which other agents are actively modifying). Worktree logic is simple (`git worktree add/remove`) and independent of the queue/dispatcher.

**My assessment: Agree.** Worktrees are correctness, not polish. Move to Phase 2.

---

### 8. No Concurrency/Deduplication Guard — [Important]

**Raised by:** Both (Claude as "no concurrency guard"; Codex raises collision concerns for worktree paths)

**Problem:** If a task bounces `review → in_progress → review`, the worker triggers twice. Without dedup, two workers run concurrently for the same task.

**Suggestion:** Check `active_processes` before spawning. If a worker of the same type is already running for that task, skip.

**Codex addition:** Include `process_id` (or `started_event_id`) in worktree path to prevent path collisions on concurrent runs.

**My assessment: Agree.** Pre-spawn dedup check is essential. Worktree path should include a unique suffix: `/tmp/lattice-worker-LAT-42-abc1234-<short_event_id>`.

---

### 9. Review Freshness Should Be Enforceable, Not Just Informational — [Important]

**Raised by:** Codex (unique finding)

**Problem:** A task can satisfy the `done` completion policy with an outdated review if commits landed after the review was attached.

**Suggestion:** Persist `commit_sha` in artifact metadata. Add an optional policy: `require_fresh_review: true` that checks `review.commit_sha == HEAD` when transitioning to `done`.

**My assessment: Good idea, but defer.** For v1, the badge showing "3 commits since review" is sufficient. Enforcing freshness means reviews block `done` until HEAD stabilizes, which creates a frustrating loop if minor fixups keep landing. Make it informational first, let usage patterns emerge, then add enforcement as an opt-in policy.

---

### 10. Compute Drift at Read Time, Not in Events — [Important]

**Raised by:** Claude (unique finding)

**Problem:** `head_at_completion` in `process_completed` captures a point-in-time value that's stale the moment another commit lands. The worker runs in a worktree and may not have correct info about main worktree HEAD.

**Suggestion:** Drop `head_at_completion` from the event. Compute drift at dashboard read time by comparing review artifact's `commit_sha` against current HEAD.

**My assessment: Agree.** Read-time derivation is always fresh. The event should carry what the worker knows (`commit_sha` it reviewed). The dashboard computes what changed since.

---

### 11. Log Path Must Be a First-Class Contract — [Important]

**Raised by:** Codex (unique finding)

**Problem:** Dashboard expects log links but the process event schema doesn't define where logs live.

**Suggestion:** Standardize: `.lattice/logs/workers/<started_event_id>.log`. Include `log_path` in process events.

**My assessment: Agree.** Simple, deterministic, debuggable. The `started_event_id` as filename keeps it unique.

---

### 12. Hooks Don't Strip Environment Variables — [Important]

**Raised by:** Neither (discovered during investigation)

**Problem:** `_build_env()` in `hooks.py:159` does `os.environ.copy()` — full passthrough. When a hook runs `lattice worker run`, the worker inherits `CLAUDECODE` env vars that prevent nesting.

**My assessment:** `lattice worker run` must explicitly strip Claude/Codex session vars before spawning the agent process. This is the actual fix for the nesting problem.

---

### 13. Worker Actor Identity Should Include Session — [Minor]

**Raised by:** Claude

**Problem:** Static `actor = "agent:review-bot"` loses per-invocation traceability.

**Suggestion:** Use `agent_meta.session` (already in the event schema, verified) to carry a per-invocation ID.

**My assessment: Agree.** Use existing `agent_meta.session` field rather than inventing new actor patterns.

---

### 14. Engine Preflight Checks — [Minor]

**Raised by:** Codex (unique finding)

**Problem:** Worker definitions assume engine CLIs exist and are authenticated. No health check before spawning.

**Suggestion:** `lattice worker doctor` — verify binaries, auth, prompt files, write access.

**My assessment: Good but defer.** For v1, if the engine isn't available, the worker fails and `process_failed` captures the error. A `doctor` command is nice-to-have.

---

### 15. Remove "Lattice Agent" From v1 Design — [Minor]

**Raised by:** Claude

**Problem:** The three-tier table (Hooks / Workers / Lattice Agent) frames the design as building toward complexity that may never be needed.

**My assessment: Agree.** Mention in future directions, not in the architecture table.

---

## Revised Phase Ordering

Based on merged findings:

1. **Process tracking events** — `process_started` / `process_completed` / `process_failed`. Match by `started_event_id`. During rebuild, set `active_processes: []`.
2. **Worker definitions + CLI + worktrees** — `workers/` at repo root (JSON). `lattice worker run <name> <task>` with worktree creation, env stripping, process event posting, `start_new_session=True`.
3. **Hook integration** — Update `"* -> review"` hook to call `lattice worker run CodeReviewLite`. Add dedup check (skip if already running).
4. **Dashboard integration** — Process indicators, completion policy badges, drift detection (read-time), log links.
5. **CodeReviewHeavy** — manual-only via `lattice worker run CodeReviewHeavy LAT-42`.
6. **Escalation policies** — Defer until usage patterns emerge.

---

## Which Review is Better?

**Claude's review is stronger overall.** It produced 12 findings vs Codex's 9, but more importantly:

- Claude identified the **strongest single insight**: the queue/dispatcher is over-engineered and direct spawning solves the nesting problem with dramatically less complexity. This reframes the entire architecture.
- Claude caught the **snapshot ephemeral state problem** (active_processes in rebuild) and the **phase ordering issue** (worktrees must be early, not late) — both missed by Codex.
- Claude's suggestions were more **architecturally coherent** — each finding connected to Lattice's existing philosophy rather than proposing new mechanisms.

**Codex contributed unique value in:**
- **Trigger path consistency** — noting that archive operations bypass `write_task_event()` and thus won't fire worker triggers. This is operationally important.
- **Review freshness enforcement** — the idea that drift should be enforceable, not just visible. Good forward-thinking even if deferred.
- **Log path contract** — practical, essential, missed by Claude.
- **Engine preflight checks** — minor but practical.

**Verdict: Claude 7/10, Codex 6/10.** Claude's architectural instinct is sharper (the "just use direct spawning" insight alone is worth the review). Codex is more thorough on operational details and edge cases. Together they cover the space well.
