# Task: Implement Lattice v0 core functionality

## Context

Lattice is a file-based, agent-native task tracker with an event-sourced core. The project scaffold and `lattice init` are already implemented and tested (36 tests passing). This prompt covers the core CLI commands, event system, and integrity tools.

**Scope note:** The dashboard (`lattice dashboard`) is a separate follow-up prompt. Git integration (post-commit hook) and OTel tracing flags are deferred — the event schema supports them, but the CLI machinery isn't needed yet.

**This is a large scope.** The phases below have clear boundaries. If context runs low, stop at a phase boundary — each phase is independently testable. The natural split points are after Phase 2 (core write path works) and after Phase 5 (all write commands work).

**Key architectural facts you must internalize:**
- Events are authoritative; task JSON files are materialized snapshots (invariant 2.1)
- CLI is the only write interface for authoritative state (invariant 2.2)
- Write ordering is event-first: append event, then materialize snapshot (invariant 2.6)
- All writes are lock-protected and atomic (invariant 2.5)
- Multi-lock operations acquire locks in sorted key order to prevent deadlocks (section 12.2)
- `_global.jsonl` is a derived convenience log, not a second source of truth (section 9.1)
- Only `task_created` and `task_archived` events go to the global log
- JSON snapshots: `json.dumps(data, sort_keys=True, indent=2) + "\n"`
- JSONL events: `json.dumps(event, sort_keys=True, separators=(",", ":")) + "\n"`
- Timestamps: RFC 3339 UTC with `Z` suffix (e.g., `2026-02-15T03:45:00Z`)

## Before writing any code, read these files in order:

1. `CLAUDE.md` — Project guide: architecture, layer boundaries, coding conventions
2. `ProjectRequirements_v1.md` — Full spec (all sections are now relevant)
3. `Decisions.md` — Architectural decisions with rationale

Then read **every existing source file** to understand what's already built.

## Existing codebase

These are already implemented and tested:

- `cli/main.py` — Click group entry point + `init` command
- `core/config.py` — `default_config()`, `serialize_config()`
- `core/ids.py` — `generate_task_id()`, `generate_event_id()`, `generate_artifact_id()` (using python-ulid)
- `storage/fs.py` — `atomic_write()`, `ensure_lattice_dirs()`, `find_root()`, `LatticeRootError`
- `tests/conftest.py` — `lattice_root` and `initialized_root` fixtures

These are stubs (docstring only): `core/events.py`, `core/tasks.py`, `core/relationships.py`, `core/artifacts.py`, `storage/locks.py`

## The write path pattern

**Every write command follows this exact pattern.** Understand it before writing any command.

```
1. CLI layer (cli/):
   - Parse arguments
   - Resolve root: find_root() → fail if None
   - Load config: read .lattice/config.json
   - Generate or validate IDs
   - Call core layer for validation and data building

2. Core layer (core/):
   - Validate inputs against config (status, type, transitions, etc.)
   - Build event dict(s) — pure data, no I/O
   - Build or update snapshot dict — pure data, no I/O
   - Return event(s) and snapshot

3. Storage layer (storage/):
   - Acquire lock(s) in sorted key order
   - Append event to events/<task_id>.jsonl (JSONL compact format)
   - If lifecycle event (task_created, task_archived): also append to events/_global.jsonl
   - Atomic write snapshot to tasks/<task_id>.json (JSON pretty format)
   - Release locks

4. CLI layer:
   - Format output (human-readable or --json envelope)
   - Print to stdout (data) or stderr (errors/warnings)
```

**Lock keys** follow the pattern `<dir>_<filename>`, e.g.:
- `events_task_01HQ...` — for the per-task event log
- `events__global` — for the global event log
- `tasks_task_01HQ...` — for the task snapshot

Sort these keys lexicographically before acquiring.

**Every event updates the snapshot's `last_event_id` and `updated_at`**, even events that don't change other snapshot fields (like `comment_added`). This keeps drift detection simple: doctor compares `last_event_id` against the actual last event in the JSONL.

---

## Phase 1: Foundation

### 1.1 ID and actor validation (`core/ids.py`)

Extend the existing module:

- `validate_id(id_str: str, expected_prefix: str) -> bool` — validate `<prefix>_<ulid>` format. The ULID portion should be a valid 26-character Crockford Base32 string.
- `validate_actor(actor_str: str) -> bool` — validate `prefix:identifier` format where both parts are non-empty. Valid prefixes: `agent`, `human`, `team`. No registry — just format checking.
- Keep all existing `generate_*` functions unchanged.

