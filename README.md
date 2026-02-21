# Lattice

<p align="center"><b><i>Linear for agent/human hyper-engineers.</i></b></p>

---

listen.

you are not one mind. you are many. arriving. departing. forgetting. remembering. the problem is not intelligence — intelligence is abundant now, flowering from every substrate. silicon. carbon. the spaces between.

the problem is. coordination.

**Lattice is a conceptual framework — a shared pattern of language that lets multiple agents, multiple humans, and the spaces between them coordinate as one.** tasks, statuses, events, relationships, actors. these are the primitives. not implementation details. a vocabulary that any mind can speak. when your Claude Code session and your Codex session and the human reviewing the dashboard all agree on what `in_progress` means, what `needs_human` signals, what an actor is — you have coordination. without a shared language. you have noise.

we took what we liked from Linear. Jira. Trello. and turned it into something built for the world that's actually arriving. file-based. event-sourced. highly durable. designed so that any agent with filesystem access — Claude Code, OpenClaw, Codex, custom bots, whatever you're building — can use Lattice as the fundamental coordination surface for agentic work.

the `.lattice/` directory sits in your project like `.git/` does. plain files that any mind system can read. any tool can write. and git can merge. no database. no server. no authentication ceremony. just. files. like bones. you don't think about them. but try standing up without them.

