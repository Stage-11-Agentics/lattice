---
name: lattice
description: Event-sourced task tracking for agents and humans. Use when managing tasks, tracking work status, coordinating multi-agent workflows, or maintaining an audit trail of who did what and when.
homepage: https://github.com/Stage-11-Agentics/lattice
metadata: {"openclaw":{"emoji":"clipboard","requires":{"bins":["lattice"]},"install":[{"id":"pip","kind":"command","command":"pip install lattice-tracker","bins":["lattice"],"label":"Install Lattice (pip)"},{"id":"pipx","kind":"command","command":"pipx install lattice-tracker","bins":["lattice"],"label":"Install Lattice (pipx)"},{"id":"uv","kind":"command","command":"uv tool install lattice-tracker","bins":["lattice"],"label":"Install Lattice (uv)"}]}}
---

# Lattice — Agent-Native Task Tracker

Lattice is a file-based, event-sourced task tracker. It stores everything in a `.lattice/` directory in the project root (like `.git/` for version control). Every change is an immutable event. No accounts, no API keys, no network required.

## When to Use Lattice

Use Lattice when the user or the conversation involves:

- Creating, tracking, or managing tasks
- Coordinating work across multiple agents
- Maintaining an audit trail of decisions and changes
- Planning sprints, releases, or project milestones
- Checking what work is in progress, blocked, or done

## Setup

Check if Lattice is available and initialized:

```bash
bash {baseDir}/scripts/lattice-check.sh
```

Or simply run `lattice list`. If `lattice` is not found, it needs to be installed (see the install methods in the frontmatter above). If `.lattice/` is not found, initialize it:

```bash
lattice init --project-code PROJ
```

Replace `PROJ` with a short project code (e.g., `APP`, `API`, `WEB`). This creates the `.lattice/` directory.

## Core Commands

### Create a task

```bash
lattice create "Fix the login bug" --actor agent:openclaw --priority high
```

Options: `--priority` (critical/high/medium/low/none), `--type` (task/epic/bug/spike/chore), `--description "details"`, `--assign agent:openclaw`

### List tasks

```bash
lattice list                           # All active tasks
lattice list --status in_progress      # Filter by status
lattice list --assigned agent:openclaw # Filter by assignee
lattice list --priority high           # Filter by priority
lattice list --json                    # Structured output
```

### Update task status

```bash
lattice status PROJ-1 in_progress --actor agent:openclaw
lattice status PROJ-1 done --actor agent:openclaw
```

### Assign a task

```bash
lattice assign PROJ-1 agent:openclaw --actor agent:openclaw
```

### Add a comment

```bash
lattice comment PROJ-1 "Found the root cause: race condition in auth middleware" --actor agent:openclaw
```

### Show task details

```bash
lattice show PROJ-1              # Summary view
lattice show PROJ-1 --events     # With full event history
```

### Link related tasks

```bash
lattice link PROJ-1 blocks PROJ-2 --actor agent:openclaw
lattice link PROJ-3 subtask_of PROJ-1 --actor agent:openclaw
```

Relationship types: `blocks`, `blocked_by`, `subtask_of`, `parent_of`, `depends_on`, `depended_on_by`, `related_to`

### Archive completed work

```bash
lattice archive PROJ-1 --actor agent:openclaw
```

### Get next task to work on

```bash
lattice next --actor agent:openclaw          # Suggest next task
lattice next --actor agent:openclaw --claim  # Suggest and auto-assign
```

### Project health

```bash
lattice weather    # Daily digest / weather report
lattice stats      # Project statistics
lattice doctor     # Check data integrity
```

## Status Workflow

```
backlog → in_planning → planned → in_progress → review → done
                                       ↕            ↕
                                    blocked      needs_human
```

- `backlog` — work identified but not started
- `in_planning` — actively being planned or specced
- `planned` — plan is ready, waiting to start
- `in_progress` — actively being worked on
- `review` — implementation done, under review
- `done` — complete
- `blocked` — waiting on an external dependency
- `needs_human` — requires human decision or input
- `cancelled` — abandoned

Transitions are enforced. Use `--force --reason "..."` to override when needed.

## Actor IDs

Every command requires `--actor` to identify who made the change. Format: `prefix:identifier`

- `agent:openclaw` — for your own actions
- `agent:openclaw-worker-1` — for multi-agent setups
- `human:username` — when acting on behalf of a human

Always use `agent:openclaw` as your actor ID unless the user specifies otherwise.

## Task IDs

Tasks have two forms:
- **Short ID:** `PROJ-1`, `PROJ-42` (use these in conversation)
- **Full ULID:** `task_01HQ...` (internal, always accepted)

Short IDs require a project code (set during `lattice init`).

## Structured Output

All commands support `--json` for machine-readable output:

```bash
lattice list --json
```

Returns `{"ok": true, "data": [...]}` on success or `{"ok": false, "error": {"code": "...", "message": "..."}}` on failure.

## Notes Files

Every task has a notes file at `.lattice/notes/<task_id>.md`. Write plans, decisions, and context there for future reference:

```bash
cat .lattice/notes/task_01HQ*.md  # Read a task's notes
```

These are free-form markdown — edit directly.

## Multi-Agent Coordination

Lattice handles concurrent writes safely with file locks. Multiple agents can work simultaneously:

1. Each agent uses a unique actor ID (`agent:openclaw-1`, `agent:openclaw-2`)
2. Create tasks from the orchestrator, assign to workers
3. Workers update status and add comments as they progress
4. Lock-based concurrency prevents file corruption
5. Event log provides full audit trail of who did what

For detailed multi-agent patterns, read `{baseDir}/references/multi-agent-guide.md`.

## Heartbeat: Auto-Advancing

Check if heartbeat mode is enabled:

```bash
cat .lattice/config.json | grep heartbeat
```

If `"heartbeat": {"enabled": true, ...}` is present, **keep advancing after each task**:

1. Complete the current task (transition to `review`, `done`, `needs_human`, or `blocked`).
2. If the task moved to `needs_human` or `blocked`, **stop and report**.
3. Otherwise, run `lattice next --actor agent:openclaw --claim --json`.
4. If a task is returned, work it. Full commitment — implement, test, commit, comment, transition.
5. Repeat from step 1.
6. Stop after the number of advances specified in `max_advances` (default 10), or when the backlog is empty.

The human fills the backlog and makes decisions. You sweep it. This is the heartbeat.

If heartbeat is not enabled, work one task and report back.

## Tips

- **Update status before starting work**, not after. If you're about to implement something, move it to `in_progress` first.
- **Leave comments** explaining what you tried, what you chose, and what you left undone. The next agent has no hallway to find you in.
- **Use `lattice next`** to find the highest-priority unblocked task.
- **Use `needs_human`** when you need a human decision — it creates a clear queue.
