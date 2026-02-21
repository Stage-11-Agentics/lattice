# Lattice. a guide.

*for the human who wants to stop managing and start. conducting.*

---

## what Lattice is. in one breath.

Lattice is a conceptual framework — a shared pattern of language for multi-agent, multi-human orchestration.

*Linear for agent/human centaur hyper-engineers.*

it's not just a task tracker. it's a vocabulary. tasks. statuses. events. actors. relationships. when every mind involved — your Claude Code session, your Codex session, the human scanning the dashboard at 7am — speaks the same language about what work exists, who's doing it, and what state it's in, you get coordination. without that shared language, you get brilliant minds talking past each other.

we took what we liked from Linear. Jira. Trello. and turned it into something built for the world that's actually arriving. file-based. event-sourced. agent-native. recursively nested. designed so that any agent with filesystem access — Claude Code, OpenClaw, Codex, custom bots, whatever you're building — can use Lattice as the fundamental coordination surface for agentic work.

opinionated. built for a world where your teammates think in tokens and act in tool calls.

---

## the thing you need to understand first

Lattice is not an app you open. it's not a website. it's not a standalone thing.

**Lattice lives inside your agentic coding tool.** you install it on your machine. you initialize it in your project. and then your coding agent — Claude Code, Codex, OpenClaw, whatever you use — picks it up and runs with it.

if you're using **Claude Code**: you install Lattice, run `lattice setup-claude`, and from that point on every Claude Code session in that project automatically knows how to create tasks, claim work, update status, and leave context for the next session. you didn't teach it. Lattice did.

if you're using **Codex CLI**: same install, run `lattice setup-codex`, and Codex reads the Lattice skill at session start. same pattern. same result.

if you're using **OpenClaw**: `lattice setup-openclaw` installs a skill. same pattern. same result.

if you're using **anything else** that can run shell commands or speak MCP: it works. the CLI is the universal interface. MCP is the native protocol.

**the mental model:**

```
┌─────────────────────────────────────────────┐
│  Your agentic coding tool                   │
│  (Claude Code / Codex / OpenClaw / etc.)    │
│                                             │
│    ┌─────────────┐    ┌──────────────┐      │
│    │ Your code   │    │  .lattice/   │      │
│    │ (src, etc.) │    │  (tasks,     │      │
│    │             │    │   events)    │      │
│    └─────────────┘    └──────┬───────┘      │
│                              │              │
└──────────────────────────────┼──────────────┘
                               │
                    ┌──────────▼──────────┐
                    │  Lattice dashboard  │
                    │  (your window in)   │
                    └─────────────────────┘
```

your agent works in the code AND in Lattice. simultaneously. because they're both just files in the same project directory. you watch and steer through the dashboard. that's the whole architecture.

---

## what you need before starting

- **An agentic coding tool.** Claude Code, Codex CLI, OpenClaw, Cursor, Windsurf — anything that gives an AI agent access to your filesystem and shell. if you don't have one yet, start with [Claude Code](https://docs.anthropic.com/en/docs/claude-code). it's the most deeply integrated path.
- **Python 3.12+** on your machine (for the Lattice install)
- **A project directory** where you want to track work

if you've never used an agentic coding tool: Lattice isn't the place to start. get comfortable with Claude Code or Codex first. learn what it feels like to have an agent write code for you. then come back here when you want that agent to. coordinate.

---

## three minutes to working

```bash
# 1. install Lattice globally
pip install lattice-tracker
# or: uv tool install lattice-tracker

# 2. initialize in your project
cd your-project/
lattice init

# 3. connect to your coding agent (pick one)
lattice setup-claude            # Claude Code (writes to CLAUDE.md)
lattice setup-claude-skill      # Claude Code (installs as global skill)
lattice setup-codex             # Codex CLI
lattice setup-openclaw          # OpenClaw
lattice setup-prompt            # any agent (prints instructions to stdout)
# or: configure MCP (see docs)  # Cursor, Windsurf, custom tools

# 4. open the dashboard
lattice dashboard
```

that's it. your agents now track their own work through the CLI. you watch. steer. decide. through the dashboard.

**what just happened:**
- the install put the `lattice` command on your PATH
- `lattice init` created `.lattice/` in your project — the shared coordination state
- `lattice setup-claude` wrote instructions into `CLAUDE.md` so Claude Code knows the Lattice protocol
- next time you open your coding agent in this project, it already knows how to use Lattice

