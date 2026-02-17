# Lattice User Guide

**Lattice is Linear, upgraded for the agent-native era.** Opinionated. Event-sourced. File-based. Built for a world where your teammates think in tokens and act in tool calls.

## What Lattice gives you

You create tasks. Your agents claim them, work them, and report back -- across sessions, across models, across tools. Every action is recorded as an immutable event. Nothing is lost between context windows.

**In Claude Code**, you type `/lattice-sweep` and walk away. Your agent claims the highest-priority task from the backlog, does the work, commits, moves to the next one, and keeps going. When it hits something that needs your judgment, it flags it `needs_human` with a comment explaining what it needs. You come back to a list of completed work and a short queue of decisions only you can make.

**In OpenClaw**, your Claude bot loads the Lattice skill and gets the same discipline. It reads the backlog, claims work, leaves breadcrumbs for the next session. Different tool, same coordination layer.

**With multiple agents**, you launch three in parallel -- Claude on the backend, Codex on the frontend, Gemini on tests. Each claims its own task via `lattice next --claim`. They see each other's progress through the shared `.lattice/` directory. No stepping on each other. No lost context. No coordination meetings.

**The sweep loop** is the pattern that makes Lattice click:

```
You prioritize the backlog
  --> Agent claims top task
    --> Agent works, commits, transitions
      --> Agent claims next task
        --> ...repeats until blocked or done
          --> You review, unblock, re-sweep
```

You do the thinking. Agents do the throughput. Lattice is the coordination layer between.

### Three minutes to working

```bash
pip install lattice-tracker
cd your-project/
lattice init
lattice setup-claude            # if using Claude Code
```

That's it. Your agents now track their own work, leave context for the next session, and coordinate through files -- the same way they read your source code.

---

## What it looks like in practice

### A day with Claude Code

Morning. You have ideas for what needs building. Put them in the backlog:

```bash
lattice create "Add OAuth login flow" --priority high --actor human:you
lattice create "Fix token refresh race condition" --type bug --priority critical --actor human:you
lattice create "Write API integration tests" --actor human:you
```

Open Claude Code. The agent reads your CLAUDE.md, sees the Lattice block, and knows the drill. You say "sweep the backlog" or run `/lattice-sweep`. The agent:

1. Runs `lattice next --claim` -- picks the critical bug first
2. Investigates, fixes, commits
3. Comments: "Root cause: stale token cache. Fixed with TTL-based invalidation."
4. Moves the task to `review`
5. Claims the next task -- the OAuth flow
6. Gets partway through, realizes it needs a design decision
7. Moves to `needs_human`: "Need: social providers to support (Google only? Google + GitHub?)"
8. Claims the test-writing task, finishes it, moves to `review`

You come back. Three tasks touched, two in review, one waiting on you:

```bash
lattice list --status needs_human
# PROJ-2  needs_human  high  "Add OAuth login flow"
```

You make the call, leave the decision in the record:

```bash
lattice comment PROJ-2 "Google + GitHub. Skip Apple for now." --actor human:you
lattice status PROJ-2 in_progress --actor human:you
```

Next session picks it up with full context. No re-explaining. The decision is in the event log.

### A day with OpenClaw

Same pattern, different surface. Your Claude bot loads the Lattice skill from the registry. It reads `.lattice/`, sees the backlog, and works through tasks the same way. The coordination layer doesn't care which tool drives it -- `.lattice/` is just files. Any agent that can read a file and run a command can participate.

### The sweep

Sweep is the pattern that turns a prioritized backlog into completed work with minimal human involvement. The loop:

1. **`lattice next --claim`** -- atomically grab the top task and move it to `in_progress`
2. **Work** -- implement, test, iterate
3. **Transition** -- move to `review` (done), `needs_human` (stuck on a decision), or `blocked` (external dependency)
4. **Comment** -- record what was done, what was chosen, what's left
5. **Commit** -- save the work
6. **Repeat** -- claim the next task

In Claude Code, `/lattice-sweep` runs this loop automatically (up to 10 tasks per sweep as a safety cap). After a sweep:

```bash
lattice list --status review        # ready for your eyes
lattice list --status needs_human   # decisions only you can make
lattice list --status blocked       # waiting on something external
```

Review the work. Unblock what's stuck. Run another sweep. The agents produce throughput; you produce judgment. That's the division of labor.

---

## Install and setup

```bash
pip install lattice-tracker
# or
uv pip install lattice-tracker
```

For MCP server support:

```bash
pip install lattice-tracker[mcp]
```

### Initialize in your project

```bash
cd your-project/
lattice init
```

You'll set your identity (`human:yourname`) and a project code (like `PROJ` for IDs like `PROJ-1`). Non-interactive:

```bash
lattice init --actor human:alice --project-code PROJ
```

This creates `.lattice/` -- commit it to your repo. Task state lives alongside your code, versioned and visible to every collaborator and CI system.

### Connect your agents

**Claude Code:**

```bash
lattice setup-claude
```

Injects a block into `CLAUDE.md` that teaches agents the workflow: create tasks before working, update status at transitions, attribute actions, leave breadcrumbs. Without this block, agents can use Lattice if prompted. With it, they do it by default.

**MCP-compatible tools:**

```json
{
  "mcpServers": {
    "lattice": {
      "command": "lattice-mcp"
    }
  }
}
```

**OpenClaw:** Install the Lattice skill from the OpenClaw registry.

---

## Patterns we live

These aren't hypothetical workflows. They're how we actually build software with agents every day.

### Parallel agent builds

When work is large enough, split it across agents running simultaneously. Each claims its own task via `lattice next --claim`. They see each other's progress through `.lattice/`.

```bash
# Define the work graph
lattice create "Auth feature" --type epic --actor human:you
lattice create "Backend: OAuth endpoints" --type ticket --actor human:you
lattice create "Frontend: login flow" --type ticket --actor human:you
lattice link PROJ-3 subtask_of PROJ-2 --actor human:you
lattice link PROJ-4 subtask_of PROJ-2 --actor human:you

# Launch agents in parallel -- each claims different work
```

Define interface contracts (protocols, API shapes, shared types) before launching implementation agents. This prevents merge conflicts and ensures agents build against the same interface.

### `needs_human` -- the coordination primitive

When an agent hits something above its pay grade -- a design decision, missing credentials, ambiguous requirements -- it signals immediately:

```bash
lattice status PROJ-5 needs_human --actor agent:claude
lattice comment PROJ-5 "Need: REST vs GraphQL for the API" --actor agent:claude
```

The agent moves on to other work. You check your queue when ready:

```bash
lattice list --status needs_human
```

Make the decision, record it, move the task back:

```bash
lattice comment PROJ-5 "REST. Simpler client, matches existing patterns." --actor human:you
lattice status PROJ-5 in_progress --actor human:you
```

No Slack. No standup. No lost context. The decision is in the event log, attributed and permanent.

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

## How it works under the hood

### Events are the source of truth

Every change is recorded as an immutable event in a per-task JSONL file. Task JSON files are materialized snapshots -- convenient caches, not the real record.

- Snapshots rebuild from events at any time (`lattice rebuild`)
- Writes append the event before updating the snapshot -- crash-recoverable
- Two agents on different machines append independently; histories merge through git
- The full audit trail answers "what happened and who did it" for every task

### Actors

Every write requires an actor -- `human:alice`, `agent:claude-opus-4`, `team:frontend`. The actor is whoever made the decision, not whoever typed the command.

Resolution order: `--actor` flag > `LATTICE_ACTOR` env var > `default_actor` in config.

### The work hierarchy

| Tier | Purpose | Who thinks here |
|------|---------|-----------------|
| **Epic** | Strategic intent -- "Build the auth system" | Leads, planners |
| **Ticket** | A deliverable -- "Implement OAuth for backend" | Humans, senior agents |
| **Task** | A unit of execution -- "Write token refresh handler" | Agents |

Available, not imposed. A quick bug fix can be a single task with no parent.

### Statuses

```
backlog --> in_planning --> planned --> in_progress --> review --> done
```

