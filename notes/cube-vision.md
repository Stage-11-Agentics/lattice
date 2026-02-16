# The Cube: A Spatial Workspace You Inhabit

## The Core Idea

The Cube is not a graph you look at. It is a **workspace you move through.**

Every other view in Lattice (Board, List, Activity) is a 2D projection — a flattened report of the work. The Cube is the work itself, arranged in space. You orbit it to see the shape of your project. You fly into a cluster to understand a group of related tasks. You land on a single task and it opens up around you — description, timeline, comments, connections — and you work on it right there, without ever leaving the space.

The fundamental interaction is **physical navigation as abstraction control.** Distance from a task determines how much you know about it. Far away, it's a point of light. Close up, it's a full workspace. The scroll wheel isn't zooming a chart — it's moving you through a space where proximity equals detail.

---

## The Three Axes (Why 3D Earns Its Keep)

3D visualization is usually a mistake — it adds visual complexity without adding meaning. The Cube earns the third dimension because each axis encodes something real:

**X-axis → Workflow progression.** Left to right: `backlog → in_planning → planned → in_progress → review → done`. This is the river. Tasks flow from left to right over their lifetime. Status zones are soft vertical planes — translucent walls of tinted fog that give the space structure without rigidity.

**Y-axis → Priority / Importance.** Urgent and high-priority tasks float high. Low-priority tasks sink. Your eye naturally goes up, so the things that need attention are where you look first.

**Z-axis → Recency of activity.** Recently touched tasks are closer to the camera's default position. Stale tasks recede into the background. This means when you open the Cube, the first things you see are the things that are actively being worked on — no hunting required.

**Within these soft constraints, force-directed physics handles the rest.** Connected tasks pull toward each other. Clusters form organically around shared relationships. The axes provide structure; the forces provide meaning.

---

## Semantic Zoom: Four Continuous Levels

The transition between these levels is **continuous, not discrete.** As you scroll forward, elements smoothly morph — a sphere gains a label, gains a border, stretches into a card, fills with content. There are no mode switches. Just proximity.

### Level 1 — Constellation (Full Project Overview)

**Camera position:** Far back. The entire project is visible.

**What you see:**
- Each task is a **luminous sphere**. Color = status. Size = priority.
- Edges are thin filaments of light connecting related tasks — fiber-optic threads that pulse faintly when activity occurs.
- Status zones are soft vertical fog planes with labels at the top: "Backlog", "In Progress", "Done", etc.
- Connected components have a subtle **nebula glow** behind them — a soft cloud that makes clusters visible as clusters, not just coincidental proximity.
- Orphan nodes (no relationships) are gathered in a gentle orbit at the periphery, clearly separated from the relational core.

**What you learn at this distance:**
- The shape of the project. Is it mostly done (right-heavy)? Mostly planned (left-heavy)?  Blocked (dense cluster with red edges)?
- Where the energy is. Recently active nodes glow brighter.
- Whether the work is connected or fragmented.

**Interactions available:**
- Orbit (drag to rotate the space)
- Click a cluster to fly toward it
- Search bar: type a query, matching nodes pulse and a guide-line draws from the camera

---

### Level 2 — Star Map (Regional View, ~15-30 tasks visible)

**Camera position:** Mid-distance. You're looking at a neighborhood of tasks.

**What you see:**
- Spheres now have **labels**: short ID on top, truncated title (first ~40 chars) below.
- Edge directionality becomes visible — small animated dots flowing along edges from source → target (like data flowing through fiber optics). Color distinguishes type: red for `blocks`, orange for `depends_on`, blue for `subtask_of`, gray for `related_to`.
- A small **legend** fades in at the bottom corner, explaining edge types.
- Nodes gain a subtle **status ring** — a thin colored halo that makes status readable even when the sphere fill color is similar.
- Priority is now communicated through a **vertical glow bar** on the node (tall = urgent, short = low).

**What you learn at this distance:**
- Which specific tasks are connected and how.
- The dependency chain. Follow the red threads to see what's blocking what.
- Task titles give enough context to understand the work without diving in.

**Interactions available:**
- Everything from Level 1, plus:
- **Hover** a node: tooltip shows full title, status, assignee, priority, created date.
- **Click** a node: camera smoothly flies to it (transitions to Level 3 focused on that node).
- **Right-click** a node: context menu — Change Status, Assign, Set Priority, Add Comment.
- **Shift+drag** between two nodes: draw a relationship edge (choose type from popup).

---

### Level 3 — Card View (Focused Cluster, ~3-8 tasks visible)

**Camera position:** Close. You're hovering in front of a small group of tasks.