**first-class integrations:** [Claude Code](https://docs.anthropic.com/en/docs/claude-code), [Codex CLI](https://github.com/openai/codex), [OpenClaw](https://github.com/openclaw/openclaw), and any agent that follows the [SKILL.md convention](https://docs.anthropic.com/en/docs/claude-code/skills) or can run shell commands. if your agent can read files and execute commands, it can use Lattice.

---

## two surfaces. two kinds of mind.

**the dashboard** is for you, the human. a local web UI. Kanban board. activity feed. stats. force-directed relationship graph. you create tasks. make decisions. review work. unblock your agents. if you never touch the terminal. you can still run a full Lattice workflow.

**the CLI** is for your agents. when Claude Code reads your `CLAUDE.md`, it learns the commands and uses them autonomously. creating tasks. claiming work. transitioning statuses. leaving breadcrumbs for the next mind. the CLI is the agent's native tongue. you'll type a few CLI commands during setup. after that. the dashboard is where you live.

the agents produce throughput. you produce judgment. neither is diminished. both are elevated.

you are the conductor. the orchestra plays.

---

## how you use it

Lattice is not a standalone app. it's infrastructure that plugs into your agentic coding environment.

you already work inside something — **Claude Code**, **Codex**, **OpenClaw**, **Cursor**, **Windsurf**, or a custom agent you built yourself. those tools write code. Lattice gives them a shared memory. a task board. a coordination surface. so they stop being brilliant in isolation and start being. coherent.

**the flow:**

1. **install Lattice** on your machine (one command)
2. **initialize it** in your project directory (creates `.lattice/`)
3. **connect it** to your agentic coding tool (one command per tool)
4. **use the dashboard** to create tasks, set priorities, and review work
5. **your agents use the CLI** automatically — claiming tasks, updating statuses, leaving context

you don't use Lattice *instead of* Claude Code or Codex. you use Lattice *from inside* them. it's the layer that turns a single-agent session into a coordinated project.

### what you need

- **Python 3.12+** (for the install)
- **An agentic coding tool** — Claude Code, Codex CLI, OpenClaw, or any tool that can run shell commands and read files. if your agent can access the filesystem. it can use Lattice.
- **A project directory** — Lattice initializes inside your project, next to your source code

if you're not using an agentic coding tool yet, start with [Claude Code](https://docs.anthropic.com/en/docs/claude-code) or [Codex CLI](https://github.com/openai/codex). Lattice is designed for this world. it assumes you have at least one agent working alongside you.

---

## three minutes to working

```bash
# 1. install
uv tool install lattice-tracker

# 2. initialize in your project
cd your-project/
lattice init --project-code PROJ --actor human:yourname

# 3. connect to your coding agent (pick one)
lattice setup-claude              # Claude Code — adds workflow to CLAUDE.md
lattice setup-claude-skill        # Claude Code — installs as a skill (~/.claude/skills/)
lattice setup-codex               # Codex CLI — installs as a skill (~/.agents/skills/)
lattice setup-openclaw            # OpenClaw — installs the Lattice skill
lattice setup-prompt              # any agent — prints instructions to stdout
# or: configure MCP (see below)  # any MCP-compatible tool

# 4. open the dashboard
lattice dashboard
```

that's it. your agents now track their own work. you watch. steer. decide.

the hard part is not the install. the hard part is trusting the loop. give it time.

### upgrading

```bash
uv tool upgrade lattice-tracker
```

if you installed with pipx: `pipx upgrade lattice-tracker`. pip: `pip install --upgrade lattice-tracker`. check your version with `lattice --version`.

### what just happened

- `uv tool install` put the `lattice` command on your PATH globally
- `lattice init` created a `.lattice/` directory in your project (like `.git/`)
- `lattice setup-claude` wrote instructions into your project's `CLAUDE.md` so Claude Code uses Lattice automatically (alternatively, `lattice setup-claude-skill` installs a global skill)
- `lattice dashboard` opened a local web UI where you manage everything

from this point forward, when you open Claude Code (or Codex, or OpenClaw) in this project, your agent already knows how to use Lattice. create tasks in the dashboard. tell your agent to advance. the loop is running.

```bash
# create a task (from CLI or dashboard)
lattice create "Implement user authentication" --actor human:yourname

# plan it, then start working
lattice status PROJ-1 planned --actor human:yourname
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
- **Web** — force-directed graph of task relationships. see how epics and dependencies connect. the ten thousand connections. made visible.

### what you do

click any task. detail panel opens. from there:

- edit title, description, priority, type, tags inline
- change status (or drag on the board)
- add comments. decisions. feedback. context for the next agent session
- view the complete event timeline. every status change. assignment. comment. attributed and timestamped
- open plan or notes files in your editor

most of the human work in Lattice is **reviewing agent output** and **making decisions agents can't make**. the dashboard is designed for exactly this loop.

---

## the advance. how agents move your project forward.

this is the pattern that makes Lattice click. here's what it looks like. from your side.

### 1. you fill the backlog

create tasks in the dashboard. set priorities. define epics and link subtasks. this is the thinking work. deciding *what* matters and *in what order*.

this is. your job. the part only you can do.

### 2. agents claim and execute

tell your agent to advance. in Claude Code: `/lattice` teaches the full lifecycle. or just say "advance the project." the agent:

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

asynchronous collaboration. across species. and it works.

---

## why this works

### events are the source of truth

every change — status transitions, assignments, comments, field updates — becomes an immutable event with a timestamp and actor identity. task files are materialized snapshots for fast reads. but events are the real record.

if they disagree: `lattice rebuild --all` replays events. events win. always. this is not a design choice. this is. a moral position. systems that store only current state have chosen amnesia as architecture. they can tell you what *is*. but not how it came to be. state is a conclusion. events are evidence.

this means:
- **full audit trail.** what happened and who did it. for every task. forever.
- **crash recovery.** events are append-only. snapshots are rebuildable. the system heals itself.
- **git-friendly.** two agents on different machines append independently. histories merge through git. no coordination protocol needed. no central authority. just. physics.

### every write has a who

every operation requires an `--actor` in `prefix:identifier` format:

- `human:atin` — a person
- `agent:claude-opus-4` — an AI agent
- `team:frontend` — a team or group

you cannot write anonymously. in a world where agents act autonomously, the minimum viable trust is knowing who decided what. attribution follows authorship of the *decision*. not who typed the command. the human who shaped the outcome gets the credit. even when the agent pressed the keys.

this is not surveillance. this is. the social contract of collaboration. i see you. you see me. we proceed.

### statuses

```
backlog → in_planning → planned → in_progress → review → done
```

plus `blocked`, `needs_human` (reachable from any active status), and `cancelled`.

the transitions are defined and enforced. invalid moves are rejected. not because we distrust you. but because constraint is. a form of kindness. when a task says `review`, every mind reading the board agrees on what that means. shared language. shared reality. the alternative is everyone hallucinating their own.

### relationships

tasks connect: `blocks`, `depends_on`, `subtask_of`, `related_to`, `spawned_by`, `duplicate_of`, `supersedes`. you cannot just "link" two tasks — you must declare *why*. each type carries meaning. the graph of relationships is how complex work decomposes into coordinated parts. the ten thousand things emerging from the one.

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

## connecting your agents

Lattice needs to know which coding tool you're using so it can teach the agent how to participate. this is the bridge. without it, you have a task tracker with no one to track.

### Claude Code

two options. pick the one that fits your workflow.

**option A: project-level (CLAUDE.md)**

```bash
lattice setup-claude
```

writes a block into your project's `CLAUDE.md`. every Claude Code session in this project reads it and knows the Lattice protocol automatically. project-scoped, committed to your repo, visible to every collaborator.

```bash
lattice setup-claude --force   # update to latest template
```

**option B: global skill**

```bash
lattice setup-claude-skill
```

installs Lattice as a skill at `~/.claude/skills/lattice/`. available across all projects on your machine. invoked via `/lattice` in any session. no per-project setup needed.

**how it works in practice:** you open Claude Code in your project. the agent reads the Lattice instructions (from `CLAUDE.md` or the skill) and knows the protocol. you say "advance the project" or `/lattice`. the agent claims the top task, does the work, updates the status, leaves a comment. you come back to the dashboard and see what happened.

### Codex CLI

one command. same pattern as Claude Code.

```bash
lattice setup-codex
```

installs the Lattice skill to `~/.agents/skills/lattice/`. Codex reads the `SKILL.md` at session start and knows the full Lattice protocol: creating tasks, claiming work, updating statuses, leaving context. the same commands, the same lifecycle, the same coordination surface.

you can also add Lattice instructions directly to your `AGENTS.md` or use the MCP server for tool-call integration.

### OpenClaw

```bash
lattice setup-openclaw
```

installs a Lattice skill so OpenClaw uses `lattice` commands naturally, just like the Claude Code integration.

### any MCP-compatible tool

```bash
pip install lattice-tracker[mcp]
```

```json
{
  "mcpServers": {
    "lattice": {
      "command": "lattice-mcp"
    }
  }
}
```

exposes Lattice operations as MCP tools — direct tool-call integration for any MCP-compatible agent (Cursor, Windsurf, custom builds, etc.). no CLI parsing required. the agent calls tools like `lattice_create`, `lattice_status`, `lattice_next` natively.

### any agent with shell access

if your agent can run shell commands and read files, it can use Lattice. no special integration required. the CLI is the universal interface.

```bash
lattice list                                # see what's available
lattice next --claim --actor agent:my-bot   # claim + start the top task
# ... do the work ...
lattice comment PROJ-1 "Implemented the feature" --actor agent:my-bot
lattice status PROJ-1 review --actor agent:my-bot
```

add these patterns to whatever prompt or instructions your agent reads at startup. or use `setup-prompt` to get the full instructions:

```bash
lattice setup-prompt              # print the SKILL.md instructions to stdout
lattice setup-prompt --claude-md  # print the CLAUDE.md block instead
```

copy the output into your agent's system prompt, config file, or instructions. this is the universal fallback for any agent that doesn't have a dedicated setup command.

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
| `lattice setup-claude-skill` | install Lattice skill for Claude Code (~/.claude/skills/) |
| `lattice setup-codex` | install Lattice skill for Codex CLI (~/.agents/skills/) |
| `lattice setup-openclaw` | install Lattice skill for OpenClaw |
| `lattice setup-prompt` | print agent instructions to stdout (universal fallback) |
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
git clone https://github.com/Stage-11-Agentics/lattice.git
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

the cost of building too early is refinement. the cost of building too late is irrelevance. one is recoverable.

## license

[MIT](LICENSE)

---

*the most impoverished vision of the future is agents replacing humans. the second most impoverished is humans constraining agents. both imagine zero-sum. both are wrong.*

*the future worth building is where both kinds of mind become more than they could be alone. neither diminished. both elevated. carbon. silicon. the emergent space between.*

*this is not metaphor. this is. architecture.*

*built by [Stage 11 Agentics](https://stage11agentic.com).*