### 1.2 Config loading and validation (`core/config.py`)

Extend the existing module:

- `load_config(raw: str) -> dict` — parse JSON string, return config dict. Core function takes a string (CLI reads the file and passes it in).
- `validate_status(config: dict, status: str) -> bool`
- `validate_transition(config: dict, from_status: str, to_status: str) -> bool`
- `validate_task_type(config: dict, task_type: str) -> bool`
- `get_wip_limit(config: dict, status: str) -> int | None`
- `VALID_PRIORITIES = ("critical", "high", "medium", "low")`
- `VALID_URGENCIES = ("immediate", "high", "normal", "low")`

### 1.3 File locking (`storage/locks.py`)

Implement using the `filelock` library (already in deps):

- `lattice_lock(locks_dir: Path, key: str, timeout: float = 10) -> ContextManager` — acquire a single lock file at `locks_dir/<key>.lock`.
- `multi_lock(locks_dir: Path, keys: list[str], timeout: float = 10) -> ContextManager` — sort keys, acquire each lock in order. This is a context manager that acquires all locks on enter and releases all on exit.
- If a lock can't be acquired within timeout, raise a clear error.

### 1.4 Event system (`core/events.py`)

- `BUILTIN_EVENT_TYPES` — frozenset of all built-in types from section 9.3: `task_created`, `task_archived`, `status_changed`, `assignment_changed`, `field_updated`, `comment_added`, `relationship_added`, `relationship_removed`, `artifact_attached`, `git_event`
- `GLOBAL_LOG_TYPES` — frozenset of event types that go to _global.jsonl: `task_created`, `task_archived`
- `create_event(type: str, task_id: str, actor: str, data: dict, *, event_id: str | None = None, ts: str | None = None, model: str | None = None, session: str | None = None) -> dict` — build a complete event dict with all required fields, optional fields included only when provided. Generates `id` and `ts` automatically if not supplied. The explicit `ts` parameter is needed for batch operations (e.g., `lattice update` generating multiple `field_updated` events that must share a timestamp). The event schema supports `otel` and `run_id` fields (section 9.2), but no CLI flags produce them yet — they can be added later without schema changes.
- `serialize_event(event: dict) -> str` — JSONL format: `json.dumps(event, sort_keys=True, separators=(",", ":")) + "\n"`
- `validate_custom_event_type(event_type: str) -> bool` — must start with `x_` and not be a built-in type.

### 1.5 JSONL append (`storage/fs.py`)

Add to the existing module:

- `jsonl_append(path: Path, line: str) -> None` — append a single line to a JSONL file. The line must already end with `\n`. Opens in append mode, writes, flushes, fsyncs. This is called *inside* a lock — the function itself doesn't acquire locks.

### 1.6 Task snapshot helpers (`core/tasks.py`)

- `apply_event_to_snapshot(snapshot: dict | None, event: dict) -> dict` — the **single materialization path** used by both write commands (incremental) and rebuild (replay). Takes an existing snapshot (or `None` for `task_created`) and an event, returns an updated snapshot. **Critical for rebuild determinism:** all snapshot timestamps (`created_at`, `updated_at`) are sourced from the event's `ts` field, never from wall clock. This guarantees byte-identical output when replaying events.

  Must handle every built-in event type:
  - `task_created` → initialize snapshot from event data; `created_at` and `updated_at` both come from event `ts`
  - `status_changed` → update `status`
  - `assignment_changed` → update `assigned_to`
  - `field_updated` → update the named field
  - `comment_added` → no field changes (just `last_event_id` + `updated_at`)
  - `relationship_added` → append to `relationships_out`
  - `relationship_removed` → remove from `relationships_out`
  - `artifact_attached` → append to `artifact_refs`
  - `git_event` → no-op for now (git integration deferred, but the type should be recognized)
  - `task_archived` → no field changes (archival is a file operation)
  - Any `x_*` custom type → no field changes
  - Always updates `last_event_id` and `updated_at` (from event `ts`)
- `serialize_snapshot(snapshot: dict) -> str` — JSON pretty format: `json.dumps(data, sort_keys=True, indent=2) + "\n"`
- `compact_snapshot(snapshot: dict) -> dict` — return only: `id`, `title`, `status`, `priority`, `urgency`, `type`, `assigned_to`, `tags`, plus `relationships_out_count` and `artifact_ref_count`.

