# Lattice + Claude Code

You're about to change how you use Claude Code. Right now, your sessions are one-shot: you describe what you want, the agent builds it, you review. There's no memory between sessions. No record of what was tried. No way to say "keep going where you left off."

Lattice gives you that. After setup, your Claude Code agent will:

- **Track its own work** — every feature, bug fix, and refactor gets a task before the agent touches a file
- **Pick up where the last session left off** — tasks persist across sessions with full context
- **Tell you when it's stuck** — instead of guessing, the agent flags decisions that need you
- **Leave notes for the next session** — what was tried, what was chosen, what's left

The setup takes about three minutes. The payoff is immediate.

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

This creates a `.lattice/` directory in your project — think of it like `.git/` but for task tracking. It holds your tasks, event logs, plans, and notes as plain files. **Commit it to your repo.** It's lightweight, git-friendly, and it's how context survives between sessions.

---

## Connect Claude Code

This is the step that makes it click:

```bash
lattice setup-claude
```

This adds a block to your project's `CLAUDE.md` that teaches every Claude Code session how to use Lattice. Without this block, the agent *can* use Lattice if you ask. With it, the agent uses Lattice *by default* — creating tasks before coding, updating status at transitions, leaving breadcrumbs for the next session.

That's the setup. Three commands: `install`, `init`, `setup-claude`.

---

## Your first advance

Now for the part that makes the lightbulb turn on.

### Step 1: Create some tasks

Open the dashboard so you can see what's happening:

```bash
lattice dashboard
# Open http://127.0.0.1:8799 in your browser
```

Create a few tasks — either from the dashboard UI or the terminal:

```bash
lattice create "Add user authentication" --actor human:alice --priority high
lattice create "Set up OAuth provider config" --actor human:alice --priority high
lattice create "Build login page" --actor human:alice --priority medium
lattice create "Add session middleware" --actor human:alice --priority medium
```

Your dashboard now shows four tasks in the Backlog column. You've defined *what* needs to happen and *in what order* (via priority). That's your job — deciding what matters.

### Step 2: Tell your agent to advance

Open Claude Code in your project and type:

```
/lattice
```

That's it. One command teaches the agent the full lifecycle. Here's what happens behind the scenes:

1. The agent runs `lattice next --claim` — this finds the highest-priority ready task and atomically assigns it
2. The agent reads the task details, any plans or notes from previous sessions
3. The agent does the work — writes code, runs tests, iterates
4. The agent commits the changes
5. The agent leaves a comment explaining what it did and why
6. The agent moves the task to `review` (or `needs_human` if it hit a decision point)
7. The agent reports back to you with a summary

### Step 3: Come back to a sorted inbox

Refresh your dashboard. The board tells the story:

- **Review column** — work the agent completed, ready for your eyes
- **Needs Human column** — decisions only you can make, each with a comment explaining what the agent needs ("Need: REST vs GraphQL for the public API")
- **In Progress column** — work currently underway
- **Backlog column** — what's still waiting

You review the completed work. You make the decisions the agent couldn't. You drag `needs_human` tasks back to In Progress after leaving your answer as a comment. Then you advance again.

```
/lattice
```

The agent picks up the next task. Or resumes the one you just unblocked. The cycle continues.

**This is the loop.** You produce judgment — priorities, decisions, taste. The agent produces throughput — code, tests, commits. Both are necessary. Neither works without the other.

---

## What makes this different from just... asking Claude to code?

Three things:

**Persistence.** Without Lattice, every Claude Code session starts blank. The agent doesn't know what happened yesterday. With Lattice, the task graph, event log, and notes survive across sessions. The agent reads the history and picks up where the last session stopped.

**Coordination.** When the agent hits something above its pay grade — a design decision, missing credentials, ambiguous requirements — it moves the task to `needs_human` and explains what it needs. You see it in your dashboard queue. No Slack. No standup. The decision is in the event log, attributed and permanent.

**Accountability.** Every change is an immutable event. `agent:claude-cli` fixed the auth bug at 2:47pm. `human:alice` approved the schema change at 3:15pm. The record is permanent. When something breaks, you know exactly what happened, who decided it, and why.

---

## The daily rhythm

**Morning.** Open the dashboard. Scan the board. Handle the `needs_human` queue first — those are agents waiting on you. Make the decisions. Drag tasks back to active.

**Working.** Run `/lattice` when you want the agent to make progress. One advance = one task. Want more? "Do 3 advances" or "keep advancing until blocked." Control the pace.

**Review.** Check the Review column. Read agent comments. Approve, reject, or redirect. Create new tasks from what you learned. Priorities shift — let them.

**End of day.** Final scan. Anything you can close? Any patterns worth noting? Update priorities for tomorrow's advances.

---

## Advanced: multiple advances

You don't have to advance one at a time:

```
# In Claude Code:
/lattice                            # one task
"Advance the project 3 times"      # explicit count
"Keep advancing until blocked"      # manual loop
```

The agent works through the backlog in priority order, transitioning each task before moving to the next. With Claude Code, you control the pace — the agent advances when you tell it to.

For **fully autonomous advancing**, see the [OpenClaw integration](integration-openclaw.md#heartbeat-mode-autonomous-advancing) — OpenClaw supports heartbeat mode where the agent sweeps the backlog continuously without manual triggers.

## Advanced: the `/lattice` skill

The CLAUDE.md block covers the essentials — creating tasks, updating status, attribution. For the full CLI reference (every command, every flag), agents can load the `/lattice` skill on demand:

```
/lattice
```

The two are complementary:
- **CLAUDE.md block** — always loaded, teaches the workflow
- **`/lattice` skill** — loaded on demand, full command reference

---

## Keeping the integration current

The CLAUDE.md block comes from a template that improves over time. Update your project's block to the latest version:

```bash
lattice setup-claude --force
```

The `--force` flag replaces the existing block with the latest template. Without it, the command exits if it detects an existing block (to avoid accidental overwrites).

---

## Troubleshooting

**Agent ignores Lattice and starts coding immediately.**
The CLAUDE.md block is missing or positioned too low in the file. Run `lattice setup-claude --force`, then move the `## Lattice` section higher in CLAUDE.md. Instruction position affects compliance — put it in the first or second section.

**Agent uses wrong status names** (like `in_implementation` or `in_review`).
These are from old documentation. The real statuses are: `backlog`, `in_planning`, `planned`, `in_progress`, `review`, `done`, `blocked`, `needs_human`, `cancelled`. Update the block with `lattice setup-claude --force`.

**`lattice next` returns nothing but there are tasks in the backlog.**
The tasks may be assigned to a different actor, or all remaining tasks are in terminal/waiting states. Run `lattice list` to see the full picture.

---

## Quick reference

| Action | Command |
|--------|---------|
| Install | `pip install lattice-tracker` |
| Initialize | `lattice init --actor human:you --project-code APP` |
| Connect Claude Code | `lattice setup-claude` |
| Update integration | `lattice setup-claude --force` |
| Open dashboard | `lattice dashboard` |
| Create task | `lattice create "Title" --actor human:you` |
| Advance (in Claude Code) | `/lattice` |
| Check inbox | `lattice list --status review` / `lattice list --status needs_human` |
| Daily digest | `lattice weather` |

---

## Next steps

- [User Guide](user-guide.md) — the full picture: dashboard, daily rhythm, philosophy
- [OpenClaw Integration](integration-openclaw.md) — using Lattice with OpenClaw agents
- [MCP Server](integration-mcp.md) — structured tool calls for any MCP-compatible client
- [needs_human and advance guide](needs-human-and-next-guide.md) — deep dive on coordination primitives
