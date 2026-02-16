# Triage Stale Tasks

You are working on the Lattice project at `/Users/atin/Projects/Stage 11 Agentics/PROJECTS/Lattice`. Your job is to triage three stale tasks and clean up the pipeline.

## Context

Lattice dogfoods itself — the `.lattice/` directory tracks development tasks for the Lattice project. Several tasks are in stale states that need resolution.

---

## LAT-46 — "Rewrite Philosophy.md as v2"

**Current status:** `review`
**What happened:** Philosophy v2 was written and committed (`28f794a`). Then v3 was written — a tighter, thesis-first rewrite — committed as two separate commits (`8c4569b`, `b7a3066`). The git log shows v3 is the latest:
- `b7a3066 docs: tighten Philosophy v3 — less philosophical, more clear`
- `8c4569b docs: add Philosophy v3 — hypercrafted, half the length, thesis-first`

**Action:** This work is complete. Philosophy has been rewritten and iterated to v3. Mark as done:
```
uv run lattice status LAT-46 done --actor agent:claude-opus-4 --reason "v2 written and iterated to v3, both committed"
```

Also add a comment recording the final state:
```
uv run lattice comment LAT-46 "Completed. Philosophy v2 committed (28f794a), then iterated to v3 (8c4569b, b7a3066). v3 is the current version — thesis-first structure, half the length of v2." --actor agent:claude-opus-4
```

---

## LAT-42 — "Add Cube as top-level 3D spatial task visualization in dashboard"

**Current status:** `in_progress`
**What happened:** The Cube v1 was implemented as a 2D force-directed graph visualization using d3. That work was tracked under LAT-44 ("Implement Cube view (2D-first graph visualization) addressing all review findings") which is already `done`.

Meanwhile, LAT-48 was created as a `critical` priority task: "Review cube-vision.md and implement the Cube 3D workspace" — this is the full Three.js 3D vision described in `notes/cube-vision.md`, which is a major evolution beyond what LAT-42 originally scoped.

**Action:** LAT-42's original scope (add Cube as a view in the dashboard) was fulfilled by LAT-44. The remaining ambition (full 3D workspace) is now captured by LAT-48. Mark LAT-42 as done:
```
uv run lattice status LAT-42 done --actor agent:claude-opus-4 --reason "Original scope (2D Cube view) completed via LAT-44. Full 3D vision tracked as LAT-48."
```

Add a comment:
```
uv run lattice comment LAT-42 "Closing — the 2D Cube view was implemented under LAT-44 (done). The evolved 3D workspace vision is now tracked as LAT-48 (critical, backlog)." --actor agent:claude-opus-4
```

---

## LAT-41 — "Design and build Lattice onboarding flow + Bitcoin Pricer demo"

**Current status:** `in_progress`
**What happened:** Task was created and immediately moved to `in_progress`, but the notes file is empty (just template placeholders). No commits reference this work. It appears to have been created during a planning session but never started.

**Action:** This task was never actually worked on. Move it back to `backlog` so it's visible as unstarted work:
```
uv run lattice status LAT-41 backlog --actor agent:claude-opus-4 --reason "No work started — notes empty, no commits. Returning to backlog for future prioritization."
```

Add a comment:
```
uv run lattice comment LAT-41 "Returning to backlog. Task was moved to in_progress but no work was done — notes template is empty, no associated commits. Needs scoping before pickup." --actor agent:claude-opus-4
```

---

## After Triage

Run `uv run lattice list` and confirm the pipeline looks clean:
- No `in_progress` items without active work
- No `review` items that are actually done
- Backlog accurately reflects unstarted work

Report what you found.