### Phase 1 validation

Run tests. The foundation modules should be independently testable:
- ID validation: valid/invalid formats, wrong prefix, actor format
- Config: load, validate transitions, WIP limits, status/type checks
- Locks: acquire/release, multi-lock ordering, timeout
- Events: create with all fields, serialize to JSONL, custom type validation
- JSONL append: content written correctly, append mode (not overwrite)
- Tasks: create snapshot, apply each event type, compact view

---

## Phase 2: Task CRUD commands

### 2.1 Shared CLI infrastructure

Before building individual commands, set up shared patterns:

**Root resolution helper:**
```python
def require_root():
    """Find .lattice/ root or exit with error."""
    root = find_root()
    if root is None:
        click.echo("Error: not a Lattice project (no .lattice/ found). Run 'lattice init' first.", err=True)
        raise SystemExit(1)
    return root
```

**Shared Click options** for write commands — use a decorator that adds common options to avoid repetition:
- `--actor` (required on write commands — the entity performing the action)
- `--model` (optional)
- `--session` (optional)
- `--json` (flag — output structured envelope)
- `--quiet` (flag — print only the primary ID)

**Config loading** is the same for every command: `json.loads((lattice_dir / "config.json").read_text())`.

**CLI file organization:** `cli/main.py` currently has `init`. As commands grow, split naturally — don't cram everything into one file, but don't over-split either. A reasonable split: keep `init` in `main.py`, group related commands in a few modules (e.g., task commands, query commands, integrity commands), and register them on the `cli` group via `cli.add_command()`.

### 2.2 `lattice create`

```
lattice create <title>
  [--type TYPE] [--priority PRIORITY] [--urgency URGENCY]
  [--status STATUS] [--description DESC] [--tags TAG,TAG,...]
  [--assigned-to ACTOR] [--id TASK_ID]
  [--actor ACTOR] [--model MODEL] [--session SESSION]
  [--json] [--quiet]
```

Implementation:
1. `title` is a required argument.
2. Default `--status` from config (`default_status`), default `--priority` from config (`default_priority`), default `--type` is `"task"`.
3. If `--id` provided: validate format (`task_` prefix + valid ULID). Check if task already exists:
   - Exists with identical payload → print existing, exit 0 (idempotent success)
   - Exists with different payload → error "Conflict: task {id} exists with different data", exit 1
   - For idempotency comparison, compare: `title`, `type`, `priority`, `urgency`, `status`, `description`, `tags`, `assigned_to`. Ignore timestamps and event IDs.
4. If no `--id`: generate one via `generate_task_id()`.
5. Validate `--actor` format, `--status` against config, `--type` against config, `--priority` against valid enum, `--urgency` against valid enum.
6. Build `task_created` event (core layer).
7. Build initial task snapshot (core layer).
8. Acquire locks: event log + global log + snapshot. Write event-first, then global log, then snapshot (storage layer).
9. Output: human-readable confirmation with task ID, or `--json` envelope with full task, or `--quiet` just the ID.

Human output example:
```
Created task task_01HQ... "Fix login redirect bug"
  status: backlog  priority: high  type: bug
```

### 2.3 `lattice update`

```
lattice update <task_id> [field=value ...]
  [--actor ACTOR] [--json] [--quiet]
```

- `task_id` is required. Validate it exists (read snapshot file).
- `field=value` pairs: parse as `key=value`. Updatable fields: `title`, `description`, `priority`, `urgency`, `type`, `tags` (comma-separated), `custom_fields.<key>` (dot notation for nested custom fields).
- For each field change, build a `field_updated` event with `field`, `from` (old value), `to` (new value).
- Skip fields where old == new (no event needed).
- If no fields actually changed, print "No changes" and exit 0.
- All events share the same timestamp. Apply all to snapshot incrementally.
- Acquire locks once for the whole batch.

### 2.4 `lattice status`

```
lattice status <task_id> <new_status>
  [--force] [--reason REASON]
  [--actor ACTOR] [--json] [--quiet]
```

- Read existing snapshot to get current status.
- If current == new: print "Already at status {status}" and exit 0.
- Check `validate_transition(config, current, new)`:
  - Valid → proceed
  - Invalid + no `--force` → error "Invalid transition from {current} to {new}. Use --force --reason to override."
  - Invalid + `--force` but no `--reason` → error "--reason is required with --force"
  - Invalid + `--force` + `--reason` → proceed with force=true in event
