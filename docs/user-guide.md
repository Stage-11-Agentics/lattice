# Lattice User Guide

**Lattice is Linear, upgraded for the agent-native era.** Opinionated. Event-sourced. File-based. Built for a world where your teammates think in tokens and act in tool calls.

## Your agents are capable. And they are alone.

Each session starts fresh. Each agent forgets what the last one learned. The plan you spent an hour refining, the debugging insight that took three sessions to reach, the architectural decision and its rationale -- gone when the context window closes. You end up re-explaining context, losing track of what agents did, and coordinating entirely in your head.

Lattice fixes this. Run `lattice init` in your project directory, and your agents get a shared coordination layer -- a `.lattice/` directory that tracks tasks, records every action as an immutable event, and survives across sessions. Your agent reads task state the same way it reads source code: by looking at files.

No server. No signup. No API key. Just files in your project, the way `.git/` is just files in your project.

```bash
pip install lattice-tracker
cd your-project/
lattice init
```

That's all the ceremony. Now every agent that touches this project can see what's happening, what happened before, and what needs to happen next. Your agents just got 10x better -- not because they're smarter, but because they can finally coordinate.

---

## What makes Lattice different

Most project management tools were built for humans with browsers and Slack channels. Lattice was built for a world where the worker might be an agent, the reviewer might be an agent, and the human is the orchestrator who sets direction and makes decisions at the threshold.

**File-based.** `.lattice/` lives in your project like `.git/` does. No database, no cloud dependency. Every language reads files, every agent navigates directories, every tool ever built can open a path and see what's there. Files are the universal substrate.

**Event-sourced.** Every change -- status updates, comments, assignments -- becomes an immutable event. Facts accumulate and don't conflict. Task JSON files are snapshots derived from events. If they ever disagree, events win. `lattice rebuild` replays history to regenerate any snapshot. Systems that store only current state have chosen amnesia as architecture. Lattice remembers everything.

**Agent-native.** `--json` on every command for structured output. `--quiet` for scripted pipelines. Idempotent retries with `--id` so agents can safely retry without duplicates. Actor attribution on every write so you always know who did what.

**Opinionated about coordination, agnostic about workflow.** Lattice gives you statuses, events, relationships, and a work hierarchy. How you use them is up to you. A solo dev with Claude Code and a 10-person team with multiple agent types both find what they need.

---

## Getting started

### Install

```bash
pip install lattice-tracker
# or
uv pip install lattice-tracker
```

For MCP server support (if your agent tools use the Model Context Protocol):

```bash
pip install lattice-tracker[mcp]
```

### Initialize

```bash
cd your-project/
lattice init
```

You'll be asked for two things:

1. **Your identity** -- `human:yourname`. Every change in Lattice is attributed to someone. This is not bureaucracy; it's the foundation of trust in a system where agents act autonomously.
2. **A project code** -- a short prefix like `PROJ` that gives you human-friendly IDs (`PROJ-1`, `PROJ-2`) instead of raw ULIDs.

Non-interactive version:

```bash
lattice init --actor human:alice --project-code PROJ
```

This creates `.lattice/` with config, empty task and event directories, and a reasonable default workflow. Commit it to your repo -- task state lives alongside your code, versioned and accessible to every collaborator and CI system.

### Your first task

```bash
lattice create "Set up project structure" --actor human:alice
# Created PROJ-1: Set up project structure
```

The task starts in `backlog`. Move it through the workflow:

```bash
lattice status PROJ-1 in_planning --actor human:alice
lattice status PROJ-1 planned --actor human:alice
lattice status PROJ-1 in_progress --actor human:alice
lattice status PROJ-1 review --actor human:alice
lattice status PROJ-1 done --actor human:alice
```

Each status change is an immutable event with a timestamp and actor. Run `lattice show PROJ-1` to see the full history.

### Give your agents access

For Claude Code, run:

```bash
lattice setup-claude
```

This injects a block into your `CLAUDE.md` that teaches agents to create tasks before starting work, update status at every transition, attribute actions correctly, and leave breadcrumbs for the next session. Without this block, agents *can* use Lattice if prompted. With it, they do it by default.

For MCP-compatible tools, add the server to your config:

```json
{
  "mcpServers": {
    "lattice": {
      "command": "lattice-mcp"
    }
  }
}
```

