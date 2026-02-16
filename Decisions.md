# Lattice Decisions

> A small log of non-obvious choices so we do not relitigate them later.
> Date format: YYYY-MM-DD.

---

## 2026-02-15: Events are authoritative

- Decision: The per-task JSONL event log is the source of truth. Task JSON files are materialized snapshots.
- Rationale: Makes crash recovery and integrity checks straightforward; avoids “which file do we believe?” ambiguity.
- Consequence: We must ship `lattice rebuild` (replay events) and `lattice doctor` (integrity checks).

---

## 2026-02-15: Avoid duplicated canonical edges

- Decision: We do not store bidirectional relationship edges as canonical state.
- Rationale: Bidirectional storage forces multi-file transactional updates and creates split-brain inconsistencies under concurrency.
- Consequence: Reverse lookups are derived by scanning snapshots in v0 and via an index in v1+.

---

## 2026-02-15: Artifacts are not archived in v0

- Decision: Archiving moves tasks/events/notes only. Artifacts stay in place.
- Rationale: Artifacts can relate to many tasks; moving them introduces complex relocation rules and broken references.
- Consequence: Archived tasks remain able to reference artifacts by ID.

---

## 2026-02-15: Idempotency via caller-supplied IDs

- Decision: CLI supports `--id` for task/artifact/event creation so agents can safely retry operations.
- Rationale: ULIDs generated at write-time are not deterministic across retries.
- Consequence: Agents should generate IDs once and reuse them; CLI treats existing IDs as upserts.

---

## 2026-02-15: Lock + atomic write is required, even for JSONL

- Decision: All writes (snapshot rewrites and JSONL appends) are lock-protected and atomic.
- Rationale: Concurrent appends can interleave or partially write without explicit locking guarantees.
- Consequence: `.lattice/locks/` exists and multi-lock operations acquire locks in deterministic order.

---

## 2026-02-15: Dashboard served by CLI, read-only

- Decision: `lattice dashboard` runs a small local read-only server rather than a standalone static HTML that reads the filesystem directly.
- Rationale: Browsers cannot reliably read arbitrary local directories without a server or user-driven file picker flows.
- Consequence: Still no database, still no write path, still offline-friendly.

---

## 2026-02-15: Git integration is minimal in v0

- Decision: v0 only records commit references to task IDs from commit messages and logs `git_event`.
- Rationale: Diff scanning and cross-platform hook behavior can be fragile and distract from core correctness.
- Consequence: Richer `files_touched` and PR integration are v1+ only.

---

## 2026-02-15: OTel fields are passthrough metadata in v0

- Decision: Events include optional `otel` fields, but no strict tracing guarantees or exporters in v0.
- Rationale: Keeping the schema ready is cheap; enforcing full tracing discipline is expensive.
- Consequence: Adoption can ramp gradually without schema changes.

---

## 2026-02-15: Python with Click for CLI implementation

- Decision: Lattice CLI is implemented in Python 3.12+ using Click. pytest for testing. ruff for linting.
- Rationale: Fastest development velocity, agents are extremely fluent in Python, and `uv` has made Python distribution practical. Click is mature, well-documented, and agents know it well.
- Consequence: Accept ~200-500ms startup latency per invocation in v0. On-disk format is the stable contract — CLI can be rewritten in a faster language later if needed without breaking anything.

---

## 2026-02-15: Free-form actor IDs with convention, no registry

- Decision: Actor IDs are free-form strings with `prefix:identifier` format (e.g., `agent:claude-opus`, `human:atin`). No registry or validation beyond format.
- Rationale: An agent registry adds complexity with no v0 payoff. Attribution is a social/process concern, not a data integrity one.
- Consequence: Config may optionally list `known_actors` for display names, but it's not required or enforced.

---

## 2026-02-15: No dedicated notes CLI command

- Decision: Notes are directly-editable markdown files at `notes/<task_id>.md`. No `lattice note` command.
- Rationale: Agents use file tools; humans use editors. A CLI command adds ceremony without value. `lattice show` displays the note path.
- Consequence: `lattice init` creates the `notes/` directory. File creation is manual or incidental.

---

## 2026-02-15: No unarchive in v0

- Decision: `lattice archive` is one-way. No `lattice unarchive` command.
- Rationale: Archive mirrors active structure, so manual recovery (move files back) is trivial. Adding a command means testing the reverse path and edge cases around stale relationships.
- Consequence: Document manual recovery procedure. Add `unarchive` later if real pain shows up.