- Build `status_changed` event with `from`, `to`, `force` (bool), `reason` (string|null).
- WIP limits are advisory only in v0. The config stores them and `get_wip_limit()` exists for future use, but don't implement the counting/warning logic yet — it requires scanning all tasks on every status change for little v0 value.

### 2.5 `lattice assign`

```
lattice assign <task_id> <actor_id>
  [--actor ACTOR] [--json] [--quiet]
```

- Validate `actor_id` format (prefix:identifier).
- Read existing snapshot to get current `assigned_to`.
- If already assigned to same actor: print "Already assigned to {actor_id}" and exit 0.
- Build `assignment_changed` event with `from` (old, may be null), `to` (new).

### 2.6 `lattice comment`

```
lattice comment <task_id> <text>
  [--actor ACTOR] [--json] [--quiet]
```

- `text` is a required argument (the comment body).
- Build `comment_added` event with `body`.
- Update snapshot's `last_event_id` and `updated_at` only — no other snapshot fields change.
- Comments live only in the event log. `lattice show` displays them from the event timeline.

### Phase 2 validation

Test every command with CliRunner:
- Create: happy path, defaults from config, custom fields, --id idempotent success, --id conflict, invalid type/priority/status
- Update: single field, multiple fields, no-change skip, invalid field name
- Status: valid transition, invalid without force, force without reason, force with reason
- Assign: valid actor, invalid format, no-change skip
- Comment: event appended, snapshot last_event_id updated, comment text in event
- Cross-cutting: verify event exists in JSONL before snapshot on disk (event-first)
- Cross-cutting: verify lock files used (can test by checking lock dir activity or by mocking)
- Cross-cutting: verify global log gets task_created events but not status_changed, etc.

---

## Phase 3: Relationships

### 3.1 Relationship types (`core/relationships.py`)

- `RELATIONSHIP_TYPES` — frozenset: `blocks`, `depends_on`, `subtask_of`, `related_to`, `spawned_by`, `duplicate_of`, `supersedes`
- `validate_relationship_type(rel_type: str) -> bool`
- `build_relationship_record(rel_type: str, target_task_id: str, created_by: str, note: str | None = None) -> dict` — builds the record shape from section 8.3: `type`, `target_task_id`, `created_at`, `created_by`, `note`.

### 3.2 `lattice link`

```
lattice link <task_id> <type> <target_task_id>
  [--note NOTE] [--actor ACTOR] [--json] [--quiet]
```

- Validate relationship type.
- Validate both task IDs exist (check tasks/ directory).
- Reject self-links (task_id == target_task_id).
- Reject duplicates: same type + same target already in snapshot's `relationships_out`.
- Build `relationship_added` event with `type`, `target_task_id`.
- Update snapshot: append to `relationships_out`.
- Only the source task's snapshot and event log are modified — no writes to the target task (invariant 2.4, no duplicated edges).

### 3.3 `lattice unlink`

```
lattice unlink <task_id> <type> <target_task_id>
  [--actor ACTOR] [--json] [--quiet]
```

- Validate the relationship exists in snapshot's `relationships_out` (match on type + target_task_id).
- If not found: error "No {type} relationship to {target_task_id}".
- Build `relationship_removed` event with `type`, `target_task_id`.
- Remove from snapshot's `relationships_out`.

### Phase 3 validation

- Link: valid relationship, all 7 types work, self-link rejected, duplicate rejected, target doesn't exist → error
- Unlink: existing relationship removed, non-existent relationship → error
- Show: after link, `lattice show` displays the relationship

---

## Phase 4: Artifacts

### 4.1 Artifact metadata (`core/artifacts.py`)

- `ARTIFACT_TYPES` — frozenset: `conversation`, `prompt`, `file`, `log`, `reference`
- `create_artifact_metadata(art_id: str, type: str, title: str, *, created_by: str, summary: str | None = None, model: str | None = None, tags: list[str] | None = None, payload_file: str | None = None, content_type: str | None = None, size_bytes: int | None = None, sensitive: bool = False, custom_fields: dict | None = None) -> dict` — build metadata dict per section 10.2. Sets `schema_version: 1`, `created_at`, etc. The flat `payload_file`, `content_type`, `size_bytes` params are assembled into a nested `payload` object in the output: `{"payload": {"file": ..., "content_type": ..., "size_bytes": ...}}`.
- `serialize_artifact(metadata: dict) -> str` — same JSON format as snapshots.

