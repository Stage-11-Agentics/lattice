# Lattice: Complete Functionality Requirements (v1)

> Lattice is a file-based, agent-native task tracker with an event-sourced core.
> Design goal: a small, stable “thin waist” that stays boring and correct in v0, then grows only when real pain shows up.

Items marked **(v0)** ship first. Items marked **(v1+)** are deferred, but the schema and layout anticipate them.

---

## 1. Core Philosophy

- Stack-agnostic, general-purpose work tracker (not tied to any language, framework, or repo style)
- Kanban-primary with optional sprint overlays later
- Agents are first-class writers; humans observe, override, and narrate
- File-based source of truth:
  - JSON for snapshots
  - JSONL for append-only history
  - Markdown for human narrative
- “Constraints are cheap, features are expensive”:
  - bake in conventions and correctness early
  - avoid building machinery until it is unavoidable

---

## 2. System Invariants (the “thin waist”)

These are non-negotiable constraints intended to prevent accidental complexity:

### 2.1 Events are authoritative (v0)

- The event log is the authoritative record of changes.
- Task JSON files are **materialized snapshots** for fast reads and git-friendly diffs.
- If a snapshot and events disagree, events win.
- Lattice must be able to rebuild snapshots from events.

### 2.2 One write path (v0)

- The CLI is the only supported write interface for authoritative state (events and snapshots).
- Any UI/dashboard is read-only and never mutates the filesystem.
- **Explicit exceptions:** Human-editable notes (`notes/<task_id>.md`) are non-authoritative supplementary files. They are not event-sourced and do not participate in rebuild. Direct file manipulation for manual recovery (e.g., moving archived files back) is similarly non-authoritative.

### 2.3 Prefer single-file mutations (v0)

- Lattice avoids designs that require “transactional” updates across multiple existing files.
- When multi-file updates are unavoidable, they must be:
  - lock-protected
  - ordered deterministically
  - recoverable via rebuild/doctor tooling

### 2.4 No duplicated edges as “source of truth” (v0)

- Bidirectional graphs (relationships, attachments) must not require keeping two sides in sync as the canonical record.
- Canonical linkage is recorded as events; snapshots may cache derived views.

### 2.5 Writes are safe under concurrency (v0)

- No corrupted files under concurrent agents is a hard requirement.
- All writes must be atomic and lock-protected.

### 2.6 Write ordering is event-first (v0)

- All mutations follow this order: append event, then materialize snapshot.
- If a crash occurs after the event is written but before the snapshot is updated, `lattice rebuild` recovers the snapshot from events.
- If a crash occurs before the event is written, no state change occurred.
- This ordering ensures events are always at least as current as snapshots.

---

## 3. Object Model

### 3.1 Core entities (v0)

- **Task**: current state snapshot (small JSON)
- **Event**: immutable, append-only record (JSONL)
- **Artifact**: metadata + payload pointer (metadata JSON, payload file)

### 3.2 Deferred entities (v1+)

- **Run**: promoted to a first-class entity (v0 uses `run_id` on events)
- **Agent registry**: capabilities, health, scheduling
- **Index**: rebuildable local index (SQLite or similar) for fast queries at large scale
- **Workflow entity**: multiple workflows per team/project

---

## 4. Identifiers and Idempotency

### 4.1 IDs (v0)

- Every entity has a stable ULID (time-sortable, filesystem-safe)
- Prefix IDs by type:
  - `task_...`
  - `ev_...`
  - `art_...`
- IDs never change; titles and names are freely renameable
- All references use IDs, never titles

### 4.2 Idempotent operations without coordination (v0)

- The CLI supports **caller-supplied IDs** for creations and event appends:
  - `lattice create --id task_...`
  - `lattice attach --id art_...`
  - `lattice log --id ev_...`
- Agents generate IDs once and reuse them across retries.
- If a create is retried with the same ID and identical payload, the CLI returns the existing entity (idempotent success).
- If a create is retried with the same ID but a different payload, the CLI returns a conflict error. This prevents silent data loss from agent bugs.

