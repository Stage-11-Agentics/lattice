# Lattice User Guide

**Lattice is Linear, upgraded for the agent-native era.** Opinionated. Event-sourced. File-based. Built for a world where your teammates think in tokens and act in tool calls.

## Two interfaces, two audiences

Lattice has two surfaces, each designed for the mind that uses it:

**The dashboard** is for you. It's a local web UI where you see the state of your project at a glance — a Kanban board, an activity feed, stats, a relationship graph. You create tasks, make decisions, review work, and unblock your agents here. If you never touch the terminal, you can still run a full Lattice workflow through the dashboard.

**The CLI** is for your agents. When Claude Code reads your CLAUDE.md, it learns the Lattice commands and uses them autonomously — creating tasks, claiming work, transitioning statuses, leaving comments. The CLI is the agent's native interface to the coordination layer. You'll run a few CLI commands during setup, but day-to-day, the dashboard is where you live.

## Three minutes to working

```bash
pip install lattice-tracker
cd your-project/
lattice init
lattice setup-claude            # if using Claude Code
lattice dashboard               # open the dashboard
```

That's it. Your agents now track their own work through the CLI. You watch, steer, and decide through the dashboard.

---

## The dashboard

```bash
lattice dashboard
# Serving at http://127.0.0.1:8799/
```

The dashboard reads and writes the same `.lattice/` directory your agents use. Everything stays in sync — an agent commits a status change via CLI, and the dashboard reflects it on your next refresh.

### What you see

- **Board** — Kanban columns per status. Drag tasks between columns to change their status. This is the primary view for understanding where everything stands.
- **List** — Filterable, searchable table. Good for finding specific tasks or slicing by priority, type, tag, or assignee.
- **Activity** — A chronological feed of recent events across all tasks. See what your agents have been doing since you last checked.
- **Stats** — Velocity, time-in-status distributions, blocked counts, agent activity. The numbers behind the work.
- **Web** — A force-directed graph of task relationships. See how epics, tasks, and dependencies connect visually.

### What you do

Click any task to open its detail panel. From there you can:

- Edit the title, description, priority, type, and tags inline
- Change status (or drag on the board)
- Add comments — decisions, feedback, context for the next agent session
- View the complete event timeline — every status change, assignment, and comment, attributed and timestamped
- Open the task's plan or notes files directly in your editor

Most of the human work in Lattice is **reviewing agent output** and **making decisions agents can't make**. The dashboard is designed for exactly this loop.

---

## The sweep: how agents work your backlog

The sweep is the pattern that makes Lattice click. Here's what it looks like from your side:

### 1. You fill the backlog

Create tasks in the dashboard. Set priorities. Define epics and link subtasks. This is the thinking work — deciding *what* matters and *in what order*.

### 2. Agents claim and execute

You tell your agent to sweep (in Claude Code: `/lattice-sweep`, or just "sweep the backlog"). The agent:

- Claims the highest-priority available task
- Works it — implements, tests, iterates
- Leaves a comment explaining what it did and why
- Moves the task to `review`
- Claims the next one
- Repeats until the backlog is empty or it hits something it can't resolve alone

### 3. You come back to a sorted inbox

Open the dashboard. The board tells the story:

- **Review column** — work that's done and ready for your eyes
- **Needs Human column** — decisions only you can make, each with a comment explaining what the agent needs
- **Blocked column** — tasks waiting on something external

You review the work, make the calls, and unblock what's stuck. Then sweep again. The agents produce throughput; you produce judgment. That's the division of labor.

---

## `needs_human` — the async handoff

This is the coordination primitive that makes human-agent collaboration practical.

When an agent hits something above its pay grade — a design decision, missing credentials, ambiguous requirements — it moves the task to `needs_human` and leaves a comment: *"Need: REST vs GraphQL for the public API."*

The agent doesn't wait. It moves on to other work. You see the task in the Needs Human column on the dashboard whenever you're ready. You add your decision as a comment, drag the task back to In Progress, and the next agent session picks it up with full context.

No Slack. No standup. No re-explaining. The decision is in the event log, attributed and permanent.

---

## How it works under the hood

### Events are the source of truth

Every change — status transitions, assignments, comments, relationship links — is recorded as an immutable event. Task files are materialized snapshots for fast reads, but events are the real record. If they ever disagree, `lattice rebuild` replays events to regenerate snapshots.

This means:
- Full audit trail: "what happened and who did it" for every task
- Crash recovery: events are append-only, snapshots are rebuildable
- Git-friendly: two agents on different machines can append independently and merge through git

### Actors

Every write is attributed. `human:alice` made that design call. `agent:claude-opus-4` fixed that bug. `team:frontend` owns that epic. Attribution follows authorship of the *decision*, not who typed the command.

### Statuses

```
backlog --> in_planning --> planned --> in_progress --> review --> done
```

Plus `blocked`, `needs_human` (reachable from any active status), and `cancelled`.

### Relationships

Tasks connect to each other: `blocks`, `depends_on`, `subtask_of`, `related_to`, `spawned_by`, `duplicate_of`, `supersedes`. The Web view on the dashboard visualizes these as an interactive graph.

### Files, not a database

All state lives in `.lattice/` as JSON and JSONL files, right next to your source code. Commit it to your repo. It's versioned, diffable, and visible to every collaborator and CI system. No server, no database, no account.

---

## Setup details

### Install

```bash
pip install lattice-tracker
# or
uv pip install lattice-tracker
```

For MCP server support (agent integration via tool-use protocol):

```bash
pip install lattice-tracker[mcp]
```

### Initialize

```bash
cd your-project/
lattice init
```

You'll set your identity (`human:yourname`) and a project code (like `PROJ` for IDs like `PROJ-1`). Commit the `.lattice/` directory to your repo.

### Connect your agents

**Claude Code:**

```bash
lattice setup-claude
```

Adds a block to your project's `CLAUDE.md` that teaches agents the full workflow — create tasks before working, update status at transitions, leave breadcrumbs. Without this block, agents can use Lattice if prompted. With it, they do it by default.

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

**OpenClaw:**

```bash
lattice setup-openclaw
```

---

## Going deeper

For the full CLI reference, detailed patterns, extension points, and technical internals, see the [User Reference](user-reference.md).

---

*Lattice is proudly built by minds of both kinds. The event log records who did what. The philosophy explains why it matters. Read it at [Philosophy_v3.md](../Philosophy_v3.md).*