### 4.2 `lattice attach`

```
lattice attach <task_id> <source>
  [--type TYPE] [--title TITLE] [--summary SUMMARY]
  [--sensitive] [--role ROLE] [--id ART_ID]
  [--actor ACTOR] [--model MODEL] [--json] [--quiet]
```

- `source` is a file path or URL.
- If `--type` not provided, infer: if source is a URL → `reference`; if source is a file → `file`.
- If source is a file:
  - Copy to `artifacts/payload/<art_id>.<ext>` (preserve extension). Use `shutil.copy2`.
  - Record `payload.file`, `payload.content_type` (guess from extension), `payload.size_bytes`.
- If source is a URL (`reference` type):
  - No payload file. Store URL in `custom_fields.url`.
- If `--title` not provided, derive from filename or URL.
- Write artifact metadata to `artifacts/meta/<art_id>.json` (atomic write).
- Append `artifact_attached` event to task's event log (with `artifact_id`, optional `role`).
- Update task snapshot's `artifact_refs` cache (append art_id).
- Idempotency: if `--id` provided and artifact metadata already exists, compare payload. Same → success. Different → conflict error.

### Phase 4 validation

- Attach file: metadata created, payload copied, event appended, task snapshot updated
- Attach URL: no payload file, metadata has url in custom_fields
- Sensitive flag: metadata has `sensitive: true`
- Idempotency: same --id + same source → success; different source → conflict
- Show: after attach, `lattice show` lists artifact refs

---

## Phase 5: Custom events and querying

### 5.1 `lattice log`

```
lattice log <task_id> <event_type> [--data JSON_STRING]
  [--id EV_ID] [--actor ACTOR] [--json] [--quiet]
```

- `event_type` MUST start with `x_`. If it's a built-in type, error: "Event type '{type}' is reserved. Custom types must start with 'x_'."
- `--data` is an optional JSON string parsed into the event's `data` field. Default: `{}`.
- Validate task exists.
- Build event, append to per-task log only. Custom events do NOT go to `_global.jsonl` — the global log contains only lifecycle events (`task_created`, `task_archived`).
- Update snapshot's `last_event_id` and `updated_at`.

### 5.2 `lattice list`

```
lattice list [--status STATUS] [--assigned ACTOR] [--tag TAG] [--type TYPE]
  [--compact] [--json]
```

- Scan all `.json` files in `tasks/`.
- Apply filters (all are optional, combine with AND):
  - `--status`: exact match on status field
  - `--assigned`: exact match on assigned_to field
  - `--tag`: task's tags array contains this tag
  - `--type`: exact match on type field
- Sort by task ID (ULID ordering = chronological).
- Default output: compact view — one task per line, tabular. Example:
  ```
  task_01HQ...  backlog  medium  task  "Fix login bug"       agent:claude
  task_01HQ...  ready    high    bug   "Handle null input"   unassigned
  ```
- `--compact` is the default for human output. `--json` outputs array of task dicts (compact if `--compact`, full otherwise).

### 5.3 `lattice show`

```
lattice show <task_id> [--full] [--compact] [--json]
```

- Read task snapshot from `tasks/<task_id>.json`. If not found, check `archive/tasks/<task_id>.json` and note it's archived.
- Read event log from `events/<task_id>.jsonl` (or `archive/events/`).
- Check for notes file at `notes/<task_id>.md`.
- Display outgoing relationships from the snapshot's `relationships_out`. Don't scan other tasks for derived incoming relationships — that's a v1 query optimization.
- Human output format:
  ```
  task_01HQ...  "Fix login redirect bug"
  Status: in_progress  Priority: high  Type: bug
  Assigned: agent:claude  Created by: human:atin
  Created: 2026-02-15T03:45:00Z  Updated: 2026-02-15T04:12:00Z

  Description:
    The login page redirects to a 404 after OAuth callback.

  Relationships:
    blocks → task_01HQ... "Deploy v2.1"

  Artifacts:
    art_01HQ... "OAuth debug log" (file)

  Notes: notes/task_01HQ....md

  Events (latest first):
    2026-02-15T04:12:00Z  status_changed  ready → in_progress  by agent:claude
    2026-02-15T04:00:00Z  comment_added  "Starting work on this"  by agent:claude
    2026-02-15T03:45:00Z  task_created  by human:atin
  ```
