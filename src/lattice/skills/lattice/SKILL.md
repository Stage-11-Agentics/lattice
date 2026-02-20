---
name: lattice
description: Lattice agent coordination guide — mental model, CLI commands, lifecycle discipline, and reference for file-based task tracking across agents and sessions.
homepage: https://github.com/Stage-11-Agentics/lattice
metadata: {"openclaw":{"emoji":"clipboard","requires":{"bins":["lattice"]},"install":[{"id":"pip","kind":"command","command":"pip install lattice-tracker","bins":["lattice"],"label":"Install Lattice (pip)"},{"id":"pipx","kind":"command","command":"pipx install lattice-tracker","bins":["lattice"],"label":"Install Lattice (pipx)"},{"id":"uv","kind":"command","command":"uv tool install lattice-tracker","bins":["lattice"],"label":"Install Lattice (uv)"}]}}
---

# Lattice — Agent-Native Task Tracker

Lattice is a file-based, event-sourced task tracker. Everything lives in `.lattice/` in the project root. Every change is an immutable event. No accounts, no API keys, no network required.

## The Lifecycle — Opening and Closing Rituals

Every unit of work follows this arc: **claim → understand → work → complete**. The opening and closing rituals are non-negotiable.

### Opening Ritual

```bash
# 1. Claim the next task (or create one)
lattice next --actor agent:claude-cli --claim --json

# 2. If the task needs planning (empty plan file), plan first
lattice status <task_id> in_planning --actor agent:claude-cli
# ... write the plan to .lattice/plans/<task_id>.md ...
lattice status <task_id> planned --actor agent:claude-cli
lattice status <task_id> in_progress --actor agent:claude-cli
```

`lattice next --claim` atomically assigns the highest-priority ready task to you and moves it to `in_progress`. If you already have a task in progress, it returns that one (resume-first logic).

If there's no existing task, create one:

```bash
lattice create "Fix the login bug" --actor agent:claude-cli --priority high
```

### Closing Ritual

**`lattice complete` is THE way to finish work.** It performs the full completion ceremony in one command: posts a review comment, attaches a review artifact, transitions through review to done.

```bash
lattice complete <task_id> --review "What was done. Key decisions. Test results. What remains." --actor agent:claude-cli
```

The `--review` text is your breadcrumb for every future agent and human who reads this task. Be specific: files changed, approach taken, edge cases considered, anything left undone.

**Do not use raw `lattice status ... done` to finish work.** The `complete` command exists because completion requires evidence — a review comment and artifact. Skipping this ceremony leaves the task without an audit trail.

| Outcome | Action |
|---------|--------|
| **Done** | `lattice complete <task_id> --review "..." --actor agent:claude-cli` |
| **Need human input** | `lattice status <task_id> needs_human --actor agent:claude-cli` + comment explaining what you need |
| **Blocked on dependency** | `lattice status <task_id> blocked --actor agent:claude-cli` + comment explaining the blocker |

## The Work In Between

Between opening and closing:

1. **Read before writing.** `lattice show <task_id> --json`. Check `.lattice/plans/<task_id>.md` and `.lattice/notes/<task_id>.md` for context from previous minds.
2. **Check previous work.** If the task has prior events, investigate what happened. `git log --oneline --grep="<short_id>"` for prior commits.
3. **Baseline tests.** Run the test suite before changing anything. You own new failures, not pre-existing ones.
4. **Commit as you go.** Each meaningful unit of progress gets a commit.
5. **Push when done.** Each task is durable on the remote immediately.

## Core Commands

```bash
# Create
lattice create "Title" --actor agent:claude-cli --priority high --type bug

# List
lattice list                           # All active tasks
lattice list --status in_progress      # Filter by status
lattice list --assigned agent:claude-cli

# Show
lattice show PROJ-1                    # Summary
lattice show PROJ-1 --events           # Full event history

# Status transitions
lattice status PROJ-1 in_progress --actor agent:claude-cli

# Complete (the closing ritual)
lattice complete PROJ-1 --review "Review text" --actor agent:claude-cli

# Assign
lattice assign PROJ-1 agent:claude-cli --actor agent:claude-cli

# Comment
lattice comment PROJ-1 "Found root cause" --actor agent:claude-cli

# Link
lattice link PROJ-1 blocks PROJ-2 --actor agent:claude-cli

# Next task
lattice next --actor agent:claude-cli --claim --json

# Health
lattice weather    # Daily digest
lattice stats      # Statistics
lattice doctor     # Data integrity check

# Archive
lattice archive PROJ-1 --actor agent:claude-cli
```

Options for `create`: `--priority` (critical/high/medium/low/none), `--type` (task/bug/spike/chore), `--description "..."`, `--assign agent:claude-cli`

Relationship types for `link`: `blocks`, `blocked_by`, `subtask_of`, `parent_of`, `depends_on`, `depended_on_by`, `related_to`

## Status Workflow

```
backlog → in_planning → planned → in_progress → review → done
                                       ↕            ↕
                                    blocked      needs_human
```

Transitions are enforced. Use `--force --reason "..."` to override when genuinely needed.

## Actor IDs

Every command requires `--actor`. Format: `prefix:identifier`

- `agent:claude-cli` — default for your own actions
- `agent:worker-1`, `agent:worker-2` — multi-agent setups
- `human:username` — when acting on behalf of a human

## Task IDs

- **Short ID:** `PROJ-1`, `PROJ-42` (use these in conversation)
- **Full ULID:** `task_01HQ...` (internal, always accepted)

All commands support `--json` for structured output: `{"ok": true, "data": ...}` or `{"ok": false, "error": {"code": "...", "message": "..."}}`.

## Heartbeat Mode

Check if enabled: look for `"heartbeat": {"enabled": true}` in `.lattice/config.json`.

When enabled, keep advancing after each task: complete the current task → `lattice next --claim` → work the next one → repeat. Stop after `max_advances` (default 10), when the backlog is empty, or when a task hits `needs_human` or `blocked`.

## Rules

1. **Open before you work.** Every unit of work starts with a Lattice task. Not after. Not during. Before.
2. **Close with `lattice complete`.** Never raw `lattice status ... done`.
3. **One task at a time.** Finish or transition before claiming the next.
4. **Don't force transitions.** If a transition fails, investigate why.
5. **Don't cancel human tasks.** Use `needs_human` instead — let the human decide.
6. **Comment liberally.** The next agent has no hallway to find you in.

## References

Detailed guides live in `{baseDir}/references/`:

- **[Multi-Agent Guide](references/multi-agent-guide.md)** — orchestrator/worker patterns, actor ID conventions, concurrency safety
- **[Worktree Guide](references/worktree-guide.md)** — git worktree protocol for parallel development with shared Lattice state