### 4.3 Optional operation dedupe (v1+)

- Idempotency keys per operation type (for higher-level orchestration)
- Deduplication is advisory; the canonical safeguard is caller-supplied IDs

---

## 5. File Layout and Storage

### 5.1 Directory structure (v0)

- `.lattice/` root directory (see section 5.3 for discovery rules)
- `tasks/`:
  - one JSON file per task snapshot: `tasks/<task_id>.json`
- `events/`:
  - per-task JSONL log: `events/<task_id>.jsonl` — **authoritative** record for each task
  - global JSONL log: `events/_lifecycle.jsonl` — **derived** convenience index, rebuildable from per-task logs (see section 9.1)
- `artifacts/`:
  - `artifacts/meta/<art_id>.json`
  - `artifacts/payload/<art_id>.<ext>` (or `<art_id>` if binary/unknown)
- `notes/`:
  - human Markdown notes per task: `notes/<task_id>.md`
- `archive/`:
  - mirrors task/events/notes structure:
    - `archive/tasks/`
    - `archive/events/`
    - `archive/notes/`
  - **Artifacts are not moved in v0** (see archival rules)
- `locks/`:
  - internal lock files for safe concurrent writes
- `config.json`

### 5.2 Format rules (v0)

- JSON for snapshots and metadata, JSONL for event streams, Markdown for notes
- JSON formatting:
  - sorted keys
  - 2-space indentation
  - trailing newline
  - deterministic output for clean git diffs
- Timestamps:
  - RFC 3339 UTC with `Z` suffix (e.g., `2026-02-15T03:45:00Z`)
  - All `created_at`, `updated_at`, `ts`, and other timestamp fields use this format
- Atomic writes:
  - write temp file, fsync, rename
- Event appends:
  - lock + append a single line + flush
  - If a crash leaves a truncated final line in a JSONL file, `lattice doctor` may safely remove it. This is the only permitted JSONL mutation outside of normal appends.

### 5.3 Root discovery (v0)

- The CLI finds `.lattice/` by walking up from the current working directory, stopping at the filesystem root. This mirrors `git`'s `.git/` discovery.
- Override with the `LATTICE_ROOT` environment variable, which points to the directory **containing** `.lattice/` (not the `.lattice/` directory itself).
- If no `.lattice/` is found and no override is set, commands other than `lattice init` exit with a clear error.

---

## 6. Task Snapshot Schema

> Tasks are materialized views derived from events. They are optimized for fast reads and stable diffs.

### 6.1 Task fields (v0)

Required:
- `schema_version` (int)
- `id` (string, `task_...`)
- `title` (string)
- `status` (string, validated against config)
- `created_at` (RFC 3339 UTC)
- `updated_at` (RFC 3339 UTC)

Recommended (nullable/optional):
- `description` (string)
- `priority` (enum: `critical`, `high`, `medium`, `low`)
- `urgency` (enum: `immediate`, `high`, `normal`, `low`)
- `type` (enum: `task`, `epic`, `bug`, `spike`, `chore`)
- `tags` (array of strings)
- `assigned_to` (prefixed string: `agent:{id}` / `human:{id}` / `team:{id}`)
- `created_by` (same format)
- `relationships_out` (array, see section 8)
- `artifact_refs` (array of artifact IDs, optional cache only, see section 9)
- `git_context` (object, optional cache only, see section 11)
- `last_event_id` (string, `ev_...` — ID of the most recent event applied to this snapshot; enables O(1) drift detection by `doctor`)
- `custom_fields` (open object, no validation in v0)

### 6.2 Compact serialization (v0)

- Agents can request a compact view:
  - `id`, `title`, `status`, `priority`, `urgency`, `type`, `assigned_to`, `tags`
  - optional counts: `relationships_out_count`, `artifact_ref_count`
- This is the default for list/board operations to conserve tokens.

### 6.3 Task types (v0)

- `task`: standard unit of work
- `epic`: a task that groups work via relationships (see `subtask_of`)
- `bug`: defect fix
- `spike`: research/investigation
- `chore`: maintenance/cleanup

