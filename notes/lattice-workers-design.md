# Lattice Workers — Design Document

**LAT-93** | Spike | Status: in_planning
**Author:** human:atin + agent:claude-opus | **Date:** 2026-02-16

---

## Problem Statement

Lattice hooks are synchronous shell commands with a 10-second timeout. They're designed for lightweight reactions — notifications, logging, quick validations. But the most valuable automation (code reviews, plan generation, test suites) requires minutes to hours of autonomous work by AI agents.

The current `on-review.sh` hook tries to cheat this by backgrounding a `claude -p` call with `nohup`. This fails in two ways:

1. **Nesting failure:** `claude -p` cannot spawn inside an active Claude Code session. The hook fires but the review agent never launches.
2. **No visibility:** Even when it works (from a bare terminal), there's zero indication in Lattice that a process is running. No spinner, no status, no failure detection.

We need a first-class abstraction for **autonomous, event-driven agents that do real work against Lattice tasks**.

---

## Concept: Lattice Workers

**Workers** are named, independent processes that react to conditions on the Lattice board. They watch for triggers (a task entering a status, a missing artifact, a stale condition) and perform autonomous work in response.

### Workers vs Hooks vs Agents

| | Hooks | Workers | Lattice Agent |
|---|---|---|---|
| **Scope** | Single reaction | Single task automation | Whole board |
| **Lifetime** | Milliseconds | Minutes to hours | Persistent |
| **Spawned by** | Event write path | Lattice worker system | Human or orchestrator |
| **Timeout** | 10 seconds | None (self-reporting) | None |
| **Nesting** | Inherits parent context | Top-level process | Top-level process |
| **Failure mode** | Silent (fire-and-forget) | Tracked via process events | Tracked |
| **Purpose** | Notify, log, validate | Review, plan, test | Orchestrate, triage, assign |

**Hooks** stay for what they're good at: lightweight, synchronous reactions.

**Workers** handle heavyweight automation that takes real time and needs visibility.

**Lattice Agent** (future) is the board-level orchestrator — it decides what gets worked on, assigns tasks, balances load. Workers are its hands; the agent is the brain.

---

## Named Workers

Workers are named entities with defined behavior, not anonymous shell scripts. Each worker has:

- **Name** — human-readable identifier (e.g., `CodeReviewLite`)
- **Trigger** — what condition activates it
- **Engine** — what AI model/tool executes the work
- **Behavior** — what it does when activated
- **Artifacts** — what it produces

### Initial Workers

#### CodeReviewLite

Fast, single-agent code review for small to medium changes.

| Property | Value |
|----------|-------|
| **Name** | `CodeReviewLite` |
| **Trigger** | Task enters `review` + no artifact with `role: review` |
| **Engine** | Single Claude instance (`claude -p`) |
| **Worktree** | Yes — frozen at trigger commit |
| **Timeout** | 10 minutes |
| **Produces** | Markdown review artifact (`role: review`) |

**What it does:**
1. Captures current HEAD commit SHA
2. Creates a git worktree at that commit
3. Posts `process_started` event on the task
4. Runs in the worktree:
   - `git diff main...<commit>` — the changeset
   - `ruff check` — lint
   - `pytest --tb=short` — test results (quick, no long-running)
   - Single-pass review: correctness, style, edge cases, security
5. Writes review to `notes/CR-<short_id>-lite-<timestamp>.md`
6. Attaches artifact with `--role review`
7. Posts `process_completed` event
8. Cleans up worktree

**When to use:** Default for most tasks. Fast feedback loop. Good enough for routine changes, small features, bug fixes.

#### CodeReviewHeavy

Full multi-agent review for significant changes. The "team three" pattern.

| Property | Value |
|----------|-------|
| **Name** | `CodeReviewHeavy` |
| **Trigger** | Manual invocation or policy-based (epic tasks, security-labeled, etc.) |
| **Engine** | 6 agents: Claude standard + critical, Codex standard + critical, Gemini standard + critical |
| **Worktree** | Yes — frozen at trigger commit |
| **Timeout** | 30 minutes |
| **Produces** | Merged review artifact (`role: review`) + individual agent reports |

**What it does:**
1. Everything CodeReviewLite does for setup (commit capture, worktree, process_started)
2. Builds review context document (diff, stats, test results, lint results)
3. Generates standard and critical review prompts
4. Launches 6 review agents in parallel:
   - Claude (standard review + critical review)
   - Codex (standard review + critical review)
   - Gemini (standard review + critical review)
