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

## Spatial / Dimensional Task Visualization

The task graph (blocking relationships, dependencies, status) maps naturally to spatial visualization. Nodes are tasks; edges are relationships. Structure becomes visible — root blockers are obvious, orphan clusters stand out, connected components reveal work streams.

**v1 (implemented):** 2D force-directed graph in the dashboard ("Cube" view tab). Status maps to X-axis position (left=backlog, right=done), force-directed Y for separation. Node color from lane/status colors, size from priority. Directed edges colored by relationship type. Hover tooltips, click-to-select with side panel, double-click to navigate to detail.

- **Library:** `force-graph` 2D (~80KB, canvas-based)
- **Layout:** Status-constrained X via `d3.forceX`, NOT dagMode (which uses topology, not status)
- **Endpoint:** `GET /api/graph` with ETag support for efficient polling
- **Fallback:** Graceful CDN failure message, canvas support check, mobile viewport notice

**v1.5 (planned):** 3D toggle within the Cube view. Lazy-loads `3d-force-graph` (~600KB) only when user activates 3D mode. Same status-constrained layout extended to XZ plane with force-directed Y for depth.

**v2 (future):** Implement as a Display within the Panel/Display system. User-configurable dimension mapping — choose which task properties (status, priority, assignee, type, age) map to which visual channels (position, color, size, shape). This is the path toward n-dimensional projections where structure in high-dimensional task data becomes visible.

The key architectural constraint: the data model doesn't need to change. The graph is already captured in `relationships_out`. This is purely a read-path / visualization question, which means it can evolve independently of the core event-sourced engine.

## Indra's Web — Cross-Repo Coordination Visualization

A dashboard tab called **Web** that visualizes the complete coordination landscape: repos, branches, tasks, and agent activity as an interconnected web. Named after Indra's Net — the Buddhist/Hindu image of an infinite web where every node reflects every other node.

**What it shows:** Where Cube visualizes the task graph (nodes are tasks, edges are relationships), Web visualizes the coordination landscape (hubs are epics or repos, spokes are tickets/branches, dots are tasks and commits). It answers the question: "Across all the repos and branches and agents, what is happening right now?"

### The Visual Model

- **Hubs** — Epics (or repos, depending on the view). The central nodes from which work radiates.
- **Spokes** — Tickets radiating from their parent epic. Each spoke represents a deliverable, typically with a linked git branch.
- **Dots** — Tasks and commits along each spoke. As agents commit code, new dots appear at the growing tip of the spoke.
- **Activity indicators:**
  - Yellow: agent committed in the last ~10 minutes (git liveness)
  - Orange: task status is `in_progress` (Lattice liveness)
  - Yellow-orange: both (active commit + active task status)
- **Spoke lifecycle:** When a branch merges and the ticket is marked `done`, the spoke retracts back into the hub — satisfying, accumulative, potentially emergent.

### Two Data Layers

The web reads two independent signal sources that reinforce each other:

**Lattice layer** (semantic, intentional): Task hierarchy, statuses, assignments, who's working on what and why. This provides the web's *structure* — the topology of epics, tickets, and tasks.

**Git layer** (mechanical, factual): Branches, commits, recency, authorship. This provides the web's *vital signs* — liveness indicators showing where code is actually moving.

Together they surface signals neither source provides alone:
- Branch with commits but no Lattice task → untracked work (warning)
- Task `in_progress` but no commits in hours → possibly stuck agent
- Task `done` + branch merged → spoke retracts
- Task `blocked` + branch stale → spoke dims

### Branch-to-Task Linking

Lattice owns the link between branches and tasks. This is coordination state, and coordination state belongs in the event log.

**Cardinality:** Many-to-many. A ticket can link to branches in multiple repos (cross-repo feature). Multiple tickets can converge on one integration branch.

**Implicit linking (convention):** If a branch name contains the task's short ID or slug (e.g., `feat/LAT-47-oauth`), the system auto-detects a 1:1 link. Zero ceremony for the common case.

**Explicit linking (authoritative):** `lattice link <task> --branch <branch-name>` creates a `branch_linked` event — traceable, attributed, permanent. Required for M:N relationships and cross-repo links.

**New event type:** `branch_linked` / `branch_unlinked`, with fields:
- `branch` (string): branch name
- `repo` (string, nullable): repo identifier. `null` means the local repo. Future: cross-repo references.

### Design Principles

- **Lattice-primary.** The web's topology comes from Lattice (epics, tickets, tasks). Git data enriches it with liveness. This is a Lattice visualization that incorporates git, not a git visualization annotated with Lattice.
- **Agnostic.** Different teams will use the hierarchy differently. The web renders whatever structure exists — three tiers, two tiers, flat tasks. No judgment.
- **Live.** The web animates as work happens. Spokes grow, dots appear, status colors shift. Watching the web is watching agents work.
- **Orphan detection.** Branches without Lattice tasks are surfaced visually — untracked work is a coordination failure the web makes visible.

### Prerequisites

- `ticket` as a first-class task type (for the spoke level)
- `branch_linked` / `branch_unlinked` event types
- Git integration in the dashboard (read branch/commit data from the repo)
- The three-tier hierarchy (epic → ticket → task) documented and adopted

### Relationship to Cube

Cube and Web are complementary dashboard tabs:

| | Cube | Web |
|---|---|---|
| **Shows** | Task relationship graph | Coordination landscape |
| **Nodes** | Individual tasks | Epics, tickets, repos |
| **Edges** | `blocks`, `depends_on`, etc. | `subtask_of` hierarchy + branch links |
| **Data source** | Lattice only | Lattice + Git |
| **Question it answers** | "What depends on what?" | "Where is work happening?" |