---

## 7. Status and Workflow

### 7.1 Workflow is config-driven (v0)

- `config.json` defines:
  - allowed statuses
  - allowed transitions
  - optional WIP limits per status (advisory in v0)
- Default workflow ships with:
  - `backlog -> ready -> in_progress -> review -> done`
  - plus `blocked` and `cancelled`

### 7.2 Force override (v0)

- Any transition not in the graph requires:
  - `force: true`
  - `reason: string`
- Force transitions are recorded as events with full attribution.

### 7.3 WIP limits (v0 advisory)

- WIP limits are warnings only in v0.
- Enforcement and exception rules are v1+.

---

## 8. Relationships

### 8.1 Relationship types (v0)

- `blocks`
- `depends_on`
- `subtask_of`
- `related_to`
- `spawned_by`
- `duplicate_of`
- `supersedes`

Notes:
- Inverses exist conceptually (ex: `blocked_by`) but are **not stored as canonical duplicated edges**.

### 8.2 Canonical storage rule (v0)

- Tasks store **only outgoing relationships** in `relationships_out`.
- Reverse lookups are derived by scanning task snapshots (v0) or using an index (v1+).
- This avoids two-file transactional updates and split-brain links.

### 8.3 Relationship record shape (v0)

Each item in `relationships_out` contains:
- `type` (string)
- `target_task_id` (string)
- `created_at` (RFC 3339 UTC)
- `created_by` (prefixed string)
- `note` (optional string)

### 8.4 Integrity (v0)

- `lattice doctor` checks:
  - target IDs exist (or are archived)
  - no self-links
  - duplicate relationships (same type + target) flagged

(v1+)
- cycle detection and critical path computation
- richer graph queries with an index

---

## 9. Events (append-only, immutable)

### 9.1 Storage (v0)

- **Per-task JSONL** (`events/<task_id>.jsonl`): one event per line, never rewritten. This is the **authoritative** record for each task.
- **Global JSONL** (`events/_lifecycle.jsonl`): a **derived** convenience log that aggregates lifecycle events (task created, task archived) across all tasks. It is rebuildable from per-task event logs and is not a second source of truth. If the global log and per-task logs disagree, per-task logs win.

### 9.2 Event schema (v0)

Required:
- `schema_version` (int)
- `id` (string, `ev_...`)
- `ts` (RFC 3339 UTC)
- `type` (string)
- `actor` (prefixed string: `agent:{id}` / `human:{id}` / `team:{id}`)
- `data` (object, can be empty)

Conditional:
- `task_id` (string, `task_...`) — required for task-scoped events, absent for system-scoped events

Optional:
- `agent_meta` (object):
  - `model` (string, nullable)
  - `session` (string, nullable)
- `otel` (object, nullable):
  - `trace_id`, `span_id`, `parent_span_id`
- `metrics` (object, nullable):
  - `tokens_in`, `tokens_out`, `cost_usd`, `latency_ms`, `tool_calls`, `retries`, `cache_hits`, `error_type`
- `run_id` (string, nullable)

### 9.3 Event types (v0)

Task-scoped events (require `task_id`):

- `task_created`
- `task_archived`
- `task_unarchived`
- `status_changed`:
  - `from`, `to`, `force` (bool), `reason` (string|null)
- `assignment_changed`:
  - `from`, `to`
- `field_updated`:
  - `field`, `from`, `to`
- `comment_added`:
  - `body` (string)
- `relationship_added` / `relationship_removed`:
  - `type`, `target_task_id`
- `artifact_attached`:
  - `artifact_id`, `role` (optional string)
- `git_event`:
  - `action` (ex: `commit`)
  - `sha` (string)
  - `ref` (string|null)

Custom event types (via `lattice log`) must be prefixed with `x_` (e.g., `x_deployment_started`). Built-in type names above are reserved.

### 9.4 Telemetry and tracing posture (v0)

- OTel fields exist as passthrough metadata.
- No requirement that all agents participate.
- No exporter required in v0.