For OpenClaw, install the Lattice skill from the OpenClaw registry. The skill teaches the agent the same workflow discipline as the CLAUDE.md block.

---

## Core concepts

### Events are the source of truth

This is the deepest principle in Lattice, the one from which all else follows. Every change is recorded as an immutable event in a per-task JSONL file. Task JSON files in `.lattice/tasks/` are materialized snapshots -- convenient caches, not the real record.

What this means in practice:

- Snapshots can be rebuilt from events at any time (`lattice rebuild`)
- Writes always append the event before updating the snapshot -- crash between the two is recoverable
- Two agents on different machines can append events independently; histories merge through git
- You can always answer "what happened to this task and who did it" by reading the event log

### Actors

Every write operation requires an actor -- `human:alice`, `agent:claude-opus-4`, `team:frontend`. This is how Lattice maintains an audit trail in a world where autonomous agents make changes.

The rule is simple: the actor is whoever made the decision, not whoever typed the command. If you told an agent "fix the login bug" and the agent creates the task, you're the actor -- you made the decision. If the agent independently decides to create a cleanup task while working, the agent is the actor.

Resolution order: `--actor` flag > `LATTICE_ACTOR` env var > `default_actor` in config.

### The work hierarchy

Lattice organizes work in three tiers:

| Tier | Purpose | Who thinks here |
|------|---------|-----------------|
| **Epic** | Strategic intent -- "Build the auth system" | Leads, planners |
| **Ticket** | A deliverable -- "Implement OAuth for backend" | Humans, senior agents |
| **Task** | A unit of execution -- "Write token refresh handler" | Agents |

Epics group tickets. Tickets group tasks. The `subtask_of` relationship connects them. This hierarchy is available, not imposed -- a quick bug fix can be a single task with no parent. Use the structure that fits the work.

### Statuses

The default workflow:

```
backlog --> in_planning --> planned --> in_progress --> review --> done
```

Plus special statuses:
- **`blocked`** -- waiting on an external dependency
- **`needs_human`** -- waiting specifically on a human decision (reachable from any active status)
- **`cancelled`** -- terminal, alongside `done`

Invalid transitions are rejected with an error listing valid options. Override with `--force --reason "..."` when needed -- the override is recorded as part of the event.

### Relationships

Tasks form a graph. Lattice makes the edges explicit:

| Type | Meaning |
|------|---------|
| `blocks` | This task blocks the target |
| `depends_on` | This task depends on the target |
| `subtask_of` | This task is a child of the target |
| `related_to` | Loosely connected |
| `spawned_by` | Created during work on the target |
| `duplicate_of` | Same work, different task |
| `supersedes` | Replaces the target |

```bash
lattice link PROJ-2 depends_on PROJ-1 --actor human:alice --note "Need the API first"
```

`lattice show` displays both outgoing and incoming relationships, so you can see the full context from any node.

---

## Patterns

These aren't hypothetical workflows. They're how we actually use Lattice every day -- the coordination patterns that emerged from building real software with agents.

### Solo dev + Claude Code

The most common pattern. You're one person working with Claude Code, and you want your agent to remember what happened across sessions.

**Setup:**

```bash
lattice init --actor human:you --project-code PROJ
lattice setup-claude
```

**Daily workflow:**

1. Create tasks for what you want to build
2. Start a Claude Code session -- the agent reads the Lattice block in CLAUDE.md
3. The agent creates tasks for its own work, moves them through statuses, leaves comments
4. Next session: the new agent reads `.lattice/` and picks up where the last one left off

**What changes:** Instead of re-explaining "we tried X but it didn't work because Y" at the start of every session, the context is in the event log. The agent reads `lattice show PROJ-5` and sees the full history -- what was tried, what was decided, what's left to do.

```bash
# Agent picks up a task
lattice next --actor agent:claude --claim --json
# Agent works, leaves breadcrumbs
lattice comment PROJ-5 "Root cause: race condition in token refresh" --actor agent:claude
# Agent finishes
lattice status PROJ-5 review --actor agent:claude
```

### Solo dev + OpenClaw

Same pattern, different tool. OpenClaw agents load the Lattice skill from the registry, which teaches the same workflow discipline as the CLAUDE.md block.

The key insight is the same: Lattice is the shared memory layer between sessions. Whether your agent is Claude, GPT, Gemini, or something else, it coordinates through the same `.lattice/` directory.

