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