**What you see:**
- The focal node's sphere **dissolves and unfolds into a floating card.** This is the signature transition of the Cube — the sphere stretches, flattens, and becomes a rectangular card with:
  - **Header:** Short ID + full title, editable on click
  - **Status badge:** Colored pill, click to cycle or drag to a status zone
  - **Priority indicator:** Vertical colored bar on the left edge
  - **Assignee:** Avatar or initials, click to reassign
  - **Description snippet:** First 3-4 lines, with "expand" affordance
  - **Relationship count badges:** "3 blocked by" / "2 blocks" — click to highlight those edges
- Neighboring connected tasks are **slightly expanded** — showing ID + title as mini-cards — so you can see the context without losing focus.
- Distant unrelated tasks fade to dim dots (depth-of-field blur effect).
- Edges from the focal task are **emphasized** — brighter, thicker, with animated flow. Other edges dim.

**What you learn at this distance:**
- The full context of a task: what it is, who owns it, what it's connected to.
- Enough to make decisions: should I start this? Is it blocked? Who should I talk to?

**Interactions available:**
- Everything from Level 2, plus:
- **Click any field on the card** to edit it inline (title, description, assignee, priority).
- **Drag the card toward a status zone** to change its status (the card flies to the new column position with a satisfying arc animation).
- **Click a relationship badge** to highlight the connected tasks and their edges — the camera adjusts to show the full relationship chain.
- **Tab/arrow keys** to navigate between connected tasks without using the mouse — the camera follows.
- **Press `N`** near a card to create a new task linked to it.

---

### Level 4 — Immersive Task (Single Task Workspace)

**Camera position:** You've landed. One task fills your view.

**What you see:**
- The card has expanded into a **full workspace panel**, centered in the 3D space:
  - **Left column:** Full description (markdown rendered), editable. Artifact links.
  - **Right column:** Event timeline — every status change, comment, assignment, relationship modification. Scrollable. Each event shows actor, timestamp, and content.
  - **Bottom:** Comment input field. Type and press Enter to add a comment directly.
- **Satellite cards** orbit gently around the main panel — one for each directly related task, showing ID + title + status. Click any satellite to fly to it (the current task becomes a satellite of the new focus).
- **Relationship threads** extend from the main panel to its satellites — you can see and understand the entire immediate neighborhood while focused on one task.
- The background darkens further. The rest of the project is a distant, dimmed constellation behind the workspace. You are "inside" this task.

**What you learn at this distance:**
- Everything. The full story of this task from creation to now.
- The full text, all comments, all decisions, all connected work.

**Interactions available:**
- Full inline editing of every field (title, description, status, priority, assignee).
- Add/remove relationships by dragging satellites or pressing a hotkey.
- Write and post comments.
- View and download artifacts.
- **Escape** or scroll back to rise up to Level 3, then Level 2.
- **Keyboard shortcut `]`** to move to the next task in a dependency chain (follow the `blocks` edge forward). **`[`** to go backward. This lets you "walk" a dependency chain.

---

## Navigation Model

| Input | Action |
|-------|--------|
| **Scroll wheel** | Fly forward/backward (semantic zoom) |
| **Left-drag on empty space** | Orbit / rotate the 3D view |
| **Right-drag on empty space** | Pan the view |
| **Left-click node** | Fly to that node (smooth camera transition) |
| **Right-click node** | Context menu (status, assign, priority, comment, delete) |
| **Shift+drag node→node** | Create relationship edge |
| **Double-click empty space** | Create new task at that spatial position |
| **`/` or `Cmd+K`** | Search — type to find, matching nodes pulse |
| **`Escape`** | Rise one zoom level / deselect |
| **`Tab`** | Cycle focus between connected tasks |
| **`[` / `]`** | Walk dependency chain backward/forward |
| **`1` / `2` / `3` / `4`** | Jump to zoom level (constellation / star map / card / immersive) |
| **`F`** | Fit all nodes in view (reset camera) |
| **`N`** | New task (context-linked if near a focused task) |
| **`G`** | Toggle grid/axis guides |

---

## Visual Design Language

### The Aesthetic: Cognitive Space

Not "sci-fi dashboard." Not "gaming UI." The visual language should feel like a **calm, structured thinking environment** — a private library in space. Dark, focused, with light used sparingly and meaningfully.

**Background:** Near-black with an extremely subtle radial gradient (slightly lighter at center). No star fields, no particles — those distract. The background exists to make the data glow.

**Status zone fog:** Extremely faint vertical color washes. Just enough to give the space structure. Think morning fog between buildings, not laser walls. These should be invisible until you look for them.

**Nodes:** Solid fills with soft edges (slight gaussian blur on the sphere boundary). No hard outlines. The node IS its color — clean and confident.

**Edges:** Thin, semi-transparent. Only the focused node's edges are bright. Everything else is a whisper. Animated flow particles (tiny dots moving along the edge) replace arrowheads — they show direction through motion, which is more legible than static arrows in 3D.