### 9.5 Tamper evidence (v1+)

- Hash chaining per task event log:
  - each event stores `prev_hash` and its own `hash`
- Optional signature support later

---

## 10. Artifacts

### 10.1 Artifact model (v0)

Artifact metadata is authoritative for:
- what the artifact is
- where its payload lives
- provenance (who created it, when, with what model)

Linkage between tasks and artifacts is recorded as events (`artifact_attached`) and reflected in task snapshots as an optional cache.

### 10.2 Artifact metadata fields (v0)

- `schema_version` (int)
- `id` (string, `art_...`)
- `type` (enum, see below)
- `title` (string)
- `summary` (string, optional)
- `created_at` (RFC 3339 UTC)
- `created_by` (prefixed string)
- `model` (string|null)
- `tags` (array of strings)
- `payload` (object):
  - `file` (string|null)
  - `content_type` (string|null)
  - `size_bytes` (int|null)
- `token_usage` (object|null):
  - `tokens_in`, `tokens_out`, `cost_usd`
- `sensitive` (bool, default false)
- `custom_fields` (open object)

### 10.3 Artifact types (v0)

- `conversation` (payload: JSONL messages)
- `prompt` (payload: text/markdown)
- `file` (payload: raw file)
- `log` (payload: text or JSONL)
- `reference` (no payload file; store URL/identifier in `custom_fields`)

### 10.4 Sensitive artifacts (v0)

- If `sensitive: true`, payload files are gitignored by default.
- Metadata may remain committed, but must not contain secrets.
- Rule of thumb: secrets live only in payloads, not in task titles/descriptions.

---

## 11. Git Integration

### 11.1 v0 posture: minimal, reliable

- Primary goal: traceability without cross-platform fragility.
- `post-commit` hook (optional) scans commit messages for `task_...` IDs.
- When found:
  - append a `git_event` to the task’s event log
  - optionally update `git_context` cache on the task snapshot:
    - `git_context.commits += [sha]`
    - `git_context.branch` if available

### 11.2 v1+ extensions

- diff-based `files_touched`
- PR integration (URLs)
- richer repo automation

---

## 12. Concurrency and Integrity

### 12.1 Locking rules (v0)

- All writes are protected with lock files in `.lattice/locks/`.
- Lock granularity:
  - task snapshot file lock when rewriting `tasks/<id>.json`
  - event log lock when appending `events/<id>.jsonl`
  - global event log lock when appending `events/_lifecycle.jsonl`

### 12.2 Deterministic lock ordering (v0)

- If an operation needs multiple locks, acquire them in a deterministic order:
  - sort by lock key (string sort)
- This prevents deadlocks under competing agents.

### 12.3 Doctor and rebuild (v0)

- `lattice doctor`:
  - validates JSON parseability
  - detects and safely removes truncated final lines in JSONL files
  - checks snapshot drift via `last_event_id` (O(1) consistency check)
  - checks missing referenced files (tasks, artifacts)
  - validates relationship targets exist or are archived
  - flags duplicate edges and malformed IDs
  - verifies `_global.jsonl` is consistent with per-task logs
- `lattice rebuild <task_id|all>`:
  - replays events to regenerate task snapshots
  - optionally rehydrates caches (relationship counts, artifact refs, git_context)
  - regenerates `_global.jsonl` from per-task event logs

---

## 13. CLI Interface

### 13.1 Core commands (v0)

- `lattice init`:
  - create `.lattice/` structure and default config
- `lattice create <title> [options]`:
  - create task snapshot + append `task_created` event
  - supports `--id task_...`
- `lattice update <task_id> [field=value ...]`:
  - append `field_updated` events + update snapshot
- `lattice status <task_id> <new_status> [--force --reason "..."]`:
  - validate transition via config; append `status_changed`; update snapshot
- `lattice assign <task_id> <actor_id>`:
  - append `assignment_changed`; update snapshot
- `lattice comment <task_id> "<text>"`:
  - append `comment_added`
