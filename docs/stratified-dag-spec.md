# Strata View — Specification

> **Status:** Draft v2 (post-design-review)
> **Lattice task:** WRK-12 (00-lattice instance)
> **Supersedes:** WRK-5 (30-agentics instance) — original SVG-based DAG spec
> **Authors:** human:valerie, agent:claude-opus-4-orchestrator
> **Date:** 2026-03-09

---

## 1. Overview

The Strata view is a **recursive multi-instance 3D visualization** for Lattice. It extends the existing Cube view (Three.js + CSS3D + d3-force) to show tasks from multiple Lattice instances — a root project and its sub-projects — as horizontal planes in 3D space.

Each stratum is a translucent horizontal plane representing one Lattice instance discovered by filesystem traversal. Within each plane, tasks are positioned by the same axes as the Cube view: X = status, Z = recency. The Y axis encodes **stratum depth** (filesystem depth of the `.lattice/` instance), with **priority as a secondary Y-offset within each stratum** (high tasks float up, low tasks sink down within their plane).

Cross-instance edges (`subtask_of`, `spawned_by`) connect nodes across planes, visualizing the recursive decomposition of work.

**Primary use case:** Understanding the shape of a recursive project — which orchestrator tasks spawn child tasks in sub-projects, how deep the decomposition goes, and where in the lifecycle each layer sits.

**Design reference:** Mockups G4a (rendered planes) and G4b (wireframe) in `recursive-lattice-mockups/`.

**Tab name:** "Strata" — sits alongside "Cube" as a separate tab, does not replace it.

---

## 2. Data Model

### 2.1 New endpoint: `/api/graph/recursive`

The existing `/api/graph` serves single-instance data. The Strata view requires a new endpoint that aggregates multiple instances.

**Discovery:** Walk the filesystem from cwd, bounded at `maxdepth=3`:
```
find . -maxdepth 3 -name '.lattice' -type d
```

**For each discovered instance:**
1. Read `config.json` — check `schema_version` matches the root. Skip mismatched instances.
2. Read all `tasks/*.json` snapshots (same logic as existing `/api/graph`).
3. Collect `relationships_out` into links.

**Response schema:**

```json
{
  "ok": true,
  "data": {
    "instances": [
      {
        "path": ".",
        "project_code": "WRK",
        "depth": 0
      },
      {
        "path": "./subproject",
        "project_code": "SUB",
        "depth": 1
      }
    ],
    "nodes": [
      {
        "id": "task_...",
        "short_id": "WRK-3",
        "instance_path": ".",
        "instance_depth": 0,
        "title": "...",
        "status": "in_progress",
        "priority": "high",
        "type": "task",
        "assigned_to": "agent:...",
        "created_at": "...",
        "updated_at": "..."
      }
    ],
    "links": [
      {
        "source": "task_A",
        "target": "task_B",
        "type": "subtask_of",
        "cross_instance": false
      },
      {
        "source": "task_C@./subproject",
        "target": "task_D",
        "type": "subtask_of",
        "cross_instance": true
      }
    ],
    "revision": "recursive:12:2026-03-09T..."
  }
}
```

**Cross-instance link resolution:** When a task's `relationships_out` contains a qualified `target_task_id` (e.g., `task_XXXX@../`), the endpoint resolves the relative path against the instance that stores the link. The response normalizes all task IDs to be globally unique (prefixed with instance path where ambiguous). The `cross_instance` flag on links lets the frontend distinguish inter-stratum hierarchy edges from intra-stratum relationships.

**Caching:** ETag based on `"{total_node_count}:{max_updated_at_across_all_instances}"`.

### 2.2 Qualified task references

Cross-instance relationships use qualified IDs in `relationships_out`:

```json
{
  "type": "subtask_of",
  "target_task_id": "task_01XXXX@..",
  "created_by": "agent:orchestrator",
  "created_at": "2026-03-09T...",
  "note": null
}
```

**Format:** `<task_ulid>@<relative_path>` where path is relative to the instance storing the link.

**v1 constraints:**
- Paths are relative to the linking instance (child points up to parent via `@..`).
- The recursive endpoint resolves from root-and-downward only.
- Viewing from a middle instance does NOT show parent strata (v2).
- If a directory is moved, cross-instance links break (acceptable for v1; v2 may use stable instance IDs).

**Scope:** The `@path` syntax is allowed for ALL relationship types in the data model (validation in `core/relationships.py` accepts it). However, the Strata view only uses `subtask_of` and `spawned_by` for strata grouping / cross-plane edges. Other cross-instance relationship types (`depends_on`, `blocks`, `related_to`) are stored but not rendered in this view. They remain intra-instance for practical purposes.

### 2.3 Strata assignment