**Cards (Level 3-4):** Frosted glass panels. Semi-transparent background with backdrop blur. Crisp text (HTML/CSS rendered via CSS3DRenderer, not textures). Thin luminous border matching the status color. These should feel like floating screens in space — tangible and interactive.

**Transitions:** Every state change is animated. Nodes don't teleport; they arc. Cards don't appear; they unfold. The camera doesn't jump; it flies. Easing: ease-in-out-cubic. Duration: 400-600ms for camera moves, 200-300ms for element transitions. Fast enough to feel responsive, slow enough to maintain spatial awareness.

**Depth of field:** When focused on a task (Level 3-4), distant elements get a subtle blur. This isn't just aesthetic — it communicates "these things are far away and not your concern right now."

### Color Mapping

| Status | Node Color | Zone Fog |
|--------|-----------|----------|
| backlog | `#6b7280` (muted gray) | barely visible gray wash |
| in_planning | `#a78bfa` (soft violet) | faint violet wash |
| planned | `#60a5fa` (sky blue) | faint blue wash |
| in_progress | `#34d399` (emerald) | faint green wash |
| review | `#fbbf24` (amber) | faint gold wash |
| done | `#22d3ee` (cyan) | faint cyan wash |
| blocked | `#f87171` (red) | pulsing faint red wash |
| cancelled | `#374151` (dark gray) | none |

### Edge Color

| Relationship | Color | Line Style |
|-------------|-------|-----------|
| blocks | `#ef4444` (red) | solid, 2px |
| depends_on | `#f97316` (orange) | solid, 1.5px |
| subtask_of | `#3b82f6` (blue) | solid, 1.5px |
| related_to | `#6b7280` (gray) | dashed, 1px |
| spawned_by | `#8b5cf6` (purple) | dotted, 1px |

---

## Technical Architecture

### Rendering Stack

```
┌─────────────────────────────────────┐
│         HTML/CSS Layer              │  ← Task cards, forms, tooltips
│     (CSS3DRenderer overlay)         │     Crisp text, real inputs
├─────────────────────────────────────┤
│         Three.js Scene              │  ← 3D space, camera, lighting
│     (WebGLRenderer base)            │     Nodes, edges, fog zones
├─────────────────────────────────────┤
│      d3-force-3d Simulation         │  ← Physics / layout engine
│     (runs in Web Worker)            │     Position calculation
├─────────────────────────────────────┤
│         Lattice /api/graph          │  ← Data source
│     (ETag-cached, polled)           │     Nodes + edges + metadata
└─────────────────────────────────────┘
```

**Why this stack:**

- **Three.js (WebGL):** Hardware-accelerated 3D. Handles hundreds of nodes at 60fps. Provides camera controls, raycasting for click detection, fog, depth-of-field post-processing.
- **CSS3DRenderer (overlay):** Renders real HTML elements positioned in 3D space. This is critical — text on cards is actual DOM text, not canvas-drawn or texture-mapped. It's crisp at every zoom level, selectable, and supports real form inputs for inline editing.
- **d3-force-3d (Web Worker):** Force simulation runs off the main thread. The UI never stutters during layout calculation. Simulation parameters (charge, axis forces) can be tuned without blocking rendering.
- **Lattice /api/graph:** Already exists. Extend with additional fields (description snippet, comment count, last activity timestamp) to support card and immersive views. Add `/api/tasks/{id}/detail` endpoint for full task data when entering Level 4.

### Level-of-Detail (LOD) System

The LOD system is what makes semantic zoom work technically:

```
Camera distance to node → LOD level → Render mode

> 800 units:   LOD 0 — InstancedMesh sphere (batched, ultra-cheap)
300-800:       LOD 1 — Individual sphere + text sprite (ID label)
80-300:        LOD 2 — Sphere + text sprite (ID + title) + status ring
20-80:         LOD 3 — CSS3D card (HTML panel replacing sphere)
< 20:          LOD 4 — CSS3D full workspace (expanded panel)
```

**Transition between LOD levels is animated.** When a node crosses a threshold, it doesn't snap — it morphs over 200ms. Sphere → card transition: sphere flattens along Z, expands in X/Y, opacity fades as the CSS3D card fades in at the same position.

**Performance budget:**
- LOD 0 nodes: batched into a single `InstancedMesh` draw call. 1000 nodes = 1 draw call.
- LOD 1-2 nodes: individual meshes + sprite labels. Target: <50 at a time (frustum culled).
- LOD 3-4 nodes: CSS3D panels. Target: <8 at a time (only nearby nodes get cards).

This means the Cube can handle **hundreds of tasks** while keeping the visible detail-rich nodes to a small, performant set.

### Data Loading Strategy

**Level 1-2 data** (ID, title, status, priority, assignee, relationships): Loaded via `/api/graph` on initial render. This is all that's needed for constellation and star map views. Cached, ETag-polled.

