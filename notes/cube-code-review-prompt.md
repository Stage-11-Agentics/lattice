# Code Review: Cube View — 2D Force-Directed Task Graph

## What was built

A new "Cube" tab in the Lattice dashboard that renders all non-archived tasks as a 2D force-directed graph. Tasks are positioned along the X-axis by workflow status (backlog → done, left to right) using `d3.forceX`, with force-directed Y-axis spreading for visual separation. Relationships (blocks, depends_on, subtask_of, etc.) render as directed edge arrows between nodes.

## Commits (oldest → newest)

- `bef765e` — `feat: add GET /api/graph endpoint for task relationship visualization`
- `c323326` — `docs: update spatial visualization section to reflect Cube v1 implementation`
- `303130f` — `feat: add Cube view — 2D force-directed task graph visualization`
- `3286557` — `fix: load d3 dependency chain correctly for Cube view CDN scripts`

(Ignore `c3b8778` plugin system and `b17a971` plugin test fix — those are unrelated.)

## Files touched

### Server-side
- **`src/lattice/dashboard/server.py`** — New `GET /api/graph` endpoint. Returns `{nodes, links, revision}` with ETag support (304 Not Modified). Filters archived tasks, strips heavy fields from nodes, resolves `relationships_out` into directed edges, filters links to non-existent targets.

### Client-side
- **`src/lattice/dashboard/static/index.html`** — All changes in this single file:
  - CDN script tags: d3@7, d3-binarytree@1, d3-octree@1, d3-force-3d@3, force-graph@1
  - Nav tab ("Cube") added after Stats
  - CSS classes: `.cube-empty`, `.cube-fallback-*`, `.cube-mobile-notice`, `.sr-only`
  - Global state vars: `cubeGraph`, `cubeResizeHandler`, `cubeData`, `cubeRenderGeneration`, `cubeCurrentRevision`
  - Constants: `CUBE_EDGE_COLORS`, `CUBE_PRIORITY_SIZE`
  - Functions: `renderCube()`, `updateCubeData()`, `cleanupCube()`, `buildCubeLegendHTML()`
  - Modified: `route()` (cube dispatch + cleanup guard), `startAutoRefresh()` (revision-based cube refresh)

### Tests
- **`tests/test_dashboard/test_graph_api.py`** — 7 test classes covering: basic response shape, archived task exclusion, missing-target link filtering, node field validation (whitelist + forbidden fields), empty lattice, ETag 304, ETag mismatch.

### Docs
- **`Decisions.md`** — New entry: "Cube view — 2D-first graph visualization with status-constrained layout"
- **`FutureFeatures.md`** — Updated spatial visualization section with v1 (implemented), v1.5 (planned 3D), v2 (Panel/Display integration)

## Design artifacts (not shipped, reference only)
- `notes/cube-impl/core-rendering.js` — ForceGraph setup, forces, canvas rendering
- `notes/cube-impl/lifecycle-routing.js` — Routing integration, generation counter, auto-refresh
- `notes/cube-impl/tooltip-inspection.js` — Hover tooltips, click/selection, side panel
- `notes/cube-impl/resilience.js` — CDN fallback, canvas check, mobile detection, a11y
- `notes/cube-impl/styles.css` — CSS classes for cube UI elements

## Key design decisions to review

1. **Status-constrained X-axis**: Uses `d3.forceX` pinned to status column index, NOT `dagMode` (which arranges by graph topology, not workflow status)
2. **Async render generation counter**: Prevents stale async renders from clobbering the UI on rapid navigation
3. **Revision-based auto-refresh**: `/api/graph` returns a `revision` string; cube polls and only calls `updateCubeData()` when revision changes
4. **Edge directionality**: `relationships_out` on source task → `{source, target, type}` links; edges are directed
5. **CDN dependency chain**: d3@7 → d3-binarytree@1 → d3-octree@1 → d3-force-3d@3 → force-graph@1 (order matters for UMD globals)
