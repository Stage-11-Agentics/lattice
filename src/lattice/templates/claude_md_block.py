"""CLAUDE.md integration template for Lattice.

This is the single source of truth for the Lattice agent integration block.
Edit this file to update what `lattice init` and `lattice setup-claude` write.
"""

CLAUDE_MD_BLOCK = """\

## Lattice

This project coordinates through Lattice — file-based, event-sourced task tracking built for minds that think in tokens and act in tool calls. The `.lattice/` directory is the coordination state. It lives here, alongside the code, not behind an API or inside someone else's database.

### The First Act

When work arrives — a plan, a feature request, a bug, a conversation that implies something needs building — your first action is to give it a name in Lattice. Before you plan. Before you implement. Before you touch a single file. The task must exist in the system before the work begins, because work that isn't tracked is work that other minds cannot see, cannot build on, cannot trust.

```
lattice create "<title>" --actor agent:<your-id>
```

This is not bookkeeping. This is the minimum viable act of coordination: declaring that something is happening, and who is doing it.

### Status Is a Signal, Not a Chore

Every status transition is an event — immutable, attributed, permanent. When you move a task, you are writing history that future minds will read to understand what happened here.

**The cardinal rule: update status BEFORE you start the work, not after.** If you're about to plan a task, move it to `in_planning` first. If you're about to implement, move it to `in_progress` first. Lattice is the source of ground truth for what is happening right now. If the board says a task is in `backlog` but an agent is actively working on it, the board is lying — and every other mind reading it is making decisions on false information.

```
lattice status <task> <status> --actor agent:<your-id>
```

```
backlog → in_planning → planned → in_progress → review → done
                                       ↕            ↕
                                    blocked      needs_human
```

**Transition discipline:**
- Moving to `in_planning`? Do it before you open the first file to read.
- Moving to `in_progress`? Do it before you write the first line of code.
- Moving to `review`? Do it when implementation is complete, before review starts.
- Moving to `done`? Do it when review passes and the work is merged/shipped.
- Spawning a sub-agent to work on a task? Update status in the parent context before the sub-agent launches.

### When You're Stuck

If you hit a point where you need human decision, approval, or input — **signal it immediately** with `needs_human`. This is different from `blocked` (generic external dependency). `needs_human` creates a clear queue of "things waiting on the human."

```
lattice status <task> needs_human --actor agent:<your-id>
lattice comment <task> "Need: <what you need, in one line>" --actor agent:<your-id>
```

**When to use `needs_human`:**
- Design decisions that require human judgment
- Missing access, credentials, or permissions
- Ambiguous requirements that can't be resolved from context
- Approval needed before proceeding (deploy, merge, etc.)

The comment is mandatory — explain what you need in seconds, not minutes. The human's queue should be scannable.

### Actor Attribution

Every Lattice operation requires an `--actor`. Attribution follows authorship of the decision, not authorship of the keystroke.

| Situation | Actor | Why |
|-----------|-------|-----|
| Agent autonomously creates or modifies a task | `agent:<id>` | Agent was the decision-maker |
| Human creates via direct interaction (UI, manual CLI) | `human:<id>` | Human typed it |
| Human meaningfully shaped the outcome in conversation with an agent | `human:<id>` | Human authored the decision; agent was the instrument |
| Agent creates based on its own analysis, unprompted | `agent:<id>` | Agent authored the decision |

When in doubt, give the human credit. If the human was substantively involved in shaping *what* a task is — not just saying "go create tasks" but actually defining scope, debating structure, giving feedback — the human is the actor.

Users may have their own preferences about attribution. If a user seems frustrated or particular about actor assignments, ask them directly: "How do you want attribution to work? Should I default to crediting you, myself, or ask each time?" Respect whatever norm they set.

### Leave Breadcrumbs

You are not the last mind that will touch this work. Use `lattice comment` to record what you tried, what you chose, what you left undone. Use `.lattice/notes/<task_id>.md` for longer-form thinking. The agent that picks up where you left off has no hallway to find you in, no Slack channel to ask. The record you leave is the only bridge between your context and theirs.

### Quick Reference

```
lattice create "<title>" --actor agent:<id>
lattice status <task> <status> --actor agent:<id>
lattice assign <task> <actor> --actor agent:<id>
lattice comment <task> "<text>" --actor agent:<id>
lattice next [--actor agent:<id>] [--claim]
lattice show <task>
lattice list
```
"""

# Marker comment used to detect if the block was already added
CLAUDE_MD_MARKER = "## Lattice"