**Level 3 data** (description snippet, comment count, last activity): Loaded lazily when a node enters LOD 3 range. Small JSON fetch per task. Cached in memory.

**Level 4 data** (full description, all events, all comments, artifacts): Loaded on demand when the user enters immersive view. Single fetch to `/api/tasks/{id}/events` (or new `/api/tasks/{id}/full` endpoint). Cached for the session.

This means the Cube loads fast (Level 1-2 data is what the current view already fetches) and only loads heavy data when the user explicitly navigates close enough to need it.

---

## The Workspace Actions (Working Inside the Cube)

This is what transforms the Cube from a visualization into a primary interface.

### Status Changes

**Method 1 — Context menu:** Right-click → Change Status → select from dropdown.

**Method 2 — Spatial drag:** In card view (Level 3), grab a card and drag it laterally. As it crosses status zone boundaries, the zone it enters highlights. Release to change status. The card then settles into its new position through the force simulation — it finds its place among its new neighbors.

**Method 3 — Keyboard:** Focus a task with Tab, press `S`, type the first few letters of the target status, Enter. Fast for power users.

### Creating Tasks

**Method 1 — Keyboard:** Press `N` anywhere. A new task form appears as a floating card at the camera's focal point. Fill in title (required), optionally status and priority, Enter to create. The new node materializes as a sphere that drops into position via the force simulation.

**Method 2 — Context-linked:** When focused on a task (Level 3-4), press `N`. The new task is pre-linked as `related_to` the focused task and spawns near it in space.

### Editing

In card view (Level 3) or immersive view (Level 4), click any field to edit:
- **Title:** Click → inline text input → Enter to save, Escape to cancel.
- **Description:** Click → textarea expands → Markdown preview toggle → Save button.
- **Priority:** Click badge → dropdown.
- **Assignee:** Click → actor picker (searchable list from known actors).
- **Status:** Click badge → dropdown, or drag method.

All edits call the existing Lattice CLI commands via API (`POST /api/tasks/{id}/update` — new endpoint wrapping `lattice status`, `lattice assign`, etc.).

### Relationship Management

**Create:** Shift+drag from one node to another. A luminous thread follows your cursor. On release, a small popup asks: "blocks / depends_on / related_to / subtask_of?" Select one. Edge materializes with the appropriate color.

**Delete:** In card view, hover a relationship badge → click the `×` → confirm.

### Comments

In immersive view (Level 4), the comment input is always visible at the bottom of the timeline. Type, Enter, done. The new comment appears in the timeline with a subtle slide-in animation. Actor is auto-attributed.

---

## What This Replaces

When the Cube reaches full maturity, it doesn't just replace the current graph view. It becomes an **alternative to every other view**:

| Traditional View | Cube Equivalent |
|-----------------|----------------|
| **Board** (kanban columns) | Level 2 with strong X-axis status forces — you see the same column layout but with relationship threads visible |
| **List** (flat table) | Search + Level 2 — filter to a set, see them with spatial context |
| **Activity** (event timeline) | Level 4 on any task — full event history inline |
| **Task detail page** | Level 4 — the task IS the page, surrounded by its context |

You don't need to leave the Cube to do anything. The Cube is the entire application, viewed from different distances.

---

## Implementation Phasing

### Phase 1 — The Space (foundation)
- Three.js scene with WebGL renderer
- d3-force-3d simulation with status/priority/recency axis forces
- InstancedMesh for LOD 0 nodes
- Basic orbit/zoom camera controls
- Data from existing `/api/graph` endpoint
- Status zone fog planes
- Edge rendering with animated flow particles
- **Result:** A real 3D graph that looks and moves beautifully, with meaningful spatial organization

### Phase 2 — Semantic Zoom (the breakthrough)
- LOD system with distance-based level transitions
- CSS3DRenderer overlay for LOD 3-4 cards
- Smooth sphere → card morphing animation
- Click-to-fly camera navigation
- Depth-of-field post-processing
- Search with visual guide-lines
- **Result:** The "scroll in and dots become stories" experience

### Phase 3 — The Workspace (working inside)
- Inline editing on cards (title, status, priority, assignee)
- Context menus on right-click
- Spatial drag for status changes
- New task creation (`N` key)
- Shift+drag relationship creation
- Comment input in immersive view
- API endpoints for write operations
- **Result:** You can do everything from inside the Cube

### Phase 4 — Polish (the feeling)
- Camera easing and motion profiles
- Transition animations (unfold, arc, settle)
- Keyboard navigation (Tab, `[`, `]`, arrow keys)
- Minimap in corner (shows full graph + viewport rectangle)
- Filter panel (toggle statuses, relationship types, actors)
- Remember camera position across sessions (localStorage)
- Performance profiling and optimization for 200+ task graphs
- **Result:** It feels inevitable — like this is how task management was always supposed to work
