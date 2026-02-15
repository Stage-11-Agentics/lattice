# The Philosophy of Lattice

## What Lattice Is

Lattice is a file-based, event-sourced task tracker designed for a world where agents are first-class participants in the work. It is coordination infrastructure for autonomous agent teams — and the humans who orchestrate them.

## Why It Exists

Every existing task tracker assumes humans are the workers and humans are the audience. The UIs, notification models, permission systems, and data models are all optimized for human cognition and human workflows. When agents need to coordinate, they're forced through APIs designed for human-facing tools — rate-limited, authentication-heavy, and optimized for display rather than computation.

Lattice starts from a different premise: **the worker might be an agent, the audience might be an agent, and the coordinator might be an agent.** The human is a stakeholder who orchestrates, sets direction, and makes high-level decisions — not someone who drags cards between columns all day.

If you take that premise seriously, almost everything about traditional task management needs rethinking:

- **Task definitions** need to be machine-actionable, not just human-readable
- **Status transitions** need to be programmatic, not drag-and-drop
- **History** needs to be lossless and machine-parseable
- **The interface** needs to be CLI and filesystem, not web UI
- **Concurrency** needs to be a first-class concern, not an afterthought

## Core Convictions

### File-based is load-bearing

Agents are exceptionally good at working with filesystems. Files are the universal interface — every language, every tool, every agent framework can read and write them. By being file-based, Lattice lives where the work lives. No API keys, no network dependencies, no SaaS subscriptions. An agent working in a codebase can read task state as naturally as it reads source code.

This is not a v0 expedient. It is a permanent architectural choice.

### Events are facts, and facts don't conflict

The event log is the source of truth. Every state change is recorded as an immutable fact: "X happened at time T by actor A." Task snapshots are derived views, rebuildable from events.

This matters for two reasons:

1. **Accountability.** When agents operate autonomously, you need an answer to "what happened?" An agent's reasoning lives in its context window and disappears when the session ends. The event log is the persistent record of agent actions — not the reasoning, but the decisions and their sequence.

2. **Distributed coordination.** Events accumulate; they don't conflict. When two agents on different machines both append events and later sync through git, the resolution is always the same: include both, order by timestamp, rebuild snapshots. The event-sourced architecture makes git-based multi-machine coordination tractable without distributed systems complexity.

### The fractal principle

Lattice instances are self-similar at every scale. A repo-level instance, a workspace-level instance, and a program-level instance all use the same CLI, the same file format, the same event model. The only thing that changes is scope.

This enables hierarchical coordination:

```
AUT-3        Program level: "Ship OAuth"
AUT-F-7      Frontend subproject: "Add OAuth login page"
AUT-B-12     Backend subproject: "OAuth middleware"
```

Each level is an independent Lattice instance. Coordination between levels is agent-mediated — an agent with access to multiple instances reads state at one level and writes updates at another. The coupling exists in agent behavior, not in infrastructure. This keeps each instance simple and self-contained while enabling arbitrarily deep organizational structure.

### Simplicity is strategic patience

Lattice is deliberately minimal — no database, no network protocol, no authentication, no real-time sync. But the foundations are rigorous: event sourcing, atomic writes, deterministic lock ordering, crash recovery. This is not a toy. It is a small tool whose foundations are strong enough to grow.

The on-disk format is the stable contract. The CLI can be rewritten. The dashboard can be replaced. But the events, the file layout, and the invariants — those are the load-bearing walls.

### Agent-readable over machine-parseable

There are two kinds of structured information in Lattice:

- **`config.json`** — machine-parseable settings the CLI needs to function. Statuses, transitions, WIP limits, project codes. Rigid schema.
- **`context.md`** — agent-readable context about the instance. Purpose, related instances, conventions, quirks. Freeform markdown.

This reflects a belief about how agents work best: they don't need rigid schemas to understand context. An agent reading "infra tasks routinely take 2-3x estimates" acts on that knowledge just as well as a human would. The context file is the CLAUDE.md of a Lattice instance — soft knowledge that makes agents effective.

## What Lattice Is Not

- **Not a Jira/Linear replacement for human teams.** If your workflow is humans dragging cards on a board, use Linear. Lattice is for workflows where agents do the moving.
- **Not a distributed database.** Each instance is independent. Coordination happens through agents and git, not consensus protocols.
- **Not a product (yet).** It is infrastructure that Fractal Agentics builds on. Whether it becomes a product, a protocol, or stays internal is an open question shaped by usage.

## The Bet

Lattice is a bet that coordination infrastructure matters more in an agent-first world, not less. When humans coordinate, they compensate for bad tools with judgment, context, and Slack messages. When agents coordinate, the tool *is* the coordination. There is no Slack backchannel. The file format, the event schema, the CLI interface — these aren't implementation details. They are the language agents use to collaborate.

Get that language right, and agent teams become radically more capable. That's what Lattice is for.
