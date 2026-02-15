# Future Features

Candidates for post-v0 implementation, drawn from the Lattice vs Linear audit (2026-02-15).

## Cycles / Time-Boxing

Time-boxed iteration boundaries for agent work. Linear's model: auto-scheduled sprints with duration, cooldown periods, auto-rollover of incomplete work, velocity tracking, and capacity estimation. Lattice equivalent would be lighter — likely a config-defined cycle entity with start/end dates, task association, and rollover semantics. Agents benefit from iteration boundaries for scoping work and measuring throughput.

## Project-Level Grouping

A first-class entity above epics for grouping related work toward a deliverable. Properties: name, lead, members, start/target dates, milestones, progress tracking. Currently Lattice uses `epic` task type + relationships, but this doesn't give you aggregate progress, status updates, or a dedicated view. A project entity would sit between tasks and any future roadmapping layer.

## Analytics / Metrics Aggregation

Lattice already stores per-event metrics (tokens_in, tokens_out, cost_usd, latency_ms, tool_calls, retries, cache_hits) as passthrough data. The gap is aggregation and visualization. Candidates:
- Cost roll-ups per task, per agent, per time period
- Velocity tracking (tasks completed per cycle/week)
- Agent efficiency metrics (tokens per task, retry rates)
- Dashboard charts (burn-up, cumulative flow, scatter)
- CLI summary commands (`lattice stats`, `lattice costs`)

This is high-value because the data is already being captured — it just needs a read path.