Plus `blocked`, `needs_human` (reachable from any active status), and `cancelled`. Invalid transitions are rejected with an error listing valid options. Override with `--force --reason "..."`.

### Relationships

| Type | Meaning |
|------|---------|
| `blocks` | This task blocks the target |
| `depends_on` | This task depends on the target |
| `subtask_of` | This task is a child of the target |
| `related_to` | Loosely connected |
| `spawned_by` | Created during work on the target |
| `duplicate_of` | Same work, different task |
| `supersedes` | Replaces the target |

`lattice show` displays both outgoing and incoming relationships from any node.

---

## The dashboard

A local web UI for when you want the visual perspective.

```bash
lattice dashboard
# Serving at http://127.0.0.1:8799/
```

- **Board** -- Kanban columns per status. Drag and drop.
- **List** -- Filterable, searchable table.
- **Activity** -- Recent events across all tasks.
- **Stats** -- Velocity, time-in-status, blocked counts, agent activity.
- **Web** -- Force-directed graph of task relationships.

Click any task for full detail: description, comments, relationships, artifacts, complete event timeline. Most fields are inline-editable.

Reads and writes to the same `.lattice/` as the CLI. Read-only mode when exposed to the network.

---

## CLI reference

Every command supports `--json` for structured output and `--quiet` for minimal output. All write commands require an actor.

| Command | What it does |
|---------|-------------|
| `lattice init` | Create `.lattice/` in your project |
| `lattice create <title>` | Create a task |
| `lattice status <id> <status>` | Change task status |
| `lattice assign <id> <actor>` | Assign a task |
| `lattice comment <id> "<text>"` | Add a comment |
| `lattice update <id> field=value` | Update task fields |
| `lattice list` | List tasks (filterable by status, type, tag, assignee) |
| `lattice show <id>` | Full task details with history |
| `lattice next` | Get the highest-priority available task |
| `lattice link <src> <type> <tgt>` | Create a relationship |
| `lattice unlink <src> <type> <tgt>` | Remove a relationship |
| `lattice attach <id> <file-or-url>` | Attach an artifact |
| `lattice event <id> <x_type>` | Record a custom event |
| `lattice archive <id>` | Archive a completed task |
| `lattice unarchive <id>` | Restore an archived task |
| `lattice dashboard` | Launch the web dashboard |
| `lattice doctor` | Check project integrity |
| `lattice rebuild <id\|--all>` | Rebuild snapshots from events |
| `lattice setup-claude` | Add/update CLAUDE.md integration block |

### Useful flags

- `--json` -- structured output (all commands)
- `--quiet` -- just the ID (all commands)
- `--type` -- task, ticket, epic, bug, spike, chore (create/list)
- `--priority` -- critical, high, medium, low (create/list)
- `--assigned` / `--assigned-to` -- filter/set assignee (list/create)
- `--tag` / `--tags` -- filter/set tags (list/create)
- `--force --reason "..."` -- override workflow constraints (status)
- `--claim` -- atomically assign and start a task (next)
- `--id` -- supply your own ID for idempotent retries (create/event)

Validation errors always list valid options. The CLI teaches its own vocabulary.

---

## Extending Lattice

### Event hooks

Shell hooks that fire after event writes:

```json
{
  "hooks": {
    "on_status_change": {
      "review": "echo 'Task {task_id} ready for review'"
    }
  }
}
```

### Workers

Autonomous agents that subscribe to events. A task moves to `review`, a hook fires, a worker runs a multi-model code review and attaches the synthesis:

```bash
lattice worker run code-review
lattice worker list
```

### Custom events

Domain-specific events beyond the built-in types:

```bash
lattice event PROJ-5 x_deployment_started \
  --data '{"environment": "staging", "sha": "abc123"}' \
  --actor agent:deployer
```

### Making it yours

Lattice is open source and designed to be forked. The on-disk format (events, snapshots, config) is the stable contract. The CLI can be rewritten. The dashboard can be replaced. The events are load-bearing walls. Build on them with confidence.

---

*Lattice is proudly built by minds of both kinds. The event log records who did what. The philosophy explains why it matters. Read it at [Philosophy_v3.md](../Philosophy_v3.md).*