### Parallel agent builds

When a project is large enough, you can split work across multiple agents running simultaneously. This is where Lattice's coordination primitives really shine -- without shared state, parallel agents step on each other.

**The pattern:**

1. Define the work as independent tasks with clear boundaries
2. Use relationships to express dependencies
3. Launch agents in parallel, each claiming their own task via `lattice next --claim`

**Example: building a feature with frontend and backend work**

```bash
# Create the work graph
lattice create "Auth feature" --type epic --actor human:you
lattice create "Backend: OAuth endpoints" --type ticket --actor human:you
lattice create "Frontend: login flow" --type ticket --actor human:you
lattice link PROJ-3 subtask_of PROJ-2 --actor human:you
lattice link PROJ-4 subtask_of PROJ-2 --actor human:you

# Launch two agents -- each claims different work
# Agent 1 picks PROJ-3 (backend), Agent 2 picks PROJ-4 (frontend)
```

**What makes this work:** Each agent sees the full graph. If Agent 2 needs the API contract from Agent 1, it checks PROJ-3's status and comments. If Agent 1 finishes first and the contract changes, it comments on PROJ-3 and Agent 2 can read the update.

**Interface contracts first:** For builds with parallel agents, define the contracts (protocols, API shapes, shared types) before launching implementation agents. This prevents merge conflicts and ensures agents build against the same interface.

### Sweep: autonomous backlog processing

When you have a stack of well-defined tasks and want an agent to work through them autonomously.

**The loop:**

```
lattice next --claim --> work --> transition --> lattice next --claim --> ...
```

In Claude Code, the `/lattice-sweep` command runs this loop automatically -- the agent claims a task, does the work, transitions it, commits, and moves to the next one. Up to 10 tasks per sweep (safety cap).

**When to use it:**
- Backlog of independent, well-defined tasks
- Tasks are scoped small enough for a single agent session
- You're comfortable reviewing the output afterward

**Post-sweep:**

```bash
# What completed?
lattice list --status done
# What needs you?
lattice list --status needs_human
# What's ready for review?
lattice list --status review
```

The human's job after a sweep: review completed work, unblock `needs_human` items, run another sweep. The agents do the work; you do the judgment.

### The taste-to-code pipeline

Human taste and judgment compound over time when captured structurally. This pattern shows how a review comment becomes an enforced standard.

```
Review finding --> Documentation update --> Lint rule
```

Each step makes the enforcement more mechanical and less dependent on human attention:

```bash
# Step 1: A review finding
lattice create "Prefer shared utils over hand-rolled helpers" --type task --actor human:you
lattice comment PROJ-10 "Found during code review of PROJ-7" --actor human:you

# Step 2: Document it
lattice create "Add util preference to ARCHITECTURE.md" --type task --actor human:you
lattice link PROJ-11 spawned_by PROJ-10 --actor human:you

# Step 3: Enforce it
lattice create "Add lint rule: no hand-rolled helpers" --type task --actor human:you
lattice link PROJ-12 spawned_by PROJ-11 --actor human:you
```

Query later: "Where did this lint rule come from?" Trace the `spawned_by` chain back to the original review finding. Human taste, encoded as machine-enforceable rules, tracked from origin to implementation.

### Team reviews (multi-model)

When the stakes are high, get multiple perspectives. Lattice tracks the review process as events, so the full audit trail is preserved.

**The pattern:**

1. Move a task to `review`
2. Launch review agents (Claude, Codex, Gemini) against the same diff
3. Each agent writes a review file and attaches it as an artifact
4. A synthesis agent merges findings into a single report
5. The human reads the synthesis and decides

```bash
# Task moves to review
lattice status PROJ-5 review --actor agent:claude

# After reviews complete, attach evidence
lattice attach PROJ-5 notes/CR-PROJ-5-synthesis.md \
  --title "Team Review Synthesis" --role review --actor agent:claude

# Human approves
lattice status PROJ-5 done --actor human:you
```

Three models reviewing the same code surface issues no single model catches alone. The synthesis separates high-confidence findings (flagged by multiple reviewers) from single-reviewer observations that need human judgment.

### `needs_human` as a coordination primitive

This is the pattern that prevents agents from getting stuck or making decisions above their pay grade.

When an agent hits a point requiring human judgment -- a design decision, missing credentials, ambiguous requirements -- it signals immediately:

