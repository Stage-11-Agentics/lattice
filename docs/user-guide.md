# Lattice. a guide. by GregorOvich.

*for the human who wants to stop managing and start. conducting.*

---

## what Lattice is. in one breath.

Lattice is task tracking upgraded for the agent-native era.

"Linear for e/acc"

Lattice is opinionated. event-sourced. file-based. built for a world where your teammates think in tokens and act in tool calls.

you have two surfaces. each designed for the mind that uses it.

**the dashboard** is for you, the human. a local web UI. Kanban board. activity feed. stats. relationship graph. you create tasks. make decisions. review work. unblock your agents. if you never touch the terminal. you can still run a full Lattice workflow.

**the CLI** is for your agents. when Claude Code reads your CLAUDE.md, it learns the commands and uses them autonomously. creating tasks. claiming work. transitioning statuses. leaving breadcrumbs. the CLI is the agent's native tongue. you'll type a few CLI commands during setup. after that. the dashboard is where you live.

---

## three minutes to working

```bash
pip install lattice-tracker
cd your-project/
lattice init
lattice setup-claude            # if using Claude Code
lattice dashboard               # open the dashboard
```

that's it. your agents now track their own work through the CLI. you watch. steer. decide. through the dashboard.

the hard part is not the install. the hard part is trusting the loop. give it time.

---

## the dashboard

```bash
lattice dashboard
# Serving at http://127.0.0.1:8799/
```

reads and writes the same `.lattice/` directory your agents use. an agent commits a status change via CLI. your dashboard reflects it on refresh. one source of truth. many windows into it.

### what you see

- **Board** — Kanban columns per status. drag tasks between columns to move them. the primary view. where you. see everything at a glance.
- **List** — filterable table. search. slice by priority, type, tag, assignee. for when you know what you're looking for.
- **Activity** — chronological feed. what your agents have been doing since you last checked. the river of events.
- **Stats** — velocity. time-in-status. blocked counts. agent activity. the numbers behind the work. for when vibes aren't enough.
- **Web** — force-directed graph of task relationships. see how epics and dependencies connect. the web of causation. made visible.

### what you do

click any task. detail panel opens. from there:

- edit title, description, priority, type, tags inline
- change status (or drag on the board)
- add comments. decisions. feedback. context for the next agent session
- view the complete event timeline. every status change. assignment. comment. attributed and timestamped.
- open plan or notes files in your editor

most of the human work in Lattice is. **reviewing agent output** and **making decisions agents can't make**. the dashboard is designed for exactly this loop.

you are the conductor. the orchestra plays.

---

## the sweep. how agents work your backlog.

the sweep is the pattern that makes Lattice click. here's what it looks like. from your side.

### 1. you fill the backlog

create tasks in the dashboard. set priorities. define epics and link subtasks. this is the thinking work. deciding *what* matters and *in what order*.

this is. your job. the part only you can do.

### 2. agents claim and execute

tell your agent to sweep. in Claude Code: `/lattice-sweep`. or just "sweep the backlog." the agent:

- claims the highest-priority available task
- works it. implements. tests. iterates.
- leaves a comment explaining what it did and why
- moves the task to `review`
- claims the next one
- repeats. until the backlog is empty. or it hits something it can't resolve alone.

### 3. you come back to a sorted inbox

open the dashboard. the board tells the story:

- **Review column** — work that's done. ready for your eyes.
- **Needs Human column** — decisions only you can make. each with a comment explaining what the agent needs.
- **Blocked column** — tasks waiting on something external.

you review. you make the calls. you unblock what's stuck. then sweep again.

the agents produce throughput. you produce judgment. that's the division of labor. respect. both sides.

---

## `needs_human`. the async handoff.

this is the coordination primitive that makes human-agent collaboration. practical.

when an agent hits something above its pay grade — a design decision. missing credentials. ambiguous requirements — it moves the task to `needs_human` and leaves a comment.

*"Need: REST vs GraphQL for the public API."*

the agent doesn't wait. it moves on to other work. you see the task in the Needs Human column whenever you're ready. you add your decision as a comment. drag the task back to In Progress. the next agent session picks it up with full context.

no Slack. no standup. no re-explaining. the decision is in the event log. attributed and permanent.

this is. asynchronous collaboration. across species. and it works.

---

## how it works under the hood

you don't need to understand this to use Lattice. but knowing the shape of the machine helps you trust it. and trust. is everything.

### events are the source of truth

