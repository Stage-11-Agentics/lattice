# Expert Plan Review: Lattice Cube — 3D Spatial Task Visualization

You are a senior software architect reviewing an implementation plan before code is written. Your job is to find flaws, gaps, and risks that the plan's author may have missed — not to validate their work. Be direct. If something is wrong, say so. If something is missing, name it.

## What You're Reviewing

An implementation plan for adding a "Cube" view to the Lattice dashboard — a 3D interactive spatial visualization of tasks, added as a new top-level navigation item alongside the existing Board, List, Activity, and Stats views.

---

## The Plan

### Context

Lattice is a file-based, event-sourced task tracker built for AI agent coordination. The dashboard is a **single HTML file** (~4800 lines, all CSS/JS inline) served by a Python stdlib `http.server`. No build step, no frameworks, no bundlers. State management is vanilla global variables. Rendering is string concatenation → `innerHTML`. Hash-based routing dispatches to `renderBoard()`, `renderList()`, etc.

The on-disk layout:
```
.lattice/
├── config.json          # Workflow statuses, transitions
├── tasks/*.json         # Task snapshots (full state, including relationships_out[])
├── events/*.jsonl       # Per-task event logs
└── archive/             # Archived tasks/events
```

Each task snapshot contains a `relationships_out` array with entries like:
```json
{"type": "blocks", "target_task_id": "task_01...", "created_by": "human:atin", "created_at": "..."}
```

Relationship types: `blocks`, `depends_on`, `subtask_of`, `related_to`, `spawned_by`, `duplicate_of`, `supersedes`.

The compact snapshot (used by `/api/tasks`) strips `relationships_out` down to `relationships_out_count` (a number). The full snapshot retains the array.

### Proposed Changes

**Library:** `3d-force-graph` (~600KB) loaded from jsDelivr CDN. Bundles Three.js internally. Supports DAG left-to-right mode, node/edge coloring, click/hover handlers, camera controls.

**1. Server (`src/lattice/dashboard/server.py`):**
- New `GET /api/graph` endpoint
- Reads all `.lattice/tasks/*.json` (full snapshots)
- Returns `{nodes: [{id, short_id, title, status, priority, type, assigned_to}], links: [{source, target, rel_type}]}`
- Only emits links where target task exists in active set

**2. Dashboard (`src/lattice/dashboard/static/index.html`):**
- CDN `<script>` for 3d-force-graph
- New nav tab: `<span class="nav-tab" data-view="cube">Cube</span>`
- Route dispatch: `else if (view === "cube") await renderCube();`
- CSS for `.cube-container`, `.cube-legend`, `.cube-empty`
- `renderCube()`: Fetches `/api/graph`, creates ForceGraph3D with:
  - `dagMode('lr')` — left-to-right by status
  - Node color from existing `getLaneColor(status)` (theme-aware)
  - Node size by priority
  - Edge color by relationship type
  - Edge arrows for directionality
  - Click node → navigate to task detail
  - `dagNodeFilter` to handle orphan nodes (no relationships)
  - `onDagError` to silently handle cycles
  - `zoomToFit()` after initial render
- `updateCubeData()`: For auto-refresh — re-fetches data, updates graph without reinit
- `cleanupCube()`: Disposes Three.js renderer when navigating away
- Legend overlay in bottom-left
- i18n keys for all 3 voice packs

**3. Docs (`FutureFeatures.md`):** Mark v1 Cube as implemented.

### Edge Cases Addressed

- 0 tasks → empty state
- No relationships → unconnected nodes float freely
- Circular dependencies → `onDagError` swallows, force-directed fallback
- Archived tasks → excluded from graph
- Theme changes → re-applies node color accessor
- Window resize → resize handler
- Auto-refresh → data-only update, no scene reinit

---

## Your Review Mandate

Evaluate this plan across the following dimensions. For each, give a verdict (solid / concern / gap) and explain.