---

## 2026-02-15: Standard Python package for distribution

- Decision: Lattice is a standard Python package (pyproject.toml, src layout). Primary install via `uv tool install` or `pipx`. Zipapp as a bonus portability option.
- Rationale: Standard packaging supports all distribution methods without choosing exclusively. `uv` gives near-single-command install.
- Consequence: Must maintain pyproject.toml and src layout conventions.

---

## 2026-02-15: Global event log is derived, not authoritative

- Decision: `_lifecycle.jsonl` is a derived convenience index, rebuildable from per-task event logs. Per-task JSONL files are the sole authoritative record.
- Rationale: Two authoritative logs (per-task + global) creates the exact "which file do we believe?" ambiguity that event sourcing was designed to prevent.
- Consequence: `lattice rebuild` regenerates `_lifecycle.jsonl`. If the lifecycle log and per-task logs disagree, per-task logs win.

---

## 2026-02-15: Idempotency rejects conflicting payloads

- Decision: Same ID + same payload = idempotent success. Same ID + different payload = conflict error.
- Rationale: Silent upsert hides agent bugs. An agent retrying with different data likely has a logic error that should surface immediately.
- Consequence: CLI must compare incoming payload against existing entity when a duplicate ID is detected.

---

## 2026-02-15: Write ordering is event-first

- Decision: All mutations append the event before materializing the snapshot.
- Rationale: If a crash occurs between event-write and snapshot-write, `rebuild` recovers the snapshot from events. The reverse (snapshot-first) would leave orphaned state with no event record.
- Consequence: Crash semantics are well-defined: events are always at least as current as snapshots.

---

## 2026-02-15: Custom event types require x_ prefix

- Decision: `lattice event` only accepts event types prefixed with `x_` (e.g., `x_deployment_started`). Built-in type names are reserved.
- Rationale: Unbounded custom event writes would undermine schema integrity and complicate rebuild logic.
- Consequence: Built-in event types form a closed enum. Extensions use a clear namespace.

---

## 2026-02-15: Root discovery walks up from cwd

- Decision: The CLI finds `.lattice/` by walking up from the current working directory, with `LATTICE_ROOT` env var as override.
- Rationale: Mirrors `git`'s well-understood discovery model. Works naturally in monorepos and nested project structures.
- Consequence: Commands other than `lattice init` error clearly if no `.lattice/` is found.

---

## 2026-02-15: All timestamps are RFC 3339 UTC

- Decision: All timestamp fields use RFC 3339 UTC with `Z` suffix (e.g., `2026-02-15T03:45:00Z`).
- Rationale: Eliminates timezone ambiguity across agents running in different environments. RFC 3339 is a strict profile of ISO 8601.
- Consequence: No local time handling. All comparisons are UTC. ULIDs provide time-ordering; timestamps are for human readability and correlation.

---

## 2026-02-15: No config mutation events in v0

- Decision: Config changes are manual edits to `config.json`. No `lattice config` command and no `config_changed` event type in v0.
- Rationale: Config changes are rare and high-stakes. Manual editing with git tracking provides adequate auditability without additional machinery.
- Consequence: Add `lattice config set` and corresponding events in v1+ if automated config management becomes needed.

---

## 2026-02-15: Removed decisions.md from .lattice/ directory

- Decision: The `.lattice/` directory no longer includes a `decisions.md` file.
- Rationale: `.lattice/` should only contain machine-managed data. Project-level decision logs belong wherever the project keeps its documentation, not inside the Lattice runtime directory.
- Consequence: One less file to confuse with the repo-level `Decisions.md` used during Lattice development.

---

## 2026-02-15: Renamed `_global.jsonl` to `_lifecycle.jsonl`

- Decision: The derived convenience event log is now named `_lifecycle.jsonl` instead of `_global.jsonl`.
- Rationale: The log only contains lifecycle events (task_created, task_archived, task_unarchived), not "global" events. The old name implied it contained all events, which was confusing.
- Consequence: All code, tests, lock keys, and documentation updated. Variable names use `lifecycle_` prefix instead of `global_`.

---

## 2026-02-15: Renamed `lattice log` to `lattice event`

- Decision: The custom event command is now `lattice event` instead of `lattice log`.
- Rationale: `lattice log` collided with the mental model of "viewing a log" (like `git log`). The command actually records a custom event, so `event` is more descriptive.
- Consequence: ProjectRequirements_v1.md still references `lattice log` in section 13.1 — it should be updated to match.