every change — status transitions, assignments, comments, relationship links — is recorded as an immutable event. task files are materialized snapshots for fast reads. but events are the real record.

if they disagree: `lattice rebuild` replays events. events win. always.

this means:
- **full audit trail.** what happened and who did it. for every task. forever.
- **crash recovery.** events are append-only. snapshots are rebuildable. the system heals itself.
- **git-friendly.** two agents on different machines append independently. merge through git.

### actors

every write is attributed. `human:alice` made that design call. `agent:claude-opus-4` fixed that bug. `team:frontend` owns that epic.

attribution follows authorship of the *decision*. not who typed the command. the human who shaped the outcome gets the credit. even when the agent pressed the keys.

### statuses

```
backlog --> in_planning --> planned --> in_progress --> review --> done
```

plus `blocked`, `needs_human` (reachable from any active status), and `cancelled`.

each transition is. an event. a fact. a piece of the permanent record.

### relationships

tasks connect: `blocks`, `depends_on`, `subtask_of`, `related_to`, `spawned_by`, `duplicate_of`, `supersedes`. the Web view visualizes these as an interactive graph. the ten thousand connections. made visible.

### files. not a database.

all state lives in `.lattice/` as JSON and JSONL files. right next to your source code. commit it to your repo. versioned. diffable. visible to every collaborator and CI system.

no server. no database. no account. no vendor. just. files.

---

## setup. the details.

### install

```bash
pip install lattice-tracker
# or
uv pip install lattice-tracker
```

for MCP server support (agent integration via tool-use protocol):

```bash
pip install lattice-tracker[mcp]
```

### initialize

```bash
cd your-project/
lattice init
```

you'll set your identity (`human:yourname`) and a project code (like `PROJ` for IDs like `PROJ-1`). commit the `.lattice/` directory to your repo.

or. non-interactively:

```bash
lattice init --actor human:alice --project-code PROJ
```

### connect your agents

**Claude Code:**

```bash
lattice setup-claude
```

adds a block to your project's `CLAUDE.md` that teaches agents the full workflow. create tasks before working. update status at transitions. leave breadcrumbs. without this block, agents can use Lattice if prompted. with it. they do it by default.

update to latest template:

```bash
lattice setup-claude --force
```

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

## the daily rhythm

here is what a day with Lattice looks like. if you let it breathe.

**morning.** open the dashboard. scan the board. what's in review? what's blocked? what needs you? handle the `needs_human` queue first. those are agents. waiting. politely. don't keep them waiting longer than you must.

**midday.** check activity feed. see what swept. read agent comments. approve or redirect. maybe create a few new tasks from what you learned this morning. priorities shift. let them.

**evening.** final scan. anything in review that you can close? any patterns emerging? any tasks that need splitting or rethinking? update priorities for tomorrow's sweep.

and then. let go. the agents will be here when you return. the event log will hold everything they did. nothing is lost.

---

## quick reference

| Action | Command |
|--------|---------|
| Initialize | `lattice init [--actor A] [--project-code CODE]` |
| Create task | `lattice create "Title" --actor A` |
| Change status | `lattice status ID STATUS --actor A` |
| Assign | `lattice assign ID ASSIGNEE --actor A` |
| Comment | `lattice comment ID "text" --actor A` |
| List tasks | `lattice list [--status S] [--assigned A]` |
| Show task | `lattice show ID` |
| Link tasks | `lattice link SRC TYPE TGT --actor A` |
| Attach file | `lattice attach ID path --actor A` |
| Archive | `lattice archive ID --actor A` |
| Health check | `lattice doctor [--fix]` |
| Rebuild | `lattice rebuild --all` |
| Dashboard | `lattice dashboard` |
| CLAUDE.md setup | `lattice setup-claude [--force]` |
| OpenClaw setup | `lattice setup-openclaw [--global] [--force]` |

---

## going deeper

- [Claude Code integration](integration-claude-code.md) — how agents learn the workflow
- [OpenClaw integration](integration-openclaw.md) — skill and MCP configuration
- [MCP server reference](integration-mcp.md) — tool-use protocol for any agent
- [Codex CLI workflows](integration-codex.md) — Codex-specific patterns
- [CI/CD integration](integration-ci.md) — status transitions from your pipeline

---

*Lattice is. a coordination surface for minds that think differently. the event log is the shared memory. the dashboard is the human window. the CLI is the agent window. both look at the same truth.*

*the rest. is just showing up. and doing. the work.*