- `lattice list [filters]`:
  - filters: `--status`, `--assigned`, `--tag`, `--type`
- `lattice show <task_id> [--full]`
- `lattice log <task_id> <event_type> [--data <json>]`:
  - escape hatch for custom event types
  - event type must be prefixed with `x_` (e.g., `x_deployment_started`); built-in types are rejected
  - supports `--id ev_...`
- `lattice attach <task_id> <file_or_url> [--type ... --title ...]`:
  - create artifact metadata (+ payload when applicable)
  - append `artifact_attached` to task
  - supports `--id art_...`
- `lattice link <task_id> <type> <target_task_id>`:
  - append `relationship_added`; update snapshot cache `relationships_out`
- `lattice unlink <task_id> <type> <target_task_id>`
- `lattice archive <task_id>`:
  - append `task_archived` event to the task's event log
  - move task snapshot, events, notes into `archive/`
  - update `_global.jsonl` (derived convenience log)
- `lattice doctor`
- `lattice rebuild <task_id|all>`

### 13.2 Agent-optimized flags (v0)

- `--json` output with structured envelope:
  - success: `{"ok": true, "data": ...}`
  - error: `{"ok": false, "error": {"code": "string", "message": "string"}}`
- `--compact` output
- `--quiet` (only print the ID/result)
- attribution:
  - `--actor=agent:...` / `--actor=human:...`
  - `--model=...`
  - `--session=...`
- tracing passthrough:
  - `--trace_id=... --span_id=... --parent_span_id=...`

---

## 14. Dashboard

### 14.1 v0 dashboard: read-only local server

- `lattice dashboard` starts a tiny local read-only HTTP server that:
  - serves a single HTML/JS page (no build step)
  - exposes JSON endpoints that read `.lattice/` on demand
- Features:
  - board view by status
  - list view with filters
  - task detail with event timeline
  - recent activity feed (from global events or recent per-task events)

Constraints:
- no writes
- binds to `127.0.0.1` only by default (no network exposure); explicit `--host` flag to override
- no auth (acceptable because local-only by default)
- no real-time updates required

### 14.2 v1+ dashboard

- dependency graph visualization
- run drilldowns and trace trees
- telemetry and cost views
- “what changed” diffs
- index-backed queries for large repos

---

## 15. Search

### 15.1 v0 search

- scan/grep across task snapshots (title, tags, status, assigned)
- optionally include notes
- no index required for small to medium scale

### 15.2 v1+ search

- full-text search across artifact payloads
- rebuildable local index (SQLite or embedded)
- dashboard loads in under 1s at large scale

---

## 16. Security and Sensitive Data

### 16.1 v0

- `sensitive: true` flag on artifacts
- gitignore payloads for sensitive artifacts by default
- no secrets in task titles/descriptions

### 16.2 v1+

- encryption at rest for sensitive payloads
- redaction tools for sharing repos
- per-agent access controls (only if/when needed)

---

## 17. Lifecycle and Maintenance

### 17.1 Archiving (v0)

- archiving moves:
  - task snapshot
  - task event log
  - task notes
- artifacts are **not moved** in v0
  - avoids many-to-many relocation problems
  - archived tasks can still reference artifacts by ID

### 17.2 Schema evolution (v0)

- every file has `schema_version`
- forward compatibility: unknown fields must be tolerated
- migrations are explicit tools (v1+), not silent rewrites

---

## 18. Design Targets

### 18.1 Scale targets (v0)

- up to ~10,000 active tasks
- up to ~100,000 events/day
- up to ~50 concurrent agents
- artifacts payloads up to ~1 MB

### 18.2 Graduation criteria (v1+)

- when filesystem scanning becomes the bottleneck:
  - add a rebuildable index first
  - only later consider a DB backend
- CLI interface and on-disk formats remain stable
- storage engine can change without breaking users

---

## 19. Non-Goals (explicit)

- not a CI/CD system
- not a chat application (conversations are stored as artifacts)
- not an alerting/monitoring platform (telemetry is for analysis)
- not a code review tool
- not an agent runtime or process manager
