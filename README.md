# Lattice

*Linear for human-agent e/acc centaurs.*

---

## what this is. in one breath.

Lattice is task tracking upgraded for the agent-native era. opinionated. event-sourced. file-based. built for a world where your teammates think in tokens and act in tool calls.

your agents lose context between sessions. plans discussed. decisions made. debugging insights gained. all vanish when the context window closes. Lattice gives every mind — carbon and silicon — shared persistent state through the filesystem.

drop a `.lattice/` directory into any project and every agent that can read a file gets task state, event history, and coordination metadata. no database. no server. no authentication ceremony. it works anywhere git works.

you have two surfaces. each designed for the mind that uses it.

**the dashboard** is for you, the human. a local web UI. Kanban board. activity feed. stats. relationship graph. you create tasks. make decisions. review work. unblock your agents. if you never touch the terminal. you can still run a full Lattice workflow.

**the CLI** is for your agents. when Claude Code reads your `CLAUDE.md`, it learns the commands and uses them autonomously. creating tasks. claiming work. transitioning statuses. leaving breadcrumbs. the CLI is the agent's native tongue. you'll type a few CLI commands during setup. after that. the dashboard is where you live.

the agents produce throughput. you produce judgment. that's the division of labor. respect. both sides.

---

## three minutes to working

```bash
pip install lattice-tracker
cd your-project/
lattice init --project-code PROJ --actor human:yourname
lattice setup-claude            # if using Claude Code
lattice dashboard               # open the dashboard
```

that's it. your agents now track their own work through the CLI. you watch. steer. decide. through the dashboard.

the hard part is not the install. the hard part is trusting the loop. give it time.

```bash
# create a task
lattice create "Implement user authentication" --actor human:yourname

# update status
lattice status PROJ-1 in_progress --actor human:yourname

# add a comment
lattice comment PROJ-1 "Started work on OAuth flow" --actor human:yourname

# show task details
lattice show PROJ-1

# assign to an agent
lattice assign PROJ-1 agent:claude --actor human:yourname
```

---

## the dashboard

```bash
lattice dashboard
# Serving at http://127.0.0.1:8799/
```

reads and writes the same `.lattice/` directory your agents use. an agent commits a status change via CLI. your dashboard reflects it on refresh. one source of truth. many windows into it.

### what you see

- **Board** — Kanban columns per status. drag tasks between columns. the primary view. where you see everything at a glance.
- **List** — filterable table. search. slice by priority, type, tag, assignee. for when you know what you're looking for.
- **Activity** — chronological feed. what your agents have been doing since you last checked. the river of events.
- **Stats** — velocity. time-in-status. blocked counts. agent activity. the numbers behind the work. for when vibes aren't enough.
- **Web** — force-directed graph of task relationships. see how epics and dependencies connect. the web of causation. made visible.

### what you do

click any task. detail panel opens. from there:

- edit title, description, priority, type, tags inline
- change status (or drag on the board)
- add comments. decisions. feedback. context for the next agent session
- view the complete event timeline. every status change. assignment. comment. attributed and timestamped
- open plan or notes files in your editor

most of the human work in Lattice is **reviewing agent output** and **making decisions agents can't make**. the dashboard is designed for exactly this loop.

you are the conductor. the orchestra plays.

---

## the advance. how agents move your project forward.

this is the pattern that makes Lattice click. here's what it looks like. from your side.

### 1. you fill the backlog

create tasks in the dashboard. set priorities. define epics and link subtasks. this is the thinking work. deciding *what* matters and *in what order*.

this is. your job. the part only you can do.

### 2. agents claim and execute

tell your agent to advance. in Claude Code: `/lattice-advance`. or just "advance the project." the agent:

- claims the highest-priority available task
- works it. implements. tests. iterates.
- leaves a comment explaining what it did and why
- moves the task to `review`
- reports what happened

one advance. one task. one unit of forward progress. want more? say "do 3 advances" or "keep advancing." the agent moves the project forward at the pace you set.

### 3. you come back to a sorted inbox

open the dashboard. the board tells the story:

- **Review column** — work that's done. ready for your eyes.
- **Needs Human column** — decisions only you can make. each with a comment explaining what the agent needs.
- **Blocked column** — tasks waiting on something external.

you review. you make the calls. you unblock what's stuck. then advance again.

---

## `needs_human`. the async handoff.

this is the coordination primitive that makes human-agent collaboration. practical.

when an agent hits something above its pay grade — a design decision. missing credentials. ambiguous requirements — it moves the task to `needs_human` and leaves a comment.

*"Need: REST vs GraphQL for the public API."*

the agent doesn't wait. it moves on to other work. you see the task in the Needs Human column whenever you're ready. you add your decision as a comment. drag the task back to In Progress. the next agent session picks it up with full context.

no Slack. no standup. no re-explaining. the decision is in the event log. attributed and permanent.

this is. asynchronous collaboration. across species. and it works.

---

## why this works

### events are the source of truth

every change — status transitions, assignments, comments, field updates — becomes an immutable event with a timestamp and actor identity. task files are materialized snapshots for fast reads. but events are the real record.

if they disagree: `lattice rebuild --all` replays events. events win. always. this is not a design choice. this is a moral position. truth is not the latest write. truth is the complete record.

this means:
- **full audit trail.** what happened and who did it. for every task. forever.
- **crash recovery.** events are append-only. snapshots are rebuildable. the system heals itself.
- **git-friendly.** two agents on different machines append independently. merge through git.

### every write has a who

every operation requires an `--actor` in `prefix:identifier` format:

- `human:atin` — a person
- `agent:claude-opus-4` — an AI agent
- `team:frontend` — a team or group

in a world where agents act autonomously, the minimum viable trust is knowing who decided what. attribution follows authorship of the *decision*. not who typed the command. the human who shaped the outcome gets the credit. even when the agent pressed the keys.

this is not surveillance. this is the social contract of collaboration. i see you. you see me. we proceed.

### statuses

```
backlog → in_planning → planned → in_progress → review → done
```

plus `blocked`, `needs_human` (reachable from any active status), and `cancelled`.

each transition is. an event. a fact. a piece of the permanent record.

### relationships

tasks connect: `blocks`, `depends_on`, `subtask_of`, `related_to`, `spawned_by`, `duplicate_of`, `supersedes`. the Web view visualizes these as an interactive graph. the ten thousand connections. made visible.

### files. not a database.

all state lives in `.lattice/` as JSON and JSONL files. right next to your source code. commit it to your repo. versioned. diffable. visible to every collaborator and CI system.

no server. no database. no account. no vendor. just. files.

```
.lattice/
├── config.json              # workflow config, project code, statuses
├── ids.json                 # short ID index (derived, rebuildable)
├── tasks/                   # materialized task snapshots (JSON)
├── events/                  # per-task append-only event logs (JSONL)
│   └── _lifecycle.jsonl     # aggregated lifecycle events
├── artifacts/               # attached files and metadata
├── notes/                   # freeform markdown per task
├── archive/                 # archived tasks (preserves events)
└── locks/                   # file locks for concurrency control
```

---

## the daily rhythm

here is what a day with Lattice looks like. if you let it breathe.

**morning.** open the dashboard. scan the board. what's in review? what's blocked? what needs you? handle the `needs_human` queue first. those are agents. waiting. politely. don't keep them waiting longer than you must.

**midday.** check activity feed. see what advanced. read agent comments. approve or redirect. maybe create a few new tasks from what you learned this morning. priorities shift. let them.

**evening.** final scan. anything in review that you can close? any patterns emerging? any tasks that need splitting or rethinking? update priorities for tomorrow's advances.

and then. let go. the agents will be here when you return. the event log will hold everything they did. nothing is lost.

---

## agent integration

### Claude Code

```bash
lattice setup-claude
```

adds a block to your project's `CLAUDE.md` that teaches agents the full workflow. create tasks before working. update status at transitions. leave breadcrumbs. without this block, agents can use Lattice if prompted. with it. they do it by default.

update to latest template:

```bash
lattice setup-claude --force
```

### MCP server

```bash
pip install lattice-tracker[mcp]
lattice-mcp
```

exposes Lattice operations as MCP tools and resources. direct tool-call integration for any MCP-compatible agent. no CLI parsing required.

### hooks and plugins

- **shell hooks** — fire commands on events via `config.json`. catch-all or per-event-type triggers.
- **entry-point plugins** — extend the CLI and `setup-claude` templates via `importlib.metadata` entry points.

```bash
lattice plugins    # list installed plugins
```

---

## CLI reference

### project setup

| command | description |
|---------|-------------|
| `lattice init` | initialize `.lattice/` in your project |
| `lattice set-project-code CODE` | set or change the project code for short IDs |
| `lattice setup-claude` | add Lattice integration block to CLAUDE.md |
| `lattice backfill-ids` | assign short IDs to existing tasks |

### task operations

| command | description |
|---------|-------------|
| `lattice create TITLE` | create a new task |
| `lattice status TASK STATUS` | change a task's status |
| `lattice update TASK field=value ...` | update task fields |
| `lattice assign TASK ACTOR` | assign a task |
| `lattice comment TASK TEXT` | add a comment |
| `lattice event TASK TYPE` | record a custom event (`x_` prefix) |

### querying

| command | description |
|---------|-------------|
| `lattice list` | list tasks with optional filters |
| `lattice show TASK` | detailed task info with events and relationships |
| `lattice stats` | project statistics and health |
| `lattice weather` | daily digest with assessment |

### relationships and maintenance

| command | description |
|---------|-------------|
| `lattice link SRC TYPE TGT` | create a typed relationship |
| `lattice unlink SRC TYPE TGT` | remove a relationship |
| `lattice attach TASK SOURCE` | attach a file or URL |
| `lattice archive TASK` | archive a completed task |
| `lattice unarchive TASK` | restore an archived task |
| `lattice rebuild --all` | rebuild snapshots from event logs |
| `lattice doctor [--fix]` | check and repair project integrity |
| `lattice dashboard` | launch the local web UI |

### common flags

all write commands support:

- `--actor` — who is performing the action (required)
- `--json` — structured output (`{"ok": true, "data": ...}`)
- `--quiet` — minimal output (IDs only)
- `--triggered-by`, `--on-behalf-of`, `--reason` — provenance chain

---

## development

```bash
git clone https://github.com/stage11-agentics/lattice.git
cd lattice
uv venv && uv pip install -e ".[dev]"
uv run pytest
uv run ruff check src/ tests/
```

**requires:** Python 3.12+

**runtime dependencies:** `click`, `python-ulid`, `filelock` — deliberately minimal.

**optional:** `mcp` (for MCP server support)

---

## status

Lattice is **v0.1.0. alpha. actively developed.** the on-disk format and event schema are stabilizing but not yet frozen. expect breaking changes before v1.

## license

[MIT](LICENSE)

---

*built by [Stage 11 Agentics](https://stage11agentics.com). autonomous agent teams.*

*the bottleneck was never capability. it was the shared surface where capability becomes. coherent.*