### 1. Architectural Fit
Does this integrate cleanly with the existing dashboard's patterns? The current dashboard is a carefully maintained single-file vanilla JS app with no build step and minimal dependencies. Is adding a ~600KB CDN library consistent with the project's philosophy? Are there simpler alternatives that were overlooked? Does the rendering pattern (3d-force-graph creates its own canvas vs. the existing innerHTML pattern) introduce friction?

### 2. DAG Mode Assumptions
The plan uses `dagMode('lr')` to arrange nodes left-to-right by status. But `dagMode` in 3d-force-graph arranges by *graph topology* (parent→child), not by an arbitrary attribute like `status`. If the blocking relationships don't form a clean DAG from backlog→done, the left-to-right layout may not correspond to status progression at all. A task in `backlog` that blocks a task in `done` would be placed left, but a task in `in_progress` with no relationships would have no DAG position. Is the plan's mental model of DAG mode accurate? What happens when the graph topology contradicts the status ordering?

### 3. Data Completeness
The `/api/graph` endpoint reads `relationships_out` from each task. But relationships in Lattice are stored **unidirectionally** — if Task A blocks Task B, only Task A has `{type: "blocks", target: B}` in its `relationships_out`. Task B does NOT have `{type: "depends_on", target: A}` (it's derived at read time). Does the plan account for this? Will the graph show edges in both directions, or only outgoing? Is this sufficient for visualization?

### 4. Performance and Resource Management
- The plan fetches full snapshots for every active task on every `/api/graph` call. With 200+ tasks, each snapshot containing arbitrary custom_fields and history, is this efficient? Should the endpoint be more surgical?
- `updateCubeData()` does a `JSON.stringify()` comparison of the entire graph dataset on every 5-second refresh tick. Is this the right comparison strategy?
- WebGL context limits: browsers typically allow ~8-16 WebGL contexts. If the user rapidly navigates in/out of Cube, is `cleanupCube()` reliably called before the new context is created? Race conditions?

### 5. UX and Interaction Design
- The plan describes click-to-navigate but no way to *stay in the Cube* while inspecting a task. Clicking a node exits the Cube entirely. Is this the right interaction? Should there be a hover tooltip or side panel instead?
- For a project with 3-5 tasks (the common case for Lattice right now), will a 3D force-directed graph look compelling or just awkward? Is there a minimum viable node count where 3D adds value vs. being gimmicky?
- Camera controls (zoom, rotate, pan) are built into 3d-force-graph. Are there cases where the default controls are confusing or conflict with page scroll?

### 6. Offline / CDN Failure
The plan loads 3d-force-graph from jsDelivr CDN. If the CDN is unreachable (offline, corporate firewall, DNS failure), the Cube tab will show... what? A broken empty div? An error? The current dashboard has zero external CDN dependencies. Is there graceful degradation?

### 7. What's Missing
What did the plan NOT address that it should have? Think about:
- Testing strategy (no tests are proposed for the new endpoint or the JS)
- Accessibility (screen readers, keyboard navigation in a WebGL canvas)
- The "hyper-customizable regions" concept from the original vision — is it premature to build the Cube without the Panels/Displays extensibility system?
- Mobile experience (3D force-directed graph on a phone?)
- Browser compatibility (WebGL support, especially in terminal-embedded browsers)

### 8. Alternative Approaches
If you were designing this from scratch, would you make different choices? Consider:
- 2D force-directed graph (d3-force + SVG/Canvas) as a more accessible first step
- Constraint-based layout (status on X-axis, priority on Y-axis) instead of force-directed
- Progressive enhancement: start with a simple 2D dependency graph, make the 3D version a toggle within it

---

## Output Format

Structure your review as:

```
## Verdict: [APPROVE / REVISE / RETHINK]

### Summary
[2-3 sentences on the overall assessment]

### Critical Issues (must fix before implementation)
1. ...

### Significant Concerns (should fix, could defer)
1. ...

### Minor Notes
1. ...

### Recommended Changes
[Concrete modifications to the plan]
```

Be specific. "This might be a problem" is less useful than "This will break when X because Y, and the fix is Z."