5. Waits for all agents to complete
6. Synthesizes a merged review report (Claude Opus merges)
7. Writes merged report to `notes/CR-<short_id>-heavy-<timestamp>.md`
8. Attaches merged artifact with `--role review`
9. Stores individual agent reports as supplementary artifacts
10. Posts `process_completed` event
11. Cleans up worktree

**When to use:** Major features, security-sensitive changes, architectural refactors. When you want multiple model perspectives and deep analysis.

### Future Workers (not in initial implementation)

| Worker | Trigger | What it does |
|--------|---------|-------------|
| `PlanReviewLite` | Task enters `in_planning` | Single-agent plan review and feedback |
| `PlanReviewHeavy` | Manual or policy | Multi-agent plan review (triple force) |
| `TestRunner` | Task enters `review` | Runs full test suite, attaches results |
| `AutoTriage` | Task enters `backlog` | Auto-assigns priority/type/labels from title+description |
| `StaleDetector` | Periodic | Finds tasks stuck in a status too long, posts warnings |

---

## Process Tracking — Event Model

Workers report their lifecycle through Lattice events. This is how the dashboard knows what's happening.

### New Event Types

#### `process_started`

Posted when a worker begins work on a task.

```json
{
  "type": "process_started",
  "task_id": "task_01...",
  "actor": "agent:review-bot",
  "data": {
    "process_type": "CodeReviewLite",
    "commit_sha": "abc1234def5678...",
    "worktree_path": "/tmp/lattice-review-LAT-42-abc1234",
    "timeout_minutes": 10
  }
}
```

#### `process_completed`

Posted when a worker finishes successfully.

```json
{
  "type": "process_completed",
  "task_id": "task_01...",
  "actor": "agent:review-bot",
  "data": {
    "process_type": "CodeReviewLite",
    "commit_sha": "abc1234def5678...",
    "result": "success",
    "artifact_id": "art_01...",
    "duration_seconds": 142,
    "head_at_completion": "def5678abc1234..."
  }
}
```

The `head_at_completion` field captures where HEAD is when the review finishes. If it differs from `commit_sha`, the dashboard can show "N commits since review."

#### `process_failed`

Posted when a worker crashes or times out.

```json
{
  "type": "process_failed",
  "task_id": "task_01...",
  "actor": "agent:review-bot",
  "data": {
    "process_type": "CodeReviewLite",
    "commit_sha": "abc1234def5678...",
    "error": "Agent process exited with code 1",
    "duration_seconds": 45
  }
}
```

### Snapshot Materialization

These events materialize into an `active_processes` field on the task snapshot:

```json
{
  "id": "task_01...",
  "status": "review",
  "active_processes": [
    {
      "process_type": "CodeReviewLite",
      "started_at": "2026-02-16T14:32:00Z",
      "actor": "agent:review-bot",
      "commit_sha": "abc1234",
      "event_id": "ev_01..."
    }
  ]
}
```

**Mutation rules:**
- `process_started` → append to `active_processes`
- `process_completed` → remove matching entry from `active_processes` (match on `process_type` + `commit_sha`)
- `process_failed` → remove matching entry from `active_processes`

An entry in `active_processes` with no corresponding `process_completed` or `process_failed` = stale/crashed process. Detectable by comparing `started_at` against `timeout_minutes`.

---

## Worker Spawning

### The Nesting Problem

The fundamental issue: hooks fire from inside whatever process wrote the event. If that's a Claude Code session, `claude -p` can't nest. If that's the dashboard, the subprocess inherits the server's context.

**Solution: Workers are spawned by a lightweight dispatcher, not by hooks.**

### Dispatch Architecture

```
Event written
  → Hook fires (synchronous, <10s)
    → Hook writes a work request to .lattice/queue/<request_id>.json
  → Hook returns immediately

Worker dispatcher (independent process)
  → Polls .lattice/queue/ for new requests
  → Matches request to worker definition
  → Spawns worker as a top-level process (no nesting)
  → Worker does its thing, reports via events
  → Dispatcher cleans up queue entry
```

Or, simpler:

```
Event written
  → write_task_event() calls execute_hooks() AND check_worker_triggers()
  → check_worker_triggers() writes to .lattice/queue/
  → An independent `lattice worker watch` process picks it up
```

### Queue File Format

```json
{
  "id": "wq_01...",
  "created_at": "2026-02-16T14:30:00Z",
  "task_id": "task_01...",
  "worker": "CodeReviewLite",
  "trigger_event": "ev_01...",
  "commit_sha": "abc1234",
  "status": "pending"
}
```