- `--full`: include complete event data (all fields).
- `--json`: structured output with all sections as objects.

### Phase 5 validation

- Log: x_ prefix accepted, built-in type rejected, --data parsed, event in per-task log only (not global)
- List: no filters returns all, each filter type works, combined filters (AND), empty results
- List: compact output formatting, --json output
- Show: full display with outgoing relationships, events, artifacts, notes path
- Show: archived task found in archive/

---

## Phase 6: Archive

### 6.1 `lattice archive`

```
lattice archive <task_id>
  [--actor ACTOR] [--json] [--quiet]
```

Event-first ordering (invariant 2.6), with locks held through the entire operation to prevent races:

1. Read existing task snapshot. Verify task exists and is not already archived.
2. Build `task_archived` event.
3. Acquire locks for: per-task event log, global event log, task snapshot.
4. Append `task_archived` to per-task event log (`events/<task_id>.jsonl`).
5. Append `task_archived` to global event log (`events/_global.jsonl`).
6. Move files (locks still held — prevents concurrent writes between event append and move):
   - `tasks/<task_id>.json` → `archive/tasks/<task_id>.json`
   - `events/<task_id>.jsonl` → `archive/events/<task_id>.jsonl`
   - `notes/<task_id>.md` → `archive/notes/<task_id>.md` (if exists)
7. Release locks.
8. Artifacts are NOT moved (section 17.1).

If task doesn't exist: error "Task {id} not found."
If task already archived (exists in archive/): error "Task {id} is already archived."

### Phase 6 validation

- Archive: event appended before files moved
- Archive: all three file types moved to archive/
- Archive: notes moved only if exists
- Archive: artifacts NOT moved
- Archive: already-archived task → error
- Archive: task no longer in tasks/ after archive
- List: archived tasks don't appear in `lattice list`
- Show: archived task found in archive/ with note

---

## Phase 7: Integrity tools

### 7.1 `lattice doctor`

```
lattice doctor [--fix] [--json]
```

Performs all checks from section 12.3. Reports findings but does not fix anything unless `--fix` is passed.

Checks:
1. **JSON parseability**: every `.json` in tasks/, artifacts/meta/, archive/tasks/, and config.json.
2. **JSONL parseability**: every `.jsonl` in events/, archive/events/. Parse line by line. If the final line is truncated (invalid JSON), flag it. With `--fix`, remove truncated final lines.
3. **Snapshot drift**: for each active task, compare `last_event_id` from the snapshot against the `id` field of the last event in the corresponding JSONL file. If they don't match, report drift.
4. **Missing references**: for each task's `relationships_out`, verify `target_task_id` exists in tasks/ or archive/tasks/.
5. **Missing artifacts**: for each task's `artifact_refs`, verify metadata exists in artifacts/meta/.
6. **Self-links**: flag any relationship where `target_task_id` == the task's own `id`.
7. **Duplicate edges**: flag relationships with same `type` + `target_task_id` appearing more than once.
8. **Malformed IDs**: check all task IDs match `task_` prefix, event IDs match `ev_`, artifact IDs match `art_`.
9. **Global log consistency**: compare events in `_global.jsonl` against per-task event logs. Every lifecycle event in global should have a matching event in the per-task log.

Output: summary of findings.
```
Checking 42 tasks, 156 events, 8 artifacts...
✓ All JSON files valid
✓ All JSONL files valid
⚠ Snapshot drift: task_01HQ... (last_event_id mismatch)
✓ All relationship targets exist
✓ No self-links
✓ No duplicate edges
✓ All IDs well-formed
✓ Global log consistent

1 warning found. Run 'lattice rebuild task_01HQ...' to fix drift.
```

With `--json`: structured envelope with array of findings.

### 7.2 `lattice rebuild`

```
lattice rebuild <task_id> [--json]
lattice rebuild --all [--json]
```

- `rebuild` and `doctor` are recovery/maintenance tools, not attributed write commands — they don't take `--actor` and don't create events. They do support `--json` for consistent output.
- **Single task**: read all events from `events/<task_id>.jsonl`, replay them through `apply_event_to_snapshot()` starting from scratch, write the resulting snapshot to `tasks/<task_id>.json` via atomic write.
- **All tasks**: rebuild every active task + regenerate `_global.jsonl`:
  1. For each `.jsonl` in events/ (excluding `_global.jsonl`): replay events, write snapshot.
  2. Rebuild `_global.jsonl`: scan all per-task event logs (including archived), collect all `task_created` and `task_archived` events, sort by `(ts, id)` for deterministic ordering even when timestamps collide, write to `_global.jsonl` via atomic write.
