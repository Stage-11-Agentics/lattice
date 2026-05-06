# Lattice Multi-Agent Coordination Guide

This guide covers using Lattice to coordinate work across multiple agents.

## Architecture

Lattice is file-based and local. All state lives in `.lattice/` inside the project directory. Agents coordinate by reading and writing to this shared directory. File locks prevent corruption when multiple agents write simultaneously.

## Setting Up Multi-Agent Workflows

### 1. Initialize Lattice (once per project)

```bash
lattice init --project-code PROJ
```

### 2. Orchestrator Creates Tasks

The orchestrator agent creates and assigns work:

```bash
lattice create "Implement user auth" --actor agent:orchestrator --priority high
lattice create "Build login endpoint" --actor agent:orchestrator --assign agent:worker-1
lattice create "Build signup endpoint" --actor agent:orchestrator --assign agent:worker-2
lattice link PROJ-2 subtask_of PROJ-1 --actor agent:orchestrator
lattice link PROJ-3 subtask_of PROJ-1 --actor agent:orchestrator
```

### 3. Workers Claim and Execute

Each worker checks for assigned tasks:

```bash
# Check what's assigned to me
lattice list --assigned agent:worker-1

# Start working
lattice status PROJ-2 in_progress --actor agent:worker-1

# Leave progress notes
lattice comment PROJ-2 "Auth middleware implemented, writing tests" --actor agent:worker-1

# Complete
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

### 5. Handling Blocks (Alerts — LAT-210)

`needs_human` and `blocked` are alerts now, not statuses. The task stays in its current lifecycle column; the alert decorates it and excludes it from `lattice next`.

When a worker is stuck:

```bash
lattice raise PROJ-2 blocked --short "Need database schema from PROJ-5" --actor agent:worker-1
```

When a worker needs a human decision:

```bash
lattice raise PROJ-2 needs_human --short "Which OAuth provider to use?" --actor agent:worker-1
```

The orchestrator (or human) clears the alert when resolved:

```bash
lattice clear PROJ-2 needs_human --answer "Use Auth0" --actor human:atin
```

### 6. Event History

Every action is recorded as an immutable event:

```bash
lattice show PROJ-2 --events
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
