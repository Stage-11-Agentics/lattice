# Architecture Summaries

This folder is a fast-path map for agents and humans. It is intentionally shorter
than reading the entire codebase and should be used before spawning deep
exploration.

Use this index to jump directly to the subsystem you need:

- `event-system.md` — event schema, append-only logs, lifecycle logging, provenance
- `snapshot-materialization.md` — how events become task snapshots and how rebuild works
- `completion-policies.md` — `workflow.completion_policies`, role evidence, and gates
- `cli-command-structure.md` — CLI entrypoint, command modules, helpers, and extension pattern
- `storage-layer.md` — atomic writes, locks, fs layout, hooks, and durability model
- `dashboard.md` — HTTP server architecture, API routes, write endpoints, and safety constraints

When behavior and docs disagree, code and `ProjectRequirements_v1.md` win.