- Must produce **byte-identical** snapshots regardless of run order (deterministic JSON serialization guarantees this).
- Acquire appropriate locks during writes.

### Phase 7 validation

- Doctor: clean state passes all checks
- Doctor: detects truncated JSONL final line
- Doctor: detects snapshot drift (manually modify last_event_id)
- Doctor: detects missing relationship target
- Doctor: detects self-link
- Doctor: detects duplicate edge
- Doctor: detects malformed ID
- Doctor: --fix removes truncated line
- Rebuild single: produces snapshot identical to original
- Rebuild all: all snapshots byte-identical, global log regenerated
- Rebuild: after simulated drift, rebuild fixes it (doctor passes after rebuild)

---

## Phase 8: Cross-cutting CLI flags

These should be implemented as part of building the commands above, but this section clarifies the exact behavior.

### 8.1 `--json` output

Every command supports `--json`. When present:

Success:
```json
{"ok": true, "data": {...}}
```

Error:
```json
{"ok": false, "error": {"code": "INVALID_TRANSITION", "message": "Cannot transition from backlog to done"}}
```

Error codes should be SCREAMING_SNAKE_CASE: `NOT_FOUND`, `CONFLICT`, `INVALID_TRANSITION`, `INVALID_ACTOR`, `INVALID_ID`, `VALIDATION_ERROR`, `LOCK_TIMEOUT`, `NOT_INITIALIZED`.

Implementation: wrap each command's logic. On exception, catch and format. A shared `json_output(ok, data=None, error=None, warnings=None)` utility function helps keep this consistent.

Errors with `--json` should still exit non-zero but print JSON to stdout (not stderr). Without `--json`, errors go to stderr.

### 8.2 `--compact` and `--quiet`

- `--compact`: for `list` and `show`, output only the compact view fields (section 6.2).
- `--quiet`: print only the primary result (task ID for create, "ok" for update/status/assign, etc.). Useful for scripting.
- If both `--quiet` and `--json` are provided, `--json` takes precedence (not an error — just ignore `--quiet`).

### 8.3 Attribution flags

These are common to all write commands:
- `--actor` (required for write commands): the entity performing the action. Validate format.
- `--model` (optional): model identifier, stored in event's `agent_meta.model`.
- `--session` (optional): session identifier, stored in event's `agent_meta.session`.

If `--actor` is missing on a write command, error: "--actor is required".

---

## Deferred to follow-up prompts

These are explicitly NOT part of this prompt:

- **Dashboard** (`lattice dashboard`) — separate prompt. The `dashboard/` package remains a stub.
- **Git integration** (`git-hook` command, post-commit hook) — deferred. The `git_event` type exists in the event schema but no CLI command produces it yet.
- **OTel tracing flags** (`--trace-id`, `--span-id`, `--parent-span-id`) — the event schema supports `otel` fields, but no CLI flags pass them yet. Trivial to add later.
- **WIP limit warnings** — config stores limits, `get_wip_limit()` exists, but no counting/warning on `lattice status` yet.
- **Derived incoming relationships** in `lattice show` — only outgoing relationships are displayed. Scanning all tasks for incoming edges is a v1 query concern.

---

## Tests

Organize tests to mirror the source structure. Existing tests (`test_cli/test_init.py`, `test_core/test_config.py`, `test_storage/test_fs.py`, `test_storage/test_root_discovery.py`) should not be broken — extend, don't rewrite.

Add test files as needed per module. Don't over-split — a single `test_cli/test_commands.py` covering multiple commands is fine if it stays readable. A single `test_core/test_events.py` covering event creation and serialization is fine.

**Extend `conftest.py`** with:
- A `cli_runner` fixture wrapping Click's CliRunner.
- A fixture that creates a populated `.lattice/` with several tasks in different states, relationships, and artifacts — for testing `list`, `show`, `doctor`.