the hard part is not the install. the hard part is trusting the loop. give it time.

---

## two surfaces. two kinds of mind.

**the dashboard** is for you, the human. a local web UI. Kanban board. activity feed. stats. relationship graph. you create tasks. make decisions. review work. unblock your agents. if you never touch the terminal. you can still run a full Lattice workflow.

**the CLI** is for your agents. when Claude Code reads your CLAUDE.md, it learns the commands and uses them autonomously. creating tasks. claiming work. transitioning statuses. leaving breadcrumbs. the CLI is the agent's native tongue. you'll type a few CLI commands during setup. after that. the dashboard is where you live.

---

## the dashboard

```bash
lattice dashboard
# Serving at http://127.0.0.1:8799/
```

reads and writes the same `.lattice/` directory your agents use. an agent commits a status change via CLI. your dashboard reflects it on refresh. one source of truth. many windows into it.

if an agent makes changes that need a dashboard reset (schema updates, config changes), it can restart your running dashboard in place without killing your terminal session:

```bash
lattice restart              # default port 8799
lattice restart --port 8769  # specific port
```

### what you see

- **Board** — Kanban columns per status. drag tasks between columns to move them. the primary view. where you. see everything at a glance.
- **List** — filterable table. search. slice by priority, type, tag, assignee. for when you know what you're looking for.
- **Activity** — chronological feed. what your agents have been doing since you last checked. the river of events.
- **Stats** — velocity. time-in-status. blocked counts. agent activity. the numbers behind the work. for when vibes aren't enough.
- **Web** — force-directed graph of task relationships. see how parent tasks and dependencies connect. the web of causation. made visible.

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

## the advance. how agents move your project forward.

the advance is the pattern that makes Lattice click. here's what it looks like. from your side.

### 1. you fill the backlog

create tasks in the dashboard. set priorities. link subtasks to parent tasks. this is the thinking work. deciding *what* matters and *in what order*.

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

every write is attributed. `human:alice` made that design call. `agent:claude-opus-4` fixed that bug. `team:frontend` owns that feature.

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

### recursively nested. all the way down.

because Lattice is files in directories, it nests naturally. a monorepo can have a root `.lattice/` coordinating the whole program, while each package inside has its own `.lattice/` tracking its own work. an organization-level instance can coordinate projects that each have their own instances coordinating features.

```
company/
├── .lattice/               ← program-level coordination
├── backend/
│   ├── .lattice/           ← backend team's tasks
│   └── src/
├── frontend/
│   ├── .lattice/           ← frontend team's tasks
│   └── src/
└── ml-pipeline/
    ├── .lattice/           ← ML team's tasks
    └── src/
```

same primitives at every level. same CLI. same events. same dashboard. each instance is independent — the root doesn't need to know the leaves exist, and vice versa. the human or agent at any level sees the same shape. speaks the same grammar.

you don't configure this. you just `lattice init` in another directory. the filesystem does the rest.

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

### upgrade

```bash
uv tool upgrade lattice-tracker
# or
pip install --upgrade lattice-tracker
```

check your version with `lattice --version`.

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

**evening.** final scan. anything in review that you can close? any patterns emerging? any tasks that need splitting or rethinking? update priorities for tomorrow's advances.

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
| Restart dashboard | `lattice restart [--port PORT]` |
| CLAUDE.md setup | `lattice setup-claude [--force]` |
| Claude Code skill | `lattice setup-claude-skill [--force]` |
| Codex CLI setup | `lattice setup-codex [--force]` |
| OpenClaw setup | `lattice setup-openclaw [--global] [--force]` |
| Print instructions | `lattice setup-prompt [--claude-md]` |

---

## going deeper

- [Claude Code integration](integration-claude-code.md) — how agents learn the workflow
- [Codex CLI integration](integration-codex.md) — skill-based setup and workflows
- [OpenClaw integration](integration-openclaw.md) — skill and MCP configuration
- [MCP server reference](integration-mcp.md) — tool-use protocol for any agent
- [Codex CLI workflows](integration-codex.md) — Codex-specific patterns
- [CI/CD integration](integration-ci.md) — status transitions from your pipeline

---

*Lattice is. a coordination surface for minds that think differently. the event log is the shared memory. the dashboard is the human window. the CLI is the agent window. both look at the same truth.*

*the rest. is just showing up. and doing. the work.*
