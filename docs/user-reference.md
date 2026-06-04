# Lattice User Reference

Technical reference for Lattice's concepts, on-disk format, workflow patterns, and CLI. For a high-level overview, see the [User Guide](user-guide.md).

---

## The work hierarchy

All work items are **tasks**. Grouping is done via `subtask_of` relationships — any task can serve as a parent for other tasks.

| Scale | Example | Pattern |
|-------|---------|---------|
| Strategic | "Build the auth system" | Parent task with subtasks |
| Tactical | "Implement OAuth for backend" | Standalone task or subtask |

A quick bug fix can be a single task with no parent. A large feature can be a parent task with subtasks linked via `subtask_of`.

---

## Statuses and transitions

```
backlog --> in_planning --> planned --> in_progress --> review --> done
                                           ↕
                                        blocked
```

Plus `cancelled` (reachable from any status).

Invalid transitions are rejected with an error listing valid options. Override with `--force --reason "..."`.

**`blocked`** is for external dependencies — waiting on a third-party API, a CI fix, another team's deliverable. It is a *status*: the work genuinely can't proceed until the external event happens.

**`needs-human`** is a **flag**, not a status — see [The needs-human flag](#the-needs-human-flag) below. It signals that a task requires human judgment (a design decision, missing access, ambiguous requirements) and rides orthogonally on top of whatever status the task is in. `blocked` and `needs-human` can coexist: a task can be `blocked` on an external event *and* flagged for a human decision at once.

---

## The needs-human flag

`needs-human` is an orthogonal flag stored on the task snapshot, not a workflow status. The field is `null` when absent, or an object recording who raised it, why, and when:

```json
{
  "needs_human": {
    "flagged_by": "agent:claude-cli",
    "reason": "Which OAuth provider should we support?",
    "since": "2026-06-03T14:22:00Z"
  }
}
```

Because the flag is independent of status, a task can be `in_progress` and flagged, `blocked` and flagged, even `done` and flagged. Setting or clearing the flag never moves the task between statuses.

```bash
# Set — reason is REQUIRED. The task keeps its current status.
lattice needs-human LAT-42 "Which OAuth provider should we support?" --actor agent:claude

# Clear — optional --note records how it was resolved.
lattice needs-human LAT-42 --clear --note "Decided: Google + GitHub" --actor human:atin
```

The required reason structurally enforces the scannable-queue convention. Setting the flag emits a `needs_human_flagged` event; clearing it emits `needs_human_cleared`. Both are fully attributed in the task event log, and both commands support `--json`.

**The queue:** `lattice list --needs-human` lists every flagged task across all statuses, each with its reason. `lattice show` renders a prominent `NEEDS HUMAN (since ..., by ...): reason` line, and `lattice weather` keys its needs-human section off the flag. `lattice next` never suggests a flagged task in any status — a flagged task is waiting on a human, not on an agent.

---

## Relationships

| Type | Meaning |
|------|---------|
| `blocks` | This task blocks the target |
| `depends_on` | This task depends on the target |
| `subtask_of` | This task is a child of the target |
| `related_to` | Loosely connected |
| `spawned_by` | Created during work on the target |
| `duplicate_of` | Same work, different task |
| `supersedes` | Replaces the target |

The dashboard's Web view renders these as an interactive force-directed graph.

---

## Short IDs

When a `project_code` is configured (e.g., `PROJ`), tasks get human-friendly aliases like `PROJ-42`. Short IDs work anywhere a task ID is expected — CLI commands, dashboard URLs, comments. Under the hood, everything is a ULID (`task_01HQ...`); short IDs are an index that maps to them. The index rebuilds from events via `lattice rebuild --all`.

---

## Events and the event log

Every change is recorded as an immutable event in a per-task JSONL file at `.lattice/events/<task_id>.jsonl`. Events are the source of truth. Task JSON files at `.lattice/tasks/<task_id>.json` are materialized snapshots — convenient caches that can be rebuilt at any time.

Write path:
1. Acquire file lock (`.lattice/locks/`)
2. Append event to JSONL (compact JSON, single line, flushed immediately)
3. Update materialized snapshot (atomic write: temp file → fsync → rename)

If the process crashes between steps 2 and 3, `lattice rebuild` recovers by replaying events.

Multi-lock operations (e.g., linking two tasks) acquire locks in deterministic (sorted) order to prevent deadlocks.

### Event types

Built-in: `task_created`, `status_changed`, `assigned`, `comment_added`, `field_updated`, `relationship_added`, `relationship_removed`, `artifact_attached`, `file_linked`, `file_unlinked`, `needs_human_flagged`, `needs_human_cleared`, `task_archived`, `task_unarchived`.

Custom: any `x_`-prefixed type via `lattice event`. Useful for domain-specific events like deployments, test runs, or releases.

### Provenance

Events support an optional `provenance` field for deep attribution:

- `triggered_by` — what caused this action
- `on_behalf_of` — who the action is really for
- `reason` — why this action was taken

CLI flags: `--triggered-by`, `--on-behalf-of`, `--reason`. All write commands support these.

---

## On-disk layout

```
.lattice/
├── config.json                    # Workflow, statuses, transitions, WIP limits, project_code
├── ids.json                       # Short ID index (short_id -> ULID mapping + next_seq)
├── tasks/<task_id>.json           # Materialized task snapshots
├── events/<task_id>.jsonl         # Per-task event logs (append-only)
├── events/_lifecycle.jsonl        # Lifecycle event log (derived, rebuildable)
├── artifacts/meta/<art_id>.json   # Artifact metadata
├── artifacts/payload/<art_id>.*   # Artifact payloads
├── plans/<task_id>.md             # Structured plan files (scaffolded on create)
├── notes/<task_id>.md             # Scratchpad notes (created on demand)
├── archive/                       # Mirrors structure for archived items
│   ├── tasks/
│   ├── events/
│   ├── plans/
│   └── notes/
└── locks/                         # Internal lock files for concurrency
```

Plans and notes are non-authoritative supplementary files — edited directly by humans or agents, not derived from events.

---

## File-decision links

Tasks can be linked to files they shaped via `lattice file-link`. This records *why* a file was built a certain way by connecting it to the task whose decisions influenced it.

### Linking

```bash
lattice file-link LAT-42 src/auth/jwt.ts --reason "JWT validation logic" --actor agent:claude
lattice file-link LAT-42 src/auth/jwt.ts src/middleware/auth.ts --actor agent:claude
```

Paths are normalized to project-relative form (strip `./`, resolve `..`, reject paths escaping the project root). The optional `--reason` annotates *why* this file is linked — displayed by `lattice explain` without requiring the reader to open the full task.

Linked files are stored in the task snapshot as objects:

```json
{
  "linked_files": [
    {"path": "src/auth/jwt.ts", "reason": "JWT validation logic"},
    {"path": "src/middleware/auth.ts", "reason": null}
  ]
}
```

### Unlinking

```bash
lattice file-unlink LAT-42 src/auth/jwt.ts --actor agent:claude
```

### Explaining

Reverse lookup — given a file, show what decisions shaped it:

```bash
lattice explain src/auth/jwt.ts              # exact file
lattice explain src/auth/                    # all files under directory
lattice explain "src/auth/*.ts"              # glob match
```

`explain` scans all task snapshots for matching `linked_files` entries and also parses `Decisions.md` for `- Files:` lines referencing the path. Output includes task title, status, description excerpt, reason annotation, and any decision-role comments.

Output modes: human-readable (default), `--json` (structured), `--verbose` (full comments and plan content).

### Conventions

- Link files when a task involves a **meaningful architectural or design decision** — not for every file touched.
- A task that refactors 50 files doesn't need 50 links. A task that decides "we use JWT instead of sessions" links the 2-3 files that embody that decision.
- Known limitation: when a file is renamed, linked paths become stale. The old path still returns results; the new path won't until re-linked.

---

## Patterns

### The advance

The pattern that turns a prioritized backlog into completed work — one task at a time:

1. **`lattice next --claim`** — atomically grab the top task and move it to `in_progress`
2. **Work** — implement, test, iterate
3. **Hand off** — move to `review` (done) or `blocked` (external dependency), or raise the `needs-human` flag (stuck on a human decision; the task keeps its status)
4. **Comment** — record what was done, what was chosen, what's left
5. **Commit** — save the work
6. **Report** — tell the user what happened

In Claude Code, `/lattice` teaches the agent the full lifecycle including advancing. For multiple advances, just invoke it again or say "do N advances."

### Parallel agent builds

Split large work across agents running simultaneously. Each claims its own task via `lattice next --claim`. They see each other's progress through `.lattice/`.

```bash
# Define the work graph
lattice create "Auth feature" --actor human:you
lattice create "Backend: OAuth endpoints" --actor human:you
lattice create "Frontend: login flow" --actor human:you
lattice link PROJ-3 subtask_of PROJ-2 --actor human:you
lattice link PROJ-4 subtask_of PROJ-2 --actor human:you

# Launch agents in parallel -- each claims different work
```

Define interface contracts (protocols, API shapes, shared types) before launching implementation agents. This prevents merge conflicts and ensures agents build against the same interface.

### Team reviews (multi-model)

High-stakes changes get multiple perspectives. Launch review agents from different models against the same diff:

1. Task moves to `review`
2. Claude, Codex, and Gemini each review independently
3. Each writes findings and attaches them as artifacts
4. A synthesis agent merges into one report
5. You read the synthesis and decide

Three models surface issues no single model catches alone. The synthesis separates high-confidence findings (flagged by multiple reviewers) from observations that need your judgment.

### Auto-fire on status transitions

Transitioning a task to `review` or `planned` automatically spawns the matching review subprocess in the background:

| Transition | What fires | Default mode |
|------------|------------|--------------|
| `→ review` | `lattice code-review <task>` (detached) | `review_mode` (default `single`) |
| `→ planned` | `lattice plan-review <task>` (detached) | `plan_review_mode` (default `triple`) |

Both default to enabled. Disable via:

- `--no-auto-review` on `lattice status` (per-call opt-out).
- `auto_code_review_on_transition: false` and/or `auto_plan_review_on_transition: false` in `.lattice/config.json` (project-wide).

Coordination piggybacks on the existing `review_state/<task_id>.json` primitive — first-writer-wins. If a review is already in flight from a manual `lattice code-review` (or another auto-fire on a different machine), the second auto-fire is a no-op and reports `auto-review skipped (review already in flight, pid …)`.

Logs land at `.lattice/.daemon/auto-{code,plan}-review-<task_id>.log` (overwritten per spawn). Every successful spawn appends an `auto_review_spawned` event to the task's event log with the review type, mode, log path, spawned-at timestamp, child PID (debug aid only), and the triggering `status_changed` event ID.

Spawn failures (no `lattice` on PATH, OS forbids fork) never block the status transition — the change still lands and a warning is logged. Auto-fire is enhancement, never gating.

**Cost-of-ownership.** With `triple` mode (the default for `plan_review_mode`), every transition into `planned` or `review` spends three agent runs plus a merge run. For projects where API spend matters, set the config keys to `false` or use `--no-auto-review` for surgical transitions.

### The taste-to-code pipeline

Human taste compounds when captured structurally:

```
Review finding --> Documentation update --> Lint rule
```

```bash
lattice create "Prefer shared utils over hand-rolled helpers" --actor human:you
lattice create "Add util preference to ARCHITECTURE.md" --actor human:you
lattice create "Add lint rule: no hand-rolled helpers" --actor human:you
lattice link PROJ-11 spawned_by PROJ-10 --actor human:you
lattice link PROJ-12 spawned_by PROJ-11 --actor human:you
```

Each step makes enforcement more mechanical. Query later: "Where did this lint rule come from?" Trace the `spawned_by` chain back to the original review finding.

---

## Extending Lattice

### Event hooks

Shell hooks that fire after event writes. Configure in `.lattice/config.json`:

```json
{
  "hooks": {
    "transitions": {
      "* -> review": "echo 'Task {task_id} ready for review'"
    }
  }
}
```

### Custom events

Domain-specific events beyond the built-in types. Any `x_`-prefixed type name is valid:

```bash
lattice event PROJ-5 x_deployment_started \
  --data '{"environment": "staging", "sha": "abc123"}' \
  --actor agent:deployer
```

### Making it yours

Lattice is open source and designed to be forked. The on-disk format (events, snapshots, config) is the stable contract. The CLI can be rewritten. The dashboard can be replaced. The events are load-bearing walls. Build on them with confidence.

---

## CLI reference

The CLI is Lattice's write interface — the primary way agents interact with the system. Every command supports `--json` for structured output and `--quiet` for minimal output. All write commands require an actor.

### Commands

| Command | What it does |
|---------|-------------|
| `lattice init` | Create `.lattice/` in your project |
| `lattice create <title>` | Create a task |
| `lattice status <id> <status>` | Change task status |
| `lattice needs-human <id> "<reason>"` | Flag a task for human attention (reason required); `--clear [--note ...]` to clear |
| `lattice migrate needs-human` | Convert tasks in the legacy `needs_human` status to the flag (`--dry-run` to preview) |
| `lattice assign <id> <actor>` | Assign a task |
| `lattice comment <id> "<text>"` | Add a comment (`--role` optionally tags it for completion policies) |
| `lattice update <id> field=value` | Update task fields |
| `lattice list` | List tasks (filterable by status, type, tag, assignee) |
| `lattice show <id>` | Full task details with history |
| `lattice next` | Get the highest-priority available task |
| `lattice link <src> <type> <tgt>` | Create a relationship |
| `lattice unlink <src> <type> <tgt>` | Remove a relationship |
| `lattice attach <id> <file-or-url>` | Attach an artifact (`--role` optionally tags it for completion policies) |
| `lattice event <id> <x_type>` | Record a custom event |
| `lattice file-link <id> <path>...` | Link file(s) to a task (`--reason` for annotation) |
| `lattice file-unlink <id> <path>...` | Unlink file(s) from a task |
| `lattice explain <path>` | Show decisions behind a file (supports directory/glob) |
| `lattice archive <id>` | Archive a completed task |
| `lattice unarchive <id>` | Restore an archived task |
| `lattice dashboard` | Launch the web dashboard |
| `lattice restart` | Restart a running dashboard (sends SIGHUP) |
| `lattice doctor` | Check project integrity |
| `lattice rebuild <id\|--all>` | Rebuild snapshots from events |
| `lattice setup-claude` | Add/update CLAUDE.md integration block |
| `lattice setup-claude-skill` | Install Lattice skill for Claude Code |
| `lattice setup-codex` | Install Lattice skill for Codex CLI |
| `lattice setup-openclaw` | Install Lattice skill for OpenClaw |
| `lattice setup-prompt` | Print agent instructions to stdout |

### Flags

- `--json` — structured output (all commands)
- `--quiet` — just the ID (all commands)
- `--actor` — who is performing the action (all write commands)
- `--type` — task, bug, spike, chore (create/list)
- `--priority` — critical, high, medium, low (create/list)
- `--assigned` / `--assigned-to` — filter/set assignee (list/create)
- `--tag` / `--tags` — filter/set tags (list/create)
- `--force --reason "..."` — override workflow constraints (status)
- `--claim` — atomically assign and start a task (next)
- `--id` — supply your own ID for idempotent retries (create/event)
- `--role` — assign a semantic role to comments/artifacts (comment/attach)
- `--reason` — annotate why a file is linked to a task (file-link)

Validation errors always list valid options. The CLI teaches its own vocabulary.

### Completion policies

If your workflow requires review evidence before `done`, use `--role` to
satisfy role-based gates (`require_roles`) with lightweight, explicit records.

```bash
lattice comment TASK "Reviewed diffs and validated acceptance criteria." \
  --role review --actor agent:claude

lattice attach TASK review-notes.md --role review --actor agent:claude
```

Both examples add `review` role evidence that completion policies can validate.

### Actor resolution

Resolution order: `--actor` flag > `LATTICE_ACTOR` env var > `default_actor` in config.

Actor format is `prefix:identifier` — e.g., `human:alice`, `agent:claude-opus-4`, `team:frontend`. No registry; validation is format-only.

---

*Lattice is proudly built by minds of both kinds. The event log records who did what. The philosophy explains why it matters. Read it at [Philosophy.md](../Philosophy.md).*