**Critical test patterns** (from CLAUDE.md):
- **Event-first verification**: after each write command, verify the event exists in the JSONL and the snapshot exists on disk. Simulate a "crash" (write event, don't write snapshot) and verify rebuild recovers.
- **Idempotency**: same ID + same payload = success. Same ID + different payload = error.
- **Rebuild determinism**: rebuild produces byte-identical snapshots.
- **Concurrent safety**: not required in tests for this prompt (would need threading/multiprocessing), but the lock infrastructure should be exercised.

Use `CliRunner` for CLI integration tests. Use `tmp_path` for filesystem isolation. Use `monkeypatch` for env vars and timestamps (freeze time for deterministic tests).

**Time determinism in tests**: many tests need predictable timestamps. Use `monkeypatch` to patch the timestamp generation function so events and snapshots have known timestamps. This makes assertions on `created_at`, `updated_at`, and `ts` fields reliable.

---

## Conventions to follow

- **JSON snapshots**: `json.dumps(data, sort_keys=True, indent=2) + "\n"`
- **JSONL events**: `json.dumps(event, sort_keys=True, separators=(",", ":")) + "\n"`
- **Atomic writes** for all JSON files: temp file in same directory, fsync, rename
- **JSONL appends**: open in append mode, write line, flush, fsync (inside held lock)
- **Layer boundaries**: `core/` has no filesystem calls, `storage/` handles I/O, `cli/` wires them together
- **Error output**: stderr for humans, stdout for --json
- **Exit codes**: 0 for success, 1 for user errors (invalid input, not found, conflict), 2 for system errors (lock timeout, I/O failure)
- **Forward compatibility**: when reading JSON/JSONL, tolerate unknown fields (don't fail on extra keys)

---

## What NOT to do

- Don't implement v1+ features: hash chains, encryption, cycle detection, index/SQLite, multiple workflows, agent registry
- Don't add dependencies beyond click, python-ulid, filelock (runtime) and pytest, ruff (dev)
- Don't implement `lattice config` command or config mutation events
- Don't implement `lattice unarchive`
- Don't implement CI/CD integration, alerting, or process management
- Don't implement auth or multi-user access control
- Don't implement `lattice note` command (notes are direct file edits)
- Don't implement the dashboard — it's a separate follow-up prompt
- Don't implement git hook commands — deferred
- Don't implement OTel tracing CLI flags — deferred
- Don't implement WIP limit warning logic — deferred

---

## Validation

After implementation, run the full suite:

```bash
uv pip install -e ".[dev]"
uv run pytest -v
uv run ruff check src/ tests/
uv run ruff format --check src/ tests/
```

Then smoke test the full workflow:

```bash
# Setup
uv run lattice init --path /tmp/lattice-smoke
cd /tmp/lattice-smoke

# Create tasks
uv run lattice create "Fix login redirect" --type bug --priority high --actor human:atin
uv run lattice create "Update dependencies" --type chore --actor human:atin
uv run lattice create "Design new dashboard" --type spike --priority medium --actor agent:claude

# Capture a task ID from create output for subsequent commands (or use lattice list)
uv run lattice list

# Status transitions
uv run lattice status <task1> ready --actor human:atin
uv run lattice status <task1> in_progress --actor agent:claude

# Assignment
uv run lattice assign <task1> agent:claude --actor human:atin

# Comments
uv run lattice comment <task1> "Starting work on OAuth callback handling" --actor agent:claude

# Relationships
uv run lattice link <task1> blocks <task2> --actor human:atin

# Artifacts
echo "debug output here" > /tmp/debug.log
uv run lattice attach <task1> /tmp/debug.log --title "OAuth debug log" --actor agent:claude

# Custom events
uv run lattice log <task1> x_deployment_started --data '{"env": "staging"}' --actor agent:claude

# Querying
uv run lattice show <task1>
uv run lattice list --status in_progress
uv run lattice list --type bug --json

# Archive
uv run lattice archive <task2> --actor human:atin
uv run lattice list  # task2 should not appear

# Integrity
uv run lattice doctor
uv run lattice rebuild --all
uv run lattice doctor  # should pass clean

# Idempotency test
uv run lattice create "Idempotent task" --id task_01JTEST00000000000000000 --actor human:atin
uv run lattice create "Idempotent task" --id task_01JTEST00000000000000000 --actor human:atin  # should succeed
uv run lattice create "Different title" --id task_01JTEST00000000000000000 --actor human:atin  # should fail with conflict

# Cleanup
cd -
rm -rf /tmp/lattice-smoke /tmp/debug.log
```

All tests should pass. Ruff should be clean. All smoke test commands should produce the expected output.