Statuses: `pending` → `claimed` → `completed` / `failed`

### CLI Commands

```bash
# Start the worker dispatcher (long-running, foreground or daemonized)
lattice worker watch

# List registered workers and their status
lattice worker list

# Manually trigger a worker on a task
lattice worker run CodeReviewLite LAT-42

# Show worker queue
lattice worker queue

# Show running/recent worker processes
lattice worker ps
```

`lattice worker watch` is the long-running process that:
1. Polls `.lattice/queue/` for pending work requests
2. Matches each to a worker definition
3. Spawns the worker as a top-level subprocess
4. Monitors for completion or timeout
5. Posts `process_completed` or `process_failed` events
6. Cleans up

`lattice worker run` bypasses the queue and runs a worker directly. Useful for testing and manual triggers.

---

## Worker Definitions

Workers are defined in `.lattice/workers/` as TOML or JSON files:

```
.lattice/workers/
├── code-review-lite.toml
└── code-review-heavy.toml
```

### code-review-lite.toml

```toml
[worker]
name = "CodeReviewLite"
description = "Fast single-agent code review"
actor = "agent:review-bot"

[trigger]
on_status = "review"
missing_role = "review"          # Only fires if no review artifact exists
cooldown_minutes = 5             # Don't re-trigger within 5 min of last run

[execution]
engine = "claude"                # claude | codex | gemini | script
command = "claude -p"            # Base command (worker system adds prompt)
prompt_file = "prompts/workers/code-review-lite.md"
worktree = true                  # Create isolated worktree
timeout_minutes = 10

[output]
artifact_role = "review"
report_prefix = "CR"
report_dir = "notes"
```

### code-review-heavy.toml

```toml
[worker]
name = "CodeReviewHeavy"
description = "Multi-agent team three code review"
actor = "agent:review-team"

[trigger]
# No automatic trigger — invoked manually or by policy
manual_only = true

[execution]
engine = "script"
command = "./workers/code-review-heavy.sh"
worktree = true
timeout_minutes = 30

[output]
artifact_role = "review"
report_prefix = "CR"
report_dir = "notes"
```

### Policy-Based Trigger Escalation

The config can specify when to escalate from Lite to Heavy:

```json
{
  "worker_policies": {
    "review": {
      "default": "CodeReviewLite",
      "escalate_to": "CodeReviewHeavy",
      "escalate_when": {
        "labels": ["security", "breaking-change"],
        "type": ["epic"],
        "diff_lines_gt": 500
      }
    }
  }
}
```

Small PR with no special labels → CodeReviewLite. Epic with security label → CodeReviewHeavy. Clean escalation path.

---

## Worktree Management

Every worker that sets `worktree = true` gets an isolated git worktree.

### Lifecycle

```bash
# 1. Worker dispatcher captures HEAD
COMMIT=$(git rev-parse HEAD)

# 2. Create worktree in a temp location
WORKTREE="/tmp/lattice-worker-LAT-42-${COMMIT:0:8}"
git worktree add "$WORKTREE" "$COMMIT" --detach

# 3. Worker runs inside $WORKTREE
# (all file reads, diffs, tests happen against frozen code)

# 4. Cleanup after worker completes
git worktree remove "$WORKTREE"
```

### Why worktrees matter

- **Accuracy:** The review covers exactly the code at the trigger commit, not whatever's on disk 20 minutes later
- **Isolation:** Other agents keep working in the main worktree without interference
- **Reproducibility:** The commit SHA in the review artifact tells you exactly what was reviewed
- **Drift detection:** Compare `commit_sha` (reviewed) vs current HEAD to know if the review is stale

---

## Dashboard Integration

### Task Card Indicators

When `active_processes` is non-empty, the dashboard task card shows:

- **Animated indicator** — spinning icon or pulsing border (themed to match dashboard theme)
- **Worker name** — "CodeReviewLite running..."
- **Duration** — "Started 3m ago"
- **Commit** — "Reviewing abc1234"

### Task Detail View

The task detail page shows a **Process History** section:

```
Process History
─────────────────────────────────────────────
● CodeReviewLite    abc1234    2:34 PM → 2:36 PM    ✓ success    142s
○ CodeReviewHeavy   abc1234    2:40 PM → (running)   ⟳ in progress
```

Each entry links to:
- The review artifact (if completed)
- The log file (always)
- The commit it reviewed

### Completion Policy Badge

When a task has a completion policy requiring a role (e.g., `review`), the dashboard shows:

- **Before artifact:** `Needs: review` (red/amber badge)
- **After artifact:** `Has: review @ abc1234` (green badge)
- **If HEAD moved:** `Has: review @ abc1234 (+3 commits since)` (green badge with warning)

---

## Failure Handling

### Worker Crashes

If a worker process dies without posting `process_completed` or `process_failed`:

1. The `active_processes` entry stays in the snapshot
2. The dispatcher detects the orphaned process (exit code, missing PID)
3. Dispatcher posts `process_failed` event with error details
4. Dashboard indicator changes from spinner to error state

### Stale Process Detection

If the dispatcher itself crashes or wasn't running:

- A periodic sweep (`lattice doctor` or a future `StaleDetector` worker) checks for entries in `active_processes` older than `timeout_minutes`
- Posts `process_failed` for any stale entries
- Alerts via hook or dashboard

### Retry Policy

Workers don't auto-retry by default. If a review fails:

1. The task still has no review artifact
2. The trigger condition still matches (status=review, missing_role=review)
3. The `cooldown_minutes` prevents immediate re-trigger
4. After cooldown, the dispatcher can re-trigger (if configured) or wait for manual `lattice worker run`

---

## Implementation Plan

### Phase 1: Process Tracking Events (foundation)

- New event types: `process_started`, `process_completed`, `process_failed`
- Mutation handlers in `tasks.py` for `active_processes`
- Tests for materialization

### Phase 2: Worker Definitions + CLI

- `.lattice/workers/` directory and TOML parsing
- `lattice worker list` — show registered workers
- `lattice worker run <name> <task>` — manual trigger (direct, no queue)
- This alone is useful — manual `lattice worker run CodeReviewLite LAT-42` from a terminal

### Phase 3: Queue + Dispatcher

- `.lattice/queue/` directory
- `lattice worker watch` — long-running dispatcher
- Auto-trigger: event write path enqueues work requests
- Dispatcher polls, spawns, monitors, cleans up

### Phase 4: Dashboard Integration

- Task card process indicators (animated)
- Completion policy badges
- Process history in task detail
- Commit drift warnings

### Phase 5: Worktree Management

- Automatic worktree creation/cleanup
- Commit pinning in process events and artifacts
- HEAD comparison for drift detection

### Phase 6: Escalation Policies

- Config-based escalation rules (Lite → Heavy)
- Label/type/size-based routing
- Manual override (`lattice worker run CodeReviewHeavy LAT-42`)

---

## Open Questions

1. **TOML vs JSON for worker definitions?** TOML is more readable for config. JSON is consistent with everything else in `.lattice/`. Leaning TOML since these are human-authored definitions, not machine-generated state.

2. **Worker definitions: in `.lattice/` or in repo root?** `.lattice/workers/` keeps everything together but `.lattice/` is gitignored. These definitions should be version-controlled. Maybe `workers/` at repo root (like `scripts/`), with `.lattice/queue/` for runtime state.

3. **Dispatcher as daemon vs polling?** Daemon is more responsive but needs process management (PID files, restart on crash). Polling is simpler but adds latency. Could start with polling and add daemon mode later.

4. **Should `lattice worker run` post process events automatically?** Yes — the worker system should always maintain the event trail, whether triggered manually or by the dispatcher.

5. **How does a worker get its prompt?** The worker definition points to a prompt file. The dispatcher reads it, injects context (task ID, commit SHA, worktree path), and passes it to the engine. The worker itself doesn't need to know about Lattice internals — it just follows instructions.

6. **Multi-repo workers?** Future concern. For now, workers operate within the repo that contains the `.lattice/` directory. Cross-repo workers would need a coordination layer above individual Lattice instances.

---

## Relationship to Existing Systems

### Hooks (keep as-is)
Hooks remain for lightweight reactions. A hook could *enqueue* a worker (write to `.lattice/queue/`), but the hook itself doesn't spawn the worker.

### Completion Policies (enhanced by workers)
Workers produce the artifacts that satisfy completion policies. The pipeline becomes:
```
task → review → worker fires → artifact attached → policy satisfied → can move to done
```

### `lattice next --claim` (complementary)
`lattice next` is for agents picking up the next available task. Workers are for automated reactions to state changes. An agent might `lattice next --claim` a task, do the work, move it to `review`, and a worker automatically handles the review.

### Dashboard (consumer)
The dashboard reads `active_processes` and renders indicators. It doesn't spawn or manage workers — that's the dispatcher's job.