Strata are determined by **filesystem depth of the `.lattice/` instance**, not by edge traversal:

| Instance path | Stratum |
|--------------|---------|
| `.` | 0 (root) |
| `./subproject` | 1 |
| `./subproject/deep` | 2 |

Max depth: 3 strata (enforced by discovery `maxdepth`).

### 2.4 Cross-instance edge semantics

The Strata view renders cross-plane edges for two relationship types:

- **`subtask_of`**: child task declares "I am a subtask of [parent task in another instance]." Rendered as hierarchy edge from parent plane down to child plane.
- **`spawned_by`**: child task declares "I was spawned by [parent task in another instance]." Same visual treatment as `subtask_of` — both represent recursive decomposition.

All other relationship types (`depends_on`, `blocks`, `related_to`, `duplicate_of`, `supersedes`) are intra-instance only in this view. They render within their stratum plane as standard Cube-style edges.

### 2.5 Cycle handling

If `subtask_of`/`spawned_by` edges across instances form a cycle (shouldn't happen with filesystem-depth strata, but defensive):
1. Strata assignment is filesystem-based, not edge-based — cycles can't affect layout.
2. Cross-instance edges pointing "upward" (child→parent where child is at a lower depth) render normally.
3. Edges pointing "downward" or same-level are flagged with a console warning and rendered distinctly (red tint).

---

## 3. Layout Algorithm (3D, extending Cube)

### 3.1 Axes

| Axis | Encoding | Force strength |
|------|----------|---------------|
| **X** | Status lane (`statusIndex * LANE_SPACING`) | 0.95 (strong) |
| **Y** | Stratum band center + priority offset within band | 0.8 (strong) |
| **Z** | Recency (`-sqrt(t) * 300`) | 0.35 (moderate) |

### 3.2 Y-axis computation

Each node's Y target:
```
Y = stratum_index * BAND_SPACING + priority_offset
```

| Constant | Value | Notes |
|----------|-------|-------|
| `BAND_SPACING` | 200 units | Vertical distance between strata planes |
| Priority offsets (within band) | critical: +60, high: +25, medium: 0, low: -30 | Same relative spread as Cube's `PRIORITY_Y`, compressed to fit within band |

Stratum 0 (root) at Y=0, stratum 1 at Y=-200, stratum 2 at Y=-400. Root on top, children below.

### 3.3 d3-force simulation

Same d3-force-3d simulation as Cube, with modified Y force:
- `forceX`: strength 0.95 → status lanes (unchanged)
- `forceY`: strength 0.8 → `stratum_center + priority_offset` (replaces Cube's priority-only Y)
- `forceZ`: strength 0.35 → recency (unchanged)
- `forceManyBody`: -60 (unchanged)
- `forceLink`: distance 50, strength 0.3 (unchanged, but now includes cross-instance links)

### 3.4 Strata planes

Translucent horizontal `PlaneGeometry` meshes at each stratum's Y center:
- Size: spans the full X (status lanes) × Z (recency depth) extent
- Material: `MeshBasicMaterial`, transparent, opacity 0.03, `depthWrite: false`
- Alternating tint per stratum (even: warm, odd: cool) for visual distinction
- Label sprite at the left edge: `"project_code — instance_path"` (e.g., `"WRK — . (root)"`, `"SUB — ./subproject"`)

---

## 4. Visual Encoding

### 4.1 Nodes

Same as Cube view: instanced spheres with per-instance color (status) and scale (priority). No changes to the existing rendering pipeline. Nodes are tagged with `instance_path` for strata grouping but rendered identically.

### 4.2 Intra-instance edges

Same as Cube: line segments with flow particles. Color from `EDGE_COLORS[link.type]`. These stay within their stratum plane.

### 4.3 Cross-instance edges (hierarchy)

Cross-plane edges (`subtask_of`, `spawned_by` where `cross_instance: true`) get distinct treatment:

**Family coloring:** Each parent task that has cross-instance children gets a unique family color from a curated palette (d3 `category10` or similar — 10 guaranteed-distinct hues). All edges from one parent share that parent's family color.

**Default rendering (always visible):**
- Solid line, 2px stroke, family color at 40% opacity
- Flow particles in family color (same mechanism as Cube)
- Small arrowhead at child end

**Hover intensification:**
- When a node is hovered, its family's edges intensify to 90% opacity
- Connected nodes (both parent and siblings under the same parent) highlight
- All non-family nodes and edges dim to 10% opacity
- Both the hovered node AND its connected edges + nodes highlight

### 4.4 LOD system

Reuse the Cube's 5-level LOD (sphere → label → card → workspace panel). The LOD labels show `short_id` which already includes the project code prefix, making instance membership visible at LOD 1+.

---

## 5. Interaction Model

### 5.1 Inherited from Cube

All Cube interactions carry over unchanged:
- Orbit controls (rotate, pan, zoom)
- Click-to-fly to node
- ESC to pull back
- Search highlighting
- LOD zoom (semantic zoom from dots to cards)

### 5.2 Strata-specific interactions

- **Plane click:** Clicking a strata plane label focuses the camera on that stratum (smooth flight to center of the plane).
- **Family hover:** Hovering a cross-instance edge or a node with cross-instance children highlights the entire family subtree across planes.

---

## 6. Technical Approach

### 6.1 Rendering: Three.js (extending Cube patterns)

**NOT SVG.** The Strata view is a 3D Three.js view, following the same architecture as `cube3d.js`.

### 6.2 File structure

New file: `src/lattice/dashboard/static/strata.js`

Follows cube3d.js conventions:
- Own `_strata` state bag (singleton mutable state, same pattern)
- 3 public globals: `renderStrata()`, `updateStrataData()`, `cleanupStrata()`
- Generation-based stale prevention
- Same LOD system, CSS3D billboarding, custom orbit controls

**Code reuse strategy:** Pattern-level reuse, not code-level. `strata.js` is written from scratch following cube3d.js patterns. Pure functions (easing curves, color lookups, recency mapping) are copied. Impure functions (scene setup, simulation, rendering) are reimplemented against `_strata` state bag with modified force config and strata-plane geometry.

Rationale: cube3d.js's architecture is ~30 functions sharing one mutable state bag. Extracting a shared library requires decoupling that state, which is a significant refactor of working code. For v1, independent files with shared patterns. Refactoring shared primitives into `cube-lib.js` is a v2 concern once both views stabilize and their shared surface area is empirically known.

### 6.3 Companion CSS

New file: `src/lattice/dashboard/static/strata.css`

Mirrors `cube3d.css` structure for the CSS3D card/panel styling. Strata-specific additions: plane label styling, family-color legend.

### 6.4 Backend changes

| File | Change |
|------|--------|
| `dashboard/server.py` | New handler `_handle_graph_recursive()` — ~100-150 lines. Discovery, multi-instance loading, cross-instance link resolution. |
| `core/relationships.py` | Update `build_relationship_record()` and validation to accept `target_task_id` containing `@path` suffix. |
| `tests/test_dashboard/test_graph_api.py` | New test class `TestGraphRecursive` — multi-instance fixture, cross-instance links, schema mismatch rejection. |

### 6.5 Dashboard integration (index.html)

1. **Tab:** `<span class="nav-tab" data-view="strata">Strata</span>`
2. **Script/CSS tags:** `<script src="/static/strata.js" defer>`, `<link rel="stylesheet" href="/static/strata.css">`
3. **Router:** `else if (view === "strata") await renderStrata();`
4. **Cleanup:** `if (currentView === "strata" && ...) cleanupStrata();`
5. **Data refresh:** `if (currentView === "strata") updateStrataData(data);`

### 6.6 Dependencies

Same as Cube: Three.js + d3-force-3d from CDN. No additional dependencies.

---

## 7. Theme Support

Inherits Cube's theme system entirely. Strata-specific additions:

| Element | Color source |
|---------|-------------|
| Strata plane (even) | `rgba(255, 200, 100, 0.03)` (warm tint) |
| Strata plane (odd) | `rgba(100, 200, 255, 0.03)` (cool tint) |
| Strata label | `var(--text-secondary)` |
| Family edge colors | Curated 10-color palette, theme-aware (brighter on dark themes) |
| Family edge (dimmed) | Family color at 10% opacity |
| Family edge (hover) | Family color at 90% opacity |

---

## 8. Scope Boundaries

### What v1 does NOT include

- **Upward resolution:** Viewing from a sub-project does not show parent strata. Root-and-down only.
- **Explicit instance registry:** Discovery is filesystem-only. No `children` config field.
- **Stable instance IDs:** References use relative paths. Moving directories breaks links.
- **Cross-instance `depends_on`/`blocks` rendering:** Stored in data model but not visualized in Strata.
- **Shared code library:** No `cube-lib.js` extraction. Pattern reuse only.

### v2 candidates

- Explicit instance declaration in `config.json` (`children: [...]`)
- Stable instance-ID qualified references (`task_XXXX@inst_YYYY`)
- Upward + sibling resolution (view from any instance in the tree)
- `cube-lib.js` extraction (shared rendering primitives)
- Cross-instance dependency rendering
- Strata collapse/expand (hide a plane, show summary)

---

## 9. Acceptance Criteria

### Must have (v1)

- [ ] "Strata" tab appears in nav bar alongside "Cube"
- [ ] `/api/graph/recursive` endpoint discovers child `.lattice/` dirs (maxdepth 3)
- [ ] Endpoint skips instances with mismatched `schema_version`
- [ ] Each stratum renders as a translucent horizontal plane at correct Y position
- [ ] Nodes from each instance sit on their respective strata plane
- [ ] X = status, Y = stratum + priority offset, Z = recency (same as Cube within each plane)
- [ ] Cross-instance `subtask_of` and `spawned_by` edges render between planes
- [ ] Family coloring: edges from one parent share a curated palette color
- [ ] Hover intensifies family edges + connected nodes, dims everything else
- [ ] Strata labels show project code and instance path
- [ ] All Cube interactions work (orbit, fly-to, search, LOD)
- [ ] Cleanup on tab switch (generation-based cancellation)
- [ ] Graceful state when no child instances exist (renders single-stratum = identical to Cube)
- [ ] Tests for recursive endpoint (multi-instance, cross-instance links, schema mismatch)

### Should have (v1 stretch)

- [ ] Plane click focuses camera on that stratum
- [ ] Family legend overlay (shows parent → family color mapping)
- [ ] Keyboard shortcut to cycle through strata (focus each plane in sequence)
- [ ] Smooth animated transitions on data update

### Nice to have (v2)

- [ ] Explicit instance registry
- [ ] Stable instance-ID references
- [ ] Upward resolution from sub-projects
- [ ] `cube-lib.js` shared primitives
- [ ] Cross-instance dependency visualization
- [ ] Strata collapse/expand
- [ ] Filter by stratum depth

---

## Appendix A: Relationship Types (fixed set)

From `core/relationships.py`:

```
blocks        — "I block [target] from proceeding"
depends_on    — "I depend on [target] completing"
subtask_of    — "I am a subtask of [target]"          ← strata hierarchy
related_to    — "I am related to [target]"
spawned_by    — "I was spawned by [target]"            ← strata hierarchy
duplicate_of  — "I am a duplicate of [target]"
supersedes    — "I supersede [target]"
```

Strata view uses `subtask_of` + `spawned_by` for cross-plane edges. All types support `@path` qualified targets in the data model.

## Appendix B: Cube3d.js Architecture Reference

The Strata view follows these patterns from cube3d.js (1605 lines):

| Pattern | How cube3d.js does it | Strata replicates? |
|---------|----------------------|-------------------|
| Singleton state bag (`_cube3d`) | ~30 fields: scene, camera, renderers, simulation, meshes, LOD state | Yes, as `_strata` |
| 3 public globals | `renderCube3D`, `updateCube3DData`, `cleanupCube3D` | Yes |
| Generation-based cancellation | `_cube3d.generation++` on cleanup; async ops check generation | Yes |
| d3-force-3d simulation | forceX (status), forceY (priority), forceZ (recency) | Yes, modified Y |
| Instanced mesh rendering | `InstancedMesh` with per-frame matrix/color updates | Yes |
| Line + flow-particle edges | `BufferGeometry` lines + `Points` with animated `t` | Yes, + family colors |
| 5-level LOD | Distance-based: sphere → label → card → panel | Yes, unchanged |
| CSS3D billboarded cards | HTML divs positioned in 3D, face camera each frame | Yes |
| Custom orbit controls | Inlined OrbitControls (rotate, pan, zoom, damping) | Yes, copied |
| Raycaster → fly-to-node | Click intersects InstancedMesh, smooth camera flight | Yes |

## Appendix C: Design Review Log

Decisions made during the design grill session (2026-03-09):

1. **Strata = instances, not edge-depth.** Bands represent filesystem-discovered `.lattice/` instances, not BFS depth from `subtask_of` edges.
2. **3D, not SVG.** Extends the existing Three.js Cube architecture. The G4a mockup depicts 3D planes.
3. **Separate tab, not replacement.** "Strata" alongside "Cube."
4. **Children store upward references.** 1:N parent→children pattern: each child stores `subtask_of target@..` pointing to its parent.
5. **Filesystem discovery for v1.** Explicit registry is v2.
6. **Relative-path qualified IDs for v1.** Stable instance IDs are v2.
7. **Priority as secondary Y within strata.** High floats up, low sinks down, contained within the band.
8. **Family-colored cross-plane edges.** Curated d3 category10 palette per parent. Always visible at low opacity, intensified on hover. Both nodes and edges highlight on hover.
9. **`@path` syntax for all relation types.** Data model is general. Strata view only renders `subtask_of` + `spawned_by` across planes.
10. **Pattern reuse, not code reuse.** `strata.js` follows cube3d.js architecture but is an independent file. Shared library extraction is v2.
11. **Depth bound = 3.** Discovery maxdepth and max strata.
12. **Cross-instance `depends_on`/`blocks` not rendered.** Intra-instance only in this view for v1.
