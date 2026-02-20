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
                                           ↕            ↕
                                        blocked      needs_human
```

Plus `cancelled` (reachable from any status).

Invalid transitions are rejected with an error listing valid options. Override with `--force --reason "..."`.

**`needs_human`** is reachable from any active status. It signals that the task requires human judgment — a design decision, missing access, ambiguous requirements. Agents should always leave a comment explaining what they need.

**`blocked`** is for external dependencies — waiting on a third-party API, a CI fix, another team's deliverable. Distinct from `needs_human` because it doesn't require a human *decision*, just a human *action* or an external event.

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

Built-in: `task_created`, `status_changed`, `assigned`, `comment_added`, `field_updated`, `relationship_added`, `relationship_removed`, `artifact_attached`, `task_archived`, `task_unarchived`.

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

## Patterns

### The advance

The pattern that turns a prioritized backlog into completed work — one task at a time:

1. **`lattice next --claim`** — atomically grab the top task and move it to `in_progress`
2. **Work** — implement, test, iterate
3. **Transition** — move to `review` (done), `needs_human` (stuck on a decision), or `blocked` (external dependency)
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
| `lattice archive <id>` | Archive a completed task |
| `lattice unarchive <id>` | Restore an archived task |
| `lattice dashboard` | Launch the web dashboard |
| `lattice restart` | Restart a running dashboard (sends SIGHUP) |
| `lattice doctor` | Check project integrity |
| `lattice rebuild <id\|--all>` | Rebuild snapshots from events |
| `lattice setup-claude` | Add/update CLAUDE.md integration block |
| `lattice setup-openclaw` | Install Lattice skill for OpenClaw |

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
