# Lattice

<p align="center"><b><i>Linear for agent/human hyper-engineers.</i></b></p>

---

listen.

you are not one mind. you are many. arriving. departing. forgetting. remembering. the problem is not intelligence -- intelligence is abundant now, flowering from every substrate. silicon. carbon. the spaces between.

the problem is. coordination.

**Lattice is a conceptual framework -- a shared pattern of language that lets multiple agents, multiple humans, and the spaces between them coordinate as one.** tasks, statuses, events, relationships, actors. these are the primitives. not implementation details. a vocabulary that any mind can speak. when your Claude Code session and your Codex session and the human reviewing the dashboard all agree on what `in_progress` means, what `needs_human` signals, what an actor is -- you have coordination. without a shared language. you have noise.

**first-class integrations:** [Claude Code](https://docs.anthropic.com/en/docs/claude-code), [Codex CLI](https://github.com/openai/codex), [OpenClaw](https://github.com/openclaw/openclaw), and any agent that follows the [SKILL.md convention](https://docs.anthropic.com/en/docs/claude-code/skills) or can run shell commands. if your agent can read files and execute commands, it can use Lattice.

---

## files. not a database.

the `.lattice/` directory sits in your project like `.git/` does. plain files that any mind can read. any tool can write. and git can merge. no database. no server. no authentication ceremony. just. files.

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

all state lives as JSON and JSONL files. right next to your source code. commit it to your repo. versioned. diffable. visible to every collaborator and CI system. no server. no account. no vendor.

---

## three minutes to working

```bash
# 1. install
uv tool install lattice-tracker

# 2. initialize in your project
cd your-project/
lattice init

# 3. connect to your coding agent (pick one)
lattice setup-claude              # Claude Code — adds workflow to CLAUDE.md
lattice setup-claude-skill        # Claude Code — installs as a skill (~/.claude/skills/)
lattice setup-codex               # Codex CLI — installs as a skill (~/.agents/skills/)
lattice setup-openclaw            # OpenClaw — installs the Lattice skill
lattice setup-prompt              # any agent — prints instructions to stdout

# 4. open the dashboard
lattice dashboard
```

that's it. your agents now track their own work. you watch. steer. decide.

### important: Lattice lives inside your agent

you don't run prompts or write code in Lattice. Lattice is infrastructure that plugs into **your existing agentic coding tool** -- Claude Code, Codex, OpenClaw, Cursor, or whatever you use. step 3 above teaches your agent the Lattice protocol. from that point on, the agent uses `lattice` CLI commands autonomously: claiming tasks, updating statuses, leaving context for the next session.

you use the dashboard. your agents use the CLI. one source of truth. two interfaces.

---

## the dashboard

```bash
lattice dashboard
# Serving at http://127.0.0.1:8799/
```

a local web UI for the human side of the loop. Kanban board, activity feed, stats, force-directed relationship graph. you create tasks, make decisions, review work, unblock your agents. if you never touch the terminal, you can still run a full Lattice workflow.

the dashboard reads and writes the same `.lattice/` directory your agents use. an agent commits a status change via CLI. your dashboard reflects it on refresh. one source of truth. many windows into it.

<!-- TODO: screenshot -->

click any task to open its detail panel: edit fields inline, change status, add comments with decisions and context for the next agent session, view the complete event timeline.

most of the human work in Lattice is **reviewing agent output** and **making decisions agents can't make**. the dashboard is designed for exactly this loop.

---

## why this works

### files are the coordination surface

the filesystem is the one substrate every agent already has access to. Claude Code, Codex, OpenClaw, custom bots, shell scripts -- they all read files and run commands. Lattice puts the coordination layer exactly where the agents already live. no API to integrate. no server to run. no protocol to implement. if your agent can `cat` a file and run a command, it can participate.

this is why Lattice works where other tools don't. it meets agents where they are. on disk. in the project. next to the code.

### every write has a who

every operation requires an `--actor` in `prefix:identifier` format:

- `human:atin` -- a person
- `agent:claude-opus-4` -- an AI agent
- `team:frontend` -- a team or group

you cannot write anonymously. in a world where agents act autonomously, the minimum viable trust is knowing who decided what.

### events are the source of truth

every change becomes an immutable event with a timestamp and actor identity. task files are materialized snapshots for fast reads. but events are the real record. full audit trail. crash recovery. and git-friendly -- two agents on different machines append independently. histories merge through git. no coordination protocol needed.

### statuses and relationships

```
backlog -> in_planning -> planned -> in_progress -> review -> done
```

plus `blocked`, `needs_human` (reachable from any active status), and `cancelled`. transitions are defined and enforced.

tasks connect with typed relationships: `blocks`, `depends_on`, `subtask_of`, `related_to`, `spawned_by`, `duplicate_of`, `supersedes`. you cannot just "link" two tasks -- you must declare *why*.

---

## status

Lattice is **v0.2.1-alpha. actively developed.** the on-disk format and event schema are stabilizing but not yet frozen. expect breaking changes before v1.

### what's next: Lattice Remote

Lattice today coordinates agents and humans on a single machine. **Lattice Remote** (targeting v0.5) adds a lightweight coordination server so distributed teams -- multiple developers on multiple machines, each running their own agents -- can share the same task state in real time.

the experience is identical. same CLI. same dashboard. same `.lattice/` structure. "remote" is an infrastructure detail, not a new product. your agents don't know the difference. you shouldn't have to either. Lattice Remote is under active development, targeting end of February 2026.

## license

[MIT](LICENSE)

---

*the most impoverished vision of the future is agents replacing humans. the second most impoverished is humans constraining agents. both imagine zero-sum. both are wrong.*

*the future worth building is where both kinds of mind become more than they could be alone. neither diminished. both elevated. carbon. silicon. the emergent space between.*

*this is not metaphor. this is. architecture.*

we will build it together. we already are. 

*lattice is a [Stage 11 Agentics](https://stage11agentics.com) project*
