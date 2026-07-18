# Lattice Multi-Agent Coordination Guide

This guide covers using Lattice to coordinate work across multiple agents.

## Architecture

Lattice is file-based and local. All state lives in `.lattice/` inside the project directory. Agents coordinate by reading and writing to this shared directory. File locks prevent corruption when multiple agents write simultaneously.

## Setting Up Multi-Agent Workflows

### 1. Initialize Lattice (once per project)

```bash
lattice init --project-code PROJ --actor agent:orchestrator
```

(`init` runs the interactive setup interview unless **both** `--project-code` and `--actor` are supplied.)

### 2. Orchestrator Creates Tasks

The orchestrator agent creates and assigns work. Each ticket must have a real deliverable of its own — see the "container ticket" anti-pattern in the main skill. Express umbrella work as sibling tickets connected by `depends_on`, not an empty parent with `subtask_of` children.

```bash
lattice create "Add session-token validation middleware" --actor agent:orchestrator --priority high --assigned-to agent:worker-1
lattice create "Add login endpoint (uses session middleware)" --actor agent:orchestrator --assigned-to agent:worker-2
lattice create "Add signup endpoint (uses session middleware)" --actor agent:orchestrator --assigned-to agent:worker-3
lattice link PROJ-2 depends_on PROJ-1 --actor agent:orchestrator
lattice link PROJ-3 depends_on PROJ-1 --actor agent:orchestrator
```

Each of PROJ-1, PROJ-2, PROJ-3 ships a concrete change on its own; the dependency edges just sequence them. If the auth work fit in a single ticket, prefer that — three "areas" can live as sections in one description with a "parallel-execution map" up top.

### 3. Workers Claim and Execute

Each worker checks for assigned tasks:

```bash
# Check what's assigned to me
lattice list --assigned agent:worker-1

# Start working
lattice status PROJ-2 in_progress --actor agent:worker-1

# Leave progress notes
lattice comment PROJ-2 "Auth middleware implemented, writing tests" --actor agent:worker-1

# Hand off to review (the reviewer/orchestrator closes with `lattice complete`)
lattice status PROJ-2 review --actor agent:worker-1
```

### 4. Self-Assignment with `lattice next`

Workers can also find unassigned work:

```bash
# See what's available
lattice next --actor agent:worker-1

# Claim it
lattice next --actor agent:worker-1 --claim
```

`lattice next` considers priority, dependencies, and blockers to suggest the best task.

### 5. Handling Blocks

When a worker is stuck:

```bash
lattice status PROJ-2 blocked --actor agent:worker-1
lattice comment PROJ-2 "Blocked: need database schema from PROJ-5" --actor agent:worker-1
```

When a worker needs a human decision, it flags the task. `needs-human` is orthogonal to status — the flag rides on top of whatever status the task is in, so the work stays exactly where it was (here, still `in_progress`):

```bash
lattice needs-human PROJ-2 "Which OAuth provider to use?" --actor agent:worker-1
```

The reason is required. The orchestrator sees the flagged task via `lattice list --needs-human` (a queue spanning every status), resolves it, and clears the flag:

```bash
lattice needs-human PROJ-2 --clear --note "Decided: Google" --actor agent:worker-1
```

Use `blocked` (a status) for generic external dependencies; use the `needs-human` flag for "waiting on a human specifically." A task can be both blocked and flagged.

### 6. Event History

Every action is recorded as an immutable event:

```bash
lattice show PROJ-2 --full
```

This provides a full audit trail: who changed what, when, and why.

## Actor ID Conventions

| Agent | Actor ID |
|-------|----------|
| Orchestrator | `agent:orchestrator` |
| Worker agents | `agent:worker-1`, `agent:worker-2`, etc. |
| Specialized agents | `agent:tester`, `agent:reviewer`, etc. |
| Human oversight | `human:username` |

## Concurrency Safety

Lattice uses file locks to prevent concurrent write corruption:

- Locks are acquired in deterministic (sorted) order to prevent deadlocks
- Write operations are atomic (write to temp file, fsync, rename)
- Event appends are lock-protected with immediate flush
- If a crash occurs between event-write and snapshot-write, `lattice rebuild` recovers

## Best Practices

1. **One actor per agent instance.** Don't share actor IDs across concurrent agents.
2. **Update status before work.** Move to `in_progress` before writing code.
3. **Comment liberally.** The next agent reading this task has no context beyond what you leave.
4. **Use `--json` in scripts.** Structured output is easier to parse programmatically.
5. **Check `lattice next` between tasks.** It accounts for priorities and blockers.