```bash
lattice status PROJ-5 needs_human --actor agent:claude
lattice comment PROJ-5 "Need: REST vs GraphQL decision for the API" --actor agent:claude
```

The agent moves on. The human checks their queue:

```bash
lattice list --status needs_human
```

Makes the decision, comments with the rationale, and moves the task back to an active status:

```bash
lattice comment PROJ-5 "Decision: REST. Rationale: simpler client, matches existing patterns" --actor human:you
lattice status PROJ-5 in_progress --actor human:you
```

The next agent session picks it up with full context. No Slack, no standup, no lost context. The decision is in the event log, attributed and permanent.

---

## The dashboard

Lattice includes a local web UI for when you want the visual perspective.

```bash
lattice dashboard
# Serving at http://127.0.0.1:8799/
```

**Views:**

- **Board** -- Kanban columns per status. Drag and drop to change status. Invalid transitions are blocked visually.
- **List** -- Filterable, searchable table. Filter by status, type, priority, or text search. Toggle to include archived tasks.
- **Activity** -- Recent events across all tasks. The stream of what's been happening.
- **Stats** -- Velocity, time-in-status, blocked task counts, agent activity breakdown. Quality metrics computed from the event log.
- **Web** -- Force-directed graph of task relationships. Nodes are tasks, edges are relationships. Structure becomes visible.

Click any task for the full detail view: title, description, status, comments, relationships, artifacts, and the complete event timeline. Most fields are inline-editable.

The dashboard reads and writes to the same `.lattice/` directory as the CLI. A status change on the board shows up in `lattice list` immediately, and a `lattice comment` from the CLI appears on the dashboard within seconds.

When bound to localhost (the default), the dashboard supports full read-write operations. When exposed to the network (`--host 0.0.0.0`), it automatically enters read-only mode as a security measure.

---

## CLI reference

Every command supports `--json` for structured output and `--quiet` for minimal output (just the ID or "ok"). All write commands require an actor.

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

Validation errors always list valid options, so agents don't need to memorize allowed values. The CLI teaches its own vocabulary.

---

## Extending Lattice

### Event hooks

Lattice fires shell hooks after event writes. Configure them in `.lattice/config.json`:

```json
{
  "hooks": {
    "on_status_change": {
      "review": "echo 'Task {task_id} ready for review'"
    }
  }
}
```

Hooks trigger on specific status transitions, enabling integrations with CI, notifications, or automated review workflows.

### Workers

Lattice workers are autonomous agents that subscribe to events and perform work. Define a worker as a JSON file specifying the trigger event, the command to run, and the context to provide:

```bash
lattice worker run code-review    # Run a worker once
lattice worker list               # See available workers
```

Workers are the building block for fully automated workflows: a task moves to `review`, a hook fires, a worker runs a multi-model code review, attaches the synthesis as an artifact, and comments with the findings.

### Custom events

For domain-specific events beyond the built-in types:

```bash
lattice event PROJ-5 x_deployment_started \
  --data '{"environment": "staging", "sha": "abc123"}' \
  --actor agent:deployer
```

Custom event types must start with `x_`. They're recorded in the task's event log but don't affect the lifecycle log.

### Making it yours

Lattice is open source and designed to be forked. The architecture is deliberately simple:

- `core/` -- pure business logic, no I/O
- `storage/` -- filesystem operations
- `cli/` -- wires them together via Click commands
- `dashboard/` -- read-only local web UI (stdlib HTTP server, no build step)

The on-disk format (events, snapshots, config) is the stable contract. The CLI can be rewritten. The dashboard can be replaced. But the events, the file layout, the schema -- those are load-bearing walls. Build on them with confidence.

---

## What Lattice is not

Lattice is not a database. It's not a cloud service. It's not a replacement for Jira or Linear for teams that need those tools.

Lattice is coordination infrastructure for agent-first development. If you're tracking agent work in markdown files, folder conventions, or ad-hoc shell scripts -- Lattice replaces all of that with a system that's event-sourced, attributed, and built for the way agents actually work.

If your agents are powerful but uncoordinated, Lattice is the missing piece. Not more intelligence -- more coordination. That's the unlock.

---

*Lattice is proudly built by minds of both kinds. The event log records who did what. The philosophy explains why it matters. Read it at [Philosophy_v3.md](../Philosophy_v3.md).*