---

## 2026-02-15: Added `lattice unarchive` (reverses earlier decision)

- Decision: `lattice unarchive` is now implemented, reversing the "No unarchive in v0" decision.
- Rationale: The implementation was straightforward (reverse the archive file moves, append a `task_unarchived` event), and the lack of unarchive was flagged during review as a usability gap. Manual file recovery is error-prone.
- Consequence: `task_unarchived` added to BUILTIN_EVENT_TYPES and LIFECYCLE_EVENT_TYPES. Archive round-trips are now fully supported.

---

## 2026-02-15: Bidirectional relationship display in `lattice show`

- Decision: `lattice show` displays both outgoing relationships (from the task's snapshot) and incoming relationships (derived by scanning all snapshots).
- Rationale: The original simplification of outgoing-only display was flagged as an oversimplification during review. Users expect to see "task B is blocked by task A" when viewing task B.
- Consequence: Canonical storage remains outgoing-only (no schema change). Incoming relationships are computed at read time by scanning `tasks/` and `archive/tasks/`. Performance is acceptable at v0 scale.

---

## 2026-02-15: Audit remediation — security, durability, and architecture

- Decision: Implemented 9 fixes from a project-wide audit covering security (path-traversal validation, POST body size limit, readonly mode for non-loopback dashboard), durability (parent-directory fsync after atomic writes), architecture (unified write-path in `lattice.storage.operations`, mutation registry pattern in `tasks.py`), and docs (requirements drift fixes, `_global.jsonl` → `_lifecycle.jsonl` alignment).
- Rationale: The audit identified gaps in input validation (path traversal via crafted task IDs), denial-of-service surface (unbounded POST bodies), data durability (missing dir fsync after rename), network safety (writes allowed on non-loopback interfaces), code duplication (CLI and dashboard had separate write paths), and documentation drift.
- Consequence: CLI and dashboard share a single `write_task_event()` in `lattice.storage.operations`. Dashboard supports `--force` status transitions matching CLI behavior. Non-loopback dashboard binds are read-only by default. All task ID inputs are validated before filesystem operations.

---

## 2026-02-15: Human-friendly short IDs (ULIDs remain canonical)

- Decision: ULIDs (`task_01...`) remain the internal primary key for filenames, events, locks, and relationships. Short IDs (`LAT-42`) are a human-facing alias layer resolved at the CLI boundary.
- Rationale: Changing the canonical ID would require rewriting every event, snapshot, lock, and filename. The alias approach is additive and non-breaking.
- Consequence: All CLI commands accept both ULID and short ID inputs. Short IDs are resolved to ULIDs before any operation.

---

## 2026-02-15: Short ID stored in snapshot and events; index file is derived

- Decision: `short_id` is a first-class field on task snapshots. Included in `task_created` event data. A dedicated `task_short_id_assigned` event handles retroactive assignment. `.lattice/ids.json` is a derived index (rebuildable from events).
- Rationale: Events are authoritative. The index is a read optimization, not a source of truth.
- Consequence: `lattice rebuild --all` regenerates `ids.json`. `lattice doctor` checks alias integrity.

---

## 2026-02-15: Project code in config.json; counter in ids.json

- Decision: `project_code` (1-5 uppercase ASCII letters) stored in `.lattice/config.json`. `next_seq` counter stored in `.lattice/ids.json` alongside the mapping.
- Rationale: Config is the natural place for project-level settings. The counter lives with the mapping it governs.
- Consequence: `lattice init --project-code`, `lattice set-project-code`, and `lattice backfill-ids` manage project code lifecycle.

---

## 2026-02-15: Existing short IDs are immutable

- Decision: Once assigned, a task's short ID never changes — even if the project code is changed later.
- Rationale: References to `LAT-7` in comments, docs, and conversations must remain stable forever.
- Consequence: Changing project code only affects future task creation.

---

## 2026-02-15: Dedicated event type for retroactive short ID assignment

- Decision: `task_short_id_assigned` (not `field_updated`) for migration of existing tasks.
- Rationale: Using `field_updated` would conflict with `short_id` being a protected field. A dedicated event type makes replay semantics explicit.
- Consequence: `short_id` is in PROTECTED_FIELDS and cannot be changed via `lattice update`.

---

## 2026-02-15: Open question — Is "task" the right primitive?

- Status: **Open question**, not a decision. Noted for future exploration.
- Observation: Tasks are the inherited metaphor from project management, but agents may coordinate more naturally around other primitives — goals, invariants, contracts, capabilities. For v0, a "task" can represent any of these. The abstraction may evolve in later versions.
- Consequence: No action now. Revisit when real usage patterns emerge that strain the task metaphor.

---

## 2026-02-15: Fractal instance hierarchy

- Decision: Lattice supports multiple independent instances at different scopes (program → workspace → repo) that form a loose hierarchy. Each instance is self-contained with identical on-disk format and CLI. Coordination between levels is agent-mediated, not system-mediated.
- Rationale: File-based is load-bearing — agents are excellent at filesystem interaction. Multi-machine, multi-project coordination works through git as the sync layer (event-sourced architecture makes merges tractable: events accumulate, snapshots are rebuilt). Keeping instances independent avoids distributed systems complexity; agents provide the intelligence to bridge levels.
- Consequence: Each instance gets an `instance_id` (ULID) and `instance_name` in config. A `context.md` file inside `.lattice/` provides agent-readable context about the instance's role, relationships, and conventions. No automatic sync in v0 — agents read/write across instances manually.

---

## 2026-02-15: Hierarchical short IDs — project-subproject-seq

- Decision: Short IDs support an optional `subproject_code` yielding the format `{project_code}-{subproject_code}-{seq}` (e.g., `AUT-F-7`). When no subproject is set, the existing `{project_code}-{seq}` format is preserved.
- Rationale: The ID becomes a coordinate — project, subproject, task number — readable in conversation, commit messages, and cross-instance references without a lookup table. Matches real organizational structure (e.g., `AUT-F` for frontend, `AUT-B` for backend).
- Consequence: `subproject_code` (1–5 uppercase ASCII, optional) added to config. Existing short IDs are unaffected (backward compatible). Subproject depth is limited to one level — deeper hierarchy uses separate instances, not longer IDs.

---

## 2026-02-15: Agent-readable context file (`.lattice/context.md`)

- Decision: Each `.lattice/` directory may contain a `context.md` — a freeform markdown file describing the instance's purpose, related instances, conventions, and idiosyncrasies. Created by `lattice init` with a minimal template.
- Rationale: Agents read natural language context exceptionally well. A rigid JSON schema for instance relationships would be premature and couldn't express soft knowledge ("infra tasks take 2-3x estimates"). `config.json` stays machine-parseable for the CLI; `context.md` is the agent-facing context layer.
- Consequence: `context.md` is the CLAUDE.md of a Lattice instance. Agents should read it before working with an instance. It is non-authoritative (like notes) — informational, not enforced.

---

## 2026-02-15: Deep Attribution (Provenance) on Events

**Decision:** Add an optional `provenance` field to events as a sibling to the existing `agent_meta` field. Three sub-fields: `triggered_by` (event/task ID or free-form reference), `on_behalf_of` (actor format, validated), and `reason` (free-text). Included only when at least one sub-field is provided. No schema_version bump.

**Context:** Lattice tracks *proximate* attribution (who performed an action) via the required `actor` field. But in agent-orchestrated workflows, the chain of causation matters: who delegated the work, what event triggered it, and why. Without deep attribution, the event log records *what happened* but not *why it happened* or *on whose authority*.

**Rationale:**
- Follows the established `agent_meta` pattern: optional, sparse, invisible when unused.
- No breaking changes. Old events remain valid. Old code reading new events ignores `provenance` (unknown fields are tolerated per schema policy).
- Three CLI flags (`--triggered-by`, `--on-behalf-of`, `--reason`) added to all write commands via `common_options`.
- `on_behalf_of` is validated as actor format for consistency with the `actor` field.

**`--reason` conflict resolution:** The `status` command previously had its own `--reason` flag for forced transitions. This was removed in favor of the `--reason` from `common_options` (param name: `provenance_reason`). When `--force` is used with `--reason`, the reason is written to both `data.reason` (backward compatibility) and `provenance.reason`. When `--reason` is used without `--force`, it goes only to `provenance.reason`. This unifies the reason mechanism across all commands.

**Consequences:**
- Every write command now accepts `--triggered-by`, `--on-behalf-of`, and `--reason`.
- The provenance field appears in the event log (JSONL) and is displayed by `lattice show`.
- MCP tools are not yet updated (follow-up work).
- The Philosophy.md section on attribution has been updated to reflect this capability.

---

## 2026-02-15: Two primitive pairs — Tickets/Tasks and Panels/Displays

**Decision:** Lattice has two parallel compositional primitives:

1. **Tickets contain Tasks** — the work decomposition primitive. Humans think in tickets (units of concern: "fix auth redirect"). Agents think in tasks (units of execution: "read config, check token, write fix"). This was established in Philosophy.md and is already implemented.

2. **Panels contain Displays** — the information presentation primitive. A Panel is a configurable container that the human sees as a unit on the dashboard. A Display is a content element within a panel — a chart, a stat card, a table, an agent output, a timeline, anything that communicates state to the human. Panels are composable and user-configurable.

**Context:** The dashboard stats page revealed that a fixed layout doesn't scale. Different projects need different views. More importantly, Lattice's role as a common language for human-agent coordination requires a shared visual vocabulary — agents need consistent expectations for how to present information to humans. If every tool invents its own display format, there's no shared norm. Panels and Displays give agents (and other tools building on Lattice) a known target: "build a Display, put it in a Panel, and the human will see it in the expected way."

**Rationale:**
- Mirrors the Tickets/Tasks pair: Panels are the human-altitude view (what do I see?), Displays are the agent-altitude view (what do I render?).
- Open primitive: any agent, skill, or external tool can produce Displays that slot into Panels. This makes the dashboard a composable communication surface, not a hardcoded report.
- Establishes a shared norm across the ecosystem — other tools (OpenClaw skills, MCP servers, etc.) can target the Panel/Display contract and know their output will render consistently.

**Consequences:**
- Dashboard becomes panel-based: users configure which panels appear and what displays they contain.
- Display types are extensible — start with the proven ones (stat cards, charts, tables, timelines) and grow.
- Panel configuration lives in `.lattice/config.json` or a dedicated dashboard config.
- Not yet implemented. This decision captures the architectural direction.

---

## 2026-02-15: Task graph structure — the backlog is not a flat list

**Decision:** Tasks in the backlog (and other statuses) form a rich directed graph through their blocking/dependency relationships. This graph structure is first-class, not incidental. The system should support both simple list views and richer graph visualizations of the same data simultaneously.

**Context:** A backlog with 30 items may look flat, but many of those items have blocking relationships — task A blocks B, which blocks C and D, which both block E. This structure is critical information that a flat list hides. The relationships already exist in the event log (via `link` commands and `blocks`/`blocked_by` fields), but the dashboard and CLI present them as secondary metadata rather than as the primary structural reality.

**Rationale:**
- Tasks are zones in a graph, not items in a queue. Two tasks in "backlog" may have very different structural positions — one is a root blocker affecting five downstream items, another is a leaf with no dependencies. Treating them the same is information loss.
- The data is already there. Lattice captures `blocks`, `blocked_by`, `related_to`, `subtask_of`, `parent_of` relationships. The decision is to elevate this from "metadata you can query" to "structure the UI makes visible."
- Enables dependency chain risk analysis, bottleneck detection, and critical path visualization — several of the demo page improvements depend on this being treated as first-class.

**Consequences:**
- Dashboard should offer graph/network views alongside list views.
- Stats computations can analyze graph properties: longest chain, most-blocking task, orphan clusters, critical path.
- CLI commands like `lattice list` may gain topology-aware options (e.g., `--tree`, `--critical-path`).
- Not yet implemented. This decision captures the architectural direction.

---

## 2026-02-16: Plugin system via `importlib.metadata` entry points

- Decision: Lattice supports plugins via two `importlib.metadata` entry point groups: `lattice.cli_plugins` (register additional CLI commands) and `lattice.template_blocks` (provide additional CLAUDE.md template sections). Zero new dependencies.
- Rationale: Enables private extensions (e.g., `lattice-fractal`) to layer on additional CLI commands and CLAUDE.md template blocks without forking the core. The public repo is fully useful on its own while Stage 11 Agentics maintains its opinionated workflow layer privately. `importlib.metadata` is stdlib, well-understood, and used by the wider Python packaging ecosystem (pytest, setuptools, etc.).
- Constraints: v0 rejects `position: "replace_base"` for template blocks — plugins can only append, not replace the base template. Plugin load failures are logged to stderr but never crash the host CLI (matching `storage/hooks.py` error-handling pattern). `LATTICE_DEBUG=1` enables full tracebacks.
- Consequence: Consumer packages define entry points in their `pyproject.toml`. The first consumer is `lattice-fractal` (private package). Core codebase requires no changes to support new plugins — they are discovered automatically at runtime.

---

## 2026-02-15: Cube view — 2D-first graph visualization with status-constrained layout

**Decision:** The dashboard's spatial task visualization ("Cube" view) launches as a 2D force-directed graph using the `force-graph` library (~80KB), not the originally-proposed 3D `3d-force-graph` (~600KB). Layout uses explicit status-based X-axis positioning (d3.forceX pinned to workflow status index) with force-directed Y-axis separation, rather than `dagMode('lr')`.

**Context:** Two independent expert reviews of the original 3D plan identified a fundamental flaw: `dagMode('lr')` arranges nodes by graph topology (parent→child edges), not by status. Tasks with no relationships have no DAG position and float freely; tasks in cycles silently fall back to force-directed layout without indication. The plan's mental model — "status as spatial position" — requires explicit coordinate assignment, which dagMode does not provide.

Additional review findings that shaped this decision:
- A ~600KB CDN dependency (the dashboard's first) is disproportionate for v1 and introduces offline/firewall failure modes
- 3D orbit controls are poor on mobile and inaccessible to screen readers
- Small task counts (3-5, Lattice's current sweet spot) look awkward in 3D space
- The Panels/Displays primitive (decided same day) should be the long-term presentation architecture

**Approach:**
- v1: 2D force-graph with status-constrained X layout. Nodes colored by status (reusing `getLaneColor`), sized by priority. Directed edges colored by relationship type.
- v1.5: Optional 3D toggle that lazy-loads 3d-force-graph only when activated
- Long-term: Implement as a Display within the Panel system

**Key design choices:**
- Hover tooltips for quick inspection; single-click selects + shows side panel; double-click navigates to task detail. Users stay in the graph context.
- CDN fallback: graceful degradation message when library fails to load
- ETag-based revision on `/api/graph` for efficient auto-refresh (avoids JSON.stringify of full graph data every 5s)
- Async render generation counter prevents stale renders on rapid navigation

**Panel/Display compatibility:** The Cube is implemented as a standalone tab for now, but its rendering logic (initCubeGraph, updateCubeData, cleanupCube) is encapsulated as a module pattern compatible with future Display wrapping. When the Panel system is built, migrating Cube to a Display should require only wiring the entry/exit/update lifecycle hooks.

**Consequences:**
- Dashboard now has one external CDN dependency (force-graph, ~80KB with defer loading)
- New `/api/graph` endpoint reads full task snapshots; uses ETag for efficient polling
- 3D visualization deferred to v1.5 as a progressive enhancement
- Mobile gets a notice banner; accessibility relies on Board/List views as alternatives

---

## 2026-02-16: Field Guides and Runsheets — agent-facing operational primitives

**Decision:** Introduce two new convention-based artifacts in `.lattice/`: **Field Guides** (`.lattice/field-guides/<surface>.md`) describe the anatomy of an interaction surface, and **Runsheets** (`.lattice/runsheets/<surface>.md`) describe critical flows through that surface. Both are scoped per non-CLI interaction surface (web UI, iOS app, Android app).

**Context:** When a test agent, demo agent, or monitoring agent needs to operate a visual/interactive surface (browser, iOS simulator, Android emulator), it currently has to explore from scratch — burning tokens on discovery that a previous agent already completed. CLI surfaces don't have this problem (`--help` and CLAUDE.md cover them). The gap is visual surfaces mediated by MCP tools (Claude-in-Chrome, iOS Simulator MCP, Mobile MCP).

**What each primitive is:**
- **Field Guide** = the territory. Surface anatomy: how to connect (which MCP, what URL, launch sequence), what every view/screen contains, what controls exist, known bugs, interaction tips for agents. Relatively stable — changes when the UI changes.
- **Runsheet** = the critical paths. Ordered sequences through the surface that matter, with context on *why* they matter, expected behavior, and what to watch for. Inspired by Maestro-style flow testing but in plain English — the agent *is* the test runner. More operationally volatile than the field guide.

**Key properties:**
- **Agent-generated, agent-consumed.** An agent explores the surface and writes the guide. Future agents read it to bootstrap. Humans can read them too, but they're optimized for agent consumption.
- **Per interaction surface.** A project with a web dashboard and an iOS app gets `dashboard.md` and `ios-app.md` in each directory.
- **Living documents.** Updated when the surface changes. Mutable, not append-only.
- **Convention files, not schema-enforced.** Like `context.md` — no CLI commands, no JSON schema, no validation. Just a known directory and naming convention.
- **Runsheet cross-references its field guide.** The runsheet opens with "Read the field guide first."

**Naming rationale:** "Field Guide" — a reference you take into unfamiliar territory, written by someone who's already explored it. Naturally scoped ("the dashboard field guide"), plain English, not overloaded with existing tech meaning. "Runsheet" — production/broadcast term for ordered sequences of what happens and when, fitting for critical flow descriptions.

**On-disk layout:**
```
.lattice/
  field-guides/
    dashboard.md
    ios-app.md
  runsheets/
    dashboard.md
    ios-app.md
```

**Rationale:**
- Turns exploration tokens into a one-time authoring cost. A Sonnet-class model reading a field guide can do what an Opus model would need 10 minutes of exploration to figure out. That's the economic argument.
- No new infrastructure needed. Uses existing Lattice conventions (markdown files in `.lattice/`, freeform, non-authoritative).
- Separating anatomy (field guide) from flows (runsheet) mirrors the separation between understanding a system and operating it. Different update cadences, different consumers.

**Consequences:**
- `lattice init` should create these directories (future work).
- Agents updating a surface should be prompted to update the field guide (convention, not enforced).
- The first instances (dashboard field guide + runsheet) are written and serve as the template for future surfaces.
- No CLI commands in v0. Field guides and runsheets are direct file edits, like notes.

---

## 2026-02-16: Three-tier work hierarchy (Epic → Ticket → Task)

**Decision:** Lattice adopts a three-tier organizational hierarchy: **Epics** (strategic intent), **Tickets** (deliverables), and **Tasks** (units of execution). `ticket` is added as a task type. The tiers are connected via `subtask_of` relationships: tasks are subtasks of tickets, tickets are subtasks of epics.

**Context:** Lattice previously had `epic` as a task type but with no special behavior — just a label. Tasks were effectively flat with optional parent-child grouping. In practice, mixed human-agent coordination operates at three distinct altitudes: humans think at the ticket level (what needs to ship and why), agents think at the task level (how to make it happen), and leads/planners think at the epic level (the strategic arc connecting deliverables). Two tiers collapsed these distinct roles; three tiers give each a home.

**Rationale:**
- Each tier has a different job: epics aggregate strategic intent, tickets are the unit of delivery (assignable, branchable, reviewable), tasks are the unit of execution (what an agent picks up and completes).
- The ticket layer is where accountability lives — it's the natural level for branch linking, PR association, and code review.
- Git branches map to tickets, not tasks. A branch serves a deliverable; individual tasks within it are commits or sub-steps.
- The hierarchy uses existing primitives (task types + `subtask_of` relationships) — no new entity types or schema changes required in v0.

**Agnosticism principle:** Lattice is neutral about how teams use these tiers. The hierarchy is available, not imposed. Some teams will use all three tiers. Some will use flat tasks. The event log records what happened regardless of organizational choice. The primitives are unopinionated; the documentation offers the three-tier model as a current design belief, intended to evolve.

**Consequences:**
- `ticket` added to the default `task_types` list in config.
- Philosophy v3 and User Guide updated to describe the hierarchy.
- This is a convention change (Option 1), not a first-class entity change (Option 2). If the convention proves stable, promotion to distinct entity types with dedicated behavior is a future option.

---

## 2026-02-16: Indra's Web — coordination visualization and branch-to-task linking

**Decision:** Add a **Web** tab to the Lattice dashboard that visualizes the cross-repo coordination landscape. Lattice owns the link between branches and tasks via new `branch_linked` / `branch_unlinked` event types. The relationship is many-to-many.

**Context:** Existing dashboard views (Board, List, Activity, Cube) show task state and task relationships. None shows the coordination landscape — where agents are actively working across repos and branches, which deliverables have code moving, and which are stalled. This is the gap between "what depends on what?" (Cube) and "where is work happening right now?" (Web).

**The name:** Indra's Net — the Buddhist/Hindu image of an infinite web where every jewel reflects every other jewel. In a multi-repo, multi-agent system, repos genuinely reflect each other: changes in shared libraries ripple into consumers. Dependencies are the web.

**Visual model:**
- **Hubs** are epics (or repos, depending on the view mode). Central nodes from which work radiates.
- **Spokes** are tickets with linked branches. Each spoke represents a deliverable.
- **Dots** are tasks and commits along each spoke. As agents commit, dots appear at the growing tip.
- **Activity colors:** Yellow = recent git commit (~10 min). Orange = Lattice `in_progress`. Yellow-orange = both.
- **Spoke lifecycle:** When a branch merges and the ticket is marked `done`, the spoke retracts back into the hub.

**Two data layers:** Lattice provides the web's *structure* (task hierarchy, statuses, assignments). Git provides the *vital signs* (branch existence, commit recency, authorship). Together they surface signals neither provides alone: untracked branches (no Lattice task), stuck agents (task active but no commits), completed work (task done + branch merged).

**Branch-to-task linking:**
- Lattice owns the link as coordination state. `branch_linked` events are traceable, attributed, permanent.
- **Cardinality:** Many-to-many. A ticket can link to branches in multiple repos. Multiple tickets can converge on one integration branch.
- **Implicit (convention):** Branch names containing the task's short ID or slug (e.g., `feat/LAT-47-oauth`) are auto-detected as 1:1 links. Zero ceremony for the common case.
- **Explicit (authoritative):** `lattice link <task> --branch <branch-name>` for M:N relationships and cross-repo links.
- **Repo scoping:** `repo` field is nullable. `null` means the local repo. Cross-repo references are future work.

**Design principles:**
- Lattice-primary: the topology comes from Lattice, not git. This is a Lattice visualization enriched with git data, not a git visualization annotated with Lattice.
- Agnostic: renders whatever structure exists. No assumptions about how many tiers a team uses.
- Live: animates as work happens. Watching the web is watching agents work.
- Orphan detection: branches without Lattice tasks are surfaced as untracked work — a coordination failure made visible.

**Consequences:**
- New event types: `branch_linked`, `branch_unlinked` (fields: `branch`, `repo`).
- Dashboard gains a fifth tab (Web) after Board, List, Activity, Cube.
- Git integration in the dashboard (reading branch/commit data) is a prerequisite.
- Full design captured in `FutureFeatures.md`.

---

## 2026-02-16: `needs_human` as first-class workflow status

- Decision: Add `needs_human` as a distinct status, separate from `blocked`.
- Rationale: Agents need a clear signal for "I'm stuck on *you*" vs. generic external dependencies. `blocked` is ambiguous — it could be a CI failure, a dependency release, or a human decision. `needs_human` creates an explicit queue of "things waiting on the human" that can be scanned and acted on immediately.
- Transitions TO `needs_human`: from `in_planning`, `planned`, `in_progress`, `review` (any active state).
- Transitions FROM `needs_human`: to `in_planning`, `planned`, `in_progress`, `review`, `cancelled`.
- NOT reachable from `backlog` (work hasn't started) or `done`/`cancelled` (terminal).
- Convention: a `lattice comment` explaining what's needed is mandatory when moving to `needs_human`.
- Consequence: Weather reports show `[HUMAN]` attention items. Dashboard uses amber/orange for visual distinction from red `blocked`. The CLAUDE.md template includes a "When You're Stuck" section teaching agents when and how to signal.

---

## 2026-02-16: `lattice next` — pure selection for agent task picking

- Decision: Add `lattice next` as a read-only query command (with optional `--claim` for atomic assignment).
- Rationale: Agents need a deterministic, priority-aware way to pick the next task. Manual `lattice list | sort | filter` is error-prone and duplicated across every agent prompt. A single command with well-defined ordering prevents divergent task-picking logic.
- Algorithm: (1) Resume-first — if `--actor` specified, return in_progress/in_planning tasks assigned to that actor. (2) Pick from ready pool — backlog/planned, unassigned or assigned to requesting actor. (3) Sort by priority → urgency → ULID (oldest first).
- `--claim` atomically assigns the task to the actor and moves it to `in_progress` (two events under one lock). Requires `--actor`.
- Pure logic lives in `core/next.py` (no I/O). CLI wiring in `cli/query_cmds.py`. Weather `_find_up_next` delegates to the same sort logic.
- Consequence: Enables the sweep pattern — an autonomous loop that claims, works, transitions, and repeats. The `/lattice-sweep` skill builds on this primitive.
