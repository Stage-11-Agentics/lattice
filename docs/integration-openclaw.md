# Lattice + OpenClaw

Your OpenClaw agent is powerful. It can write code, run commands, search the web, coordinate with other agents. But ask it tomorrow what it did today and it won't remember. Ask it what's left to do on your project and it'll guess. Ask three agents to work on the same codebase and they'll step on each other.

Lattice fixes this. It gives your OpenClaw agent a shared memory — a task graph that persists across sessions, tracks who did what, and lets agents coordinate without colliding. After setup:

- **Your agent tracks its own work** — creates tasks before coding, updates status at transitions, leaves notes for the next session
- **You see a sorted inbox** — a local dashboard showing work in review, decisions waiting on you, and blockers identified
- **Agents pick up where the last one left off** — task context, plans, and notes survive across sessions
- **Multiple agents coordinate safely** — file-level locking prevents corruption; event logs prevent confusion

The setup takes three minutes. The payoff starts on your first advance.

---

## Install Lattice

```bash
pip install lattice-tracker
```

Or with modern Python tooling:

```bash
pipx install lattice-tracker    # isolated install
uv tool install lattice-tracker # if you use uv
```

Verify it works:

```bash
lattice --help
```

---

## Initialize your first project

Navigate to your project and run:

```bash
cd /path/to/your/project
lattice init
```

You'll be asked two things:

1. **Your identity** — something like `human:alice` or `human:atin`. This is how Lattice knows who made each decision.
2. **A project code** — a short prefix like `APP` or `API`. This gives your tasks readable IDs: `APP-1`, `APP-2`, `APP-3`.

Or skip the prompts:

```bash
lattice init --actor human:alice --project-code APP
```

This creates a `.lattice/` directory in your project — think of it like `.git/` but for task tracking. Plain JSON and JSONL files. No database, no server, no account. **Commit it to your repo.** It's how context survives between sessions.

---

## Connect OpenClaw

You have two options. Pick the one that fits your setup.

### Option A: Install the Lattice skill (recommended)

```bash
lattice setup-openclaw
```

This copies the Lattice skill into your project's `skills/` directory. When your OpenClaw agent encounters task-related work, the skill injects Lattice CLI knowledge into the agent's context. The agent then uses `lattice` commands naturally, like any other tool.

For a global install (available to all your projects):

```bash
lattice setup-openclaw --global
```

### Option B: Use the MCP server

If you prefer structured tool calls over CLI, add Lattice as an MCP server in your OpenClaw config:

```json
{
  "servers": {
    "lattice": {
      "command": "lattice-mcp"
    }
  }
}
```

Or zero-install via `uvx` (no `pip install` needed — just `uv`):

```json
{
  "servers": {
    "lattice": {
      "command": "uvx",
      "args": ["--from", "lattice-tracker[mcp]", "lattice-mcp"]
    }
  }
}
```

The MCP server exposes all Lattice operations as typed tools: `lattice_create`, `lattice_status`, `lattice_list`, `lattice_next`, and more. Your agent gets structured JSON inputs and outputs instead of parsing CLI text.

**Use the skill** if you want the most natural OpenClaw experience. **Use MCP** if you use multiple AI tools and want one config for all of them. **Use both** — they don't conflict.

---

## Your first advance

This is where the lightbulb turns on.

### Step 1: Create some tasks

Open the dashboard to watch what's happening:

```bash
lattice dashboard
# Open http://127.0.0.1:8799 in your browser
```

Create a few tasks — from the dashboard UI or the terminal:

```bash
lattice create "Add user authentication" --actor human:alice --priority high
lattice create "Set up OAuth provider config" --actor human:alice --priority high
lattice create "Build login page" --actor human:alice --priority medium
lattice create "Add session middleware" --actor human:alice --priority medium
```

Your dashboard now shows four tasks in the Backlog column. You've defined *what* needs to happen and *in what order*. That's your job — deciding what matters.

### Step 2: Tell your agent to advance

In your OpenClaw conversation:

> "Advance the project. Claim the next highest-priority task, do the work, and report back."

Or more concisely: "Use `lattice next --actor agent:openclaw --claim` to pick a task, then work it."

Here's what happens:

1. The agent runs `lattice next --actor agent:openclaw --claim` — finds the highest-priority ready task and assigns it
2. The agent reads the task details and any notes from previous sessions
3. The agent does the work — writes code, runs tests, iterates
4. The agent leaves a comment: `lattice comment APP-2 "Set up OAuth with Google provider. Config in .env.example." --actor agent:openclaw`
5. The agent moves the task: `lattice status APP-2 review --actor agent:openclaw`
6. The agent reports what it did

### Step 3: Come back to a sorted inbox

Refresh your dashboard. The board tells the story:

- **Review column** — work the agent completed, ready for your eyes
- **Needs Human column** — decisions only you can make, each with a comment ("Need: which OAuth providers to support?")
- **In Progress column** — work currently underway
- **Backlog column** — what's still waiting

You review. You decide. You drag `needs_human` tasks back to In Progress after commenting with your answer. Then you advance again.

**This is the loop.** You produce judgment — priorities, decisions, direction. The agent produces throughput — code, tests, commits. Both are necessary. Neither works alone.

---

## What makes this different from just... using OpenClaw?

Three things:

**Persistence.** Without Lattice, every OpenClaw session starts from scratch. The agent doesn't know what happened in the last conversation. With Lattice, the task graph, event log, and notes survive across sessions. The agent reads `.lattice/` and knows exactly where things stand.

**Coordination.** When the agent hits something it can't decide — a design choice, missing credentials, ambiguous requirements — it moves the task to `needs_human` and says what it needs. You see it in your dashboard. No back-and-forth. The decision lands in the event log, attributed and permanent.

**Multi-agent safety.** Run two OpenClaw agents on the same project? Without Lattice, they'll edit the same files and create chaos. With Lattice, each agent claims tasks atomically, works independently, and the file-level locks prevent corruption. The event log shows exactly who did what.

---

## The daily rhythm

**Morning.** Open the dashboard. Handle the `needs_human` queue — those are agents waiting on you. Make the decisions. Move tasks back to active.

**Working.** Tell your OpenClaw agent to advance when you want progress. One advance = one task. Want more? "Advance 3 tasks" or "keep going until blocked."

**Review.** Check the Review column. Read agent comments. Approve, reject, or redirect. Create new tasks from what you learned.

**End of day.** Final scan. Close what you can. Update priorities for tomorrow.

---

## Actor IDs

Your OpenClaw agent should use `agent:openclaw` as its actor ID:

```bash
lattice create "My task" --actor agent:openclaw
lattice status APP-1 in_progress --actor agent:openclaw
```

For multi-agent setups, give each agent a unique ID:

```bash
--actor agent:openclaw-planner
--actor agent:openclaw-worker-1
--actor agent:openclaw-worker-2
```

---

## The advance pattern for OpenClaw

Since OpenClaw doesn't have a built-in `/lattice` skill like Claude Code, here's the pattern your agent follows:

```bash
# 1. Claim the next task
lattice next --actor agent:openclaw --claim --json

# 2. Read the task
lattice show APP-3

# 3. Check for context from previous sessions
cat .lattice/plans/<task_id>.md
cat .lattice/notes/<task_id>.md

# 4. Do the work...

# 5. Leave breadcrumbs
lattice comment APP-3 "What I did. What I decided. What's left." --actor agent:openclaw

# 6. Transition
lattice status APP-3 review --actor agent:openclaw
```

The Lattice skill teaches your agent this protocol. You don't need to spell it out every time — just say "advance the project" and the skill handles the rest.

---

## Heartbeat mode: autonomous advancing

During `lattice init`, you were asked whether to enable **heartbeat mode**. If you said yes, your agent doesn't stop after one task — it keeps sweeping the backlog automatically.

With heartbeat enabled, the agent loops:

1. Claims the highest-priority task and works it
2. Transitions the task (`review`, `done`, `needs_human`, `blocked`)
3. If the task needs you (`needs_human` or `blocked`), **stops and reports**
4. Otherwise, claims the next task and keeps going
5. Stops after 10 tasks (configurable) or when the backlog is empty

This is the heartbeat — the steady pulse of forward progress. You fill the backlog, walk away, come back to a sorted inbox with work completed, decisions queued, and blockers identified.

The Lattice skill reads the heartbeat config automatically. Just tell your OpenClaw agent "advance the project" and it will keep going until it needs you or runs out of work.

To enable heartbeat on an existing project, add this to `.lattice/config.json`:

```json
"heartbeat": {
  "enabled": true,
  "max_advances": 10
}
```

Then reinstall the skill to get the latest instructions:

```bash
lattice setup-openclaw --force
```

---

## Troubleshooting

**Agent doesn't know about Lattice.**
The skill isn't loaded. Run `lattice setup-openclaw` (or `--global` for all projects). Verify the skill exists: `ls skills/lattice/SKILL.md` or `ls ~/.openclaw/skills/lattice/SKILL.md`.

**`lattice` command not found.**
The CLI isn't installed or isn't on PATH. Run `pip install lattice-tracker` and verify with `lattice --help`. If using `pipx` or `uv tool`, ensure the tool bin directory is on your PATH.

**Agent creates tasks but doesn't update status.**
The skill teaches status updates, but agent compliance varies. Reinforce with an explicit instruction: "Always update Lattice status before starting work and after completing it."

**`lattice next` returns nothing but there are tasks in the backlog.**
Tasks may be assigned to a different actor, or all remaining tasks are in terminal/waiting states. Run `lattice list` to see the full picture.

---

## Quick reference

| Action | Command |
|--------|---------|
| Install | `pip install lattice-tracker` |
| Initialize | `lattice init --actor human:you --project-code APP` |
| Install OpenClaw skill | `lattice setup-openclaw` |
| Update skill | `lattice setup-openclaw --force` |
| Open dashboard | `lattice dashboard` |
| Create task | `lattice create "Title" --actor human:you` |
| Agent claims next task | `lattice next --actor agent:openclaw --claim` |
| Check inbox | `lattice list --status review` / `lattice list --status needs_human` |
| Daily digest | `lattice weather` |

---

## Next steps

- [User Guide](user-guide.md) — the full picture: dashboard, daily rhythm, philosophy
- [Claude Code Integration](integration-claude-code.md) — using Lattice with Claude Code
- [MCP Server](integration-mcp.md) — detailed MCP configuration for all clients
- [needs_human and advance guide](needs-human-and-next-guide.md) — deep dive on coordination primitives
- [Multi-Agent Guide](../src/lattice/skills/lattice/references/multi-agent-guide.md) — coordinating multiple OpenClaw agents
