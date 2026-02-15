# Master Plan: Human-Friendly Task IDs for Lattice

> Synthesized from three independent analyses (Claude Opus, Codex, Gemini 3 Pro).
> Each step validated against the actual source code.

---

## Design Decisions (to record in Decisions.md)

### D1: ULIDs remain canonical; short IDs are aliases
- ULIDs (`task_01...`) remain the internal primary key for filenames, events, locks, and relationships.
- Short IDs (`LAT-42`) are a human-facing alias layer.
- **Rationale:** Changing the canonical ID would require rewriting every event, snapshot, lock, and filename. The alias approach is additive and non-breaking.

### D2: Short ID stored in snapshot and events; index file is derived
- `short_id` is a first-class field on task snapshots (like `title`, `status`).
- Included in `task_created` event data for new tasks.
- A new event type `task_short_id_assigned` handles retroactive assignment (migration).
- `.lattice/ids.json` is a **derived** index (like snapshots) — rebuildable from events.
- **Rationale:** Events are authoritative. The index is a read optimization, not a source of truth.

### D3: Project code in config.json; counter in ids.json
- `project_code` (1-5 uppercase letters) stored in `.lattice/config.json`.
- `next_seq` counter stored in `.lattice/ids.json` alongside the mapping.
- Both are rebuildable from events.
- **Rationale:** Config is the natural place for project-level settings. The counter lives with the mapping it governs.

### D4: Existing short IDs are immutable
- Once assigned, a task's short ID never changes — even if the project code is changed later.
- Changing project code only affects future task creation.
- **Rationale:** References to `LAT-7` in comments, docs, and conversations must remain stable forever.

### D5: New built-in event type for retroactive assignment
- `task_short_id_assigned` (not `field_updated`) for migration of existing tasks.
- **Rationale:** Using `field_updated` would conflict with `short_id` being a protected field. A dedicated event type makes replay semantics explicit and avoids special-case logic in the mutation path.

---

## On-Disk Changes

### New/modified files in `.lattice/`

```
.lattice/
├── config.json          # MODIFIED: add "project_code" field
├── ids.json             # NEW: derived index file
│   {
│     "schema_version": 1,
│     "next_seq": 43,
│     "map": {
│       "LAT-1": "task_01ABC...",
│       "LAT-2": "task_01DEF..."
│     }
│   }
└── ids/                 # NOT USED (rejected: per-file aliases add complexity without benefit at v0 scale)
```

### Why a single `ids.json` (not per-alias files)

Both Gemini (per-file aliases) and Codex (single JSON with bidirectional maps) proposed index approaches. The master plan uses a **simplified single file**:
- One file = one atomic write = simpler rebuild
- At v0 scale (hundreds of tasks, not millions), loading a single JSON file is negligible
- Bidirectional map is unnecessary — snapshots already contain `short_id`, so reverse lookup (ULID → short ID) comes from the snapshot itself
- The `map` only needs short_id → ULID for CLI resolution

---

## Implementation Steps

### Step 1: Data Model — Config, IDs, Events, Snapshots
**Files:** `core/config.py`, `core/ids.py`, `core/events.py`, `core/tasks.py`

**config.py:**
- Add `project_code` to `LatticeConfig` TypedDict (optional, `str | None`)
- Add `validate_project_code(code: str) -> bool` — 1-5 uppercase ASCII letters
- No change to `default_config()` (project_code defaults to absent/None)

**ids.py:**
- Add `SHORT_ID_RE = re.compile(r"^[A-Z]{1,5}-\d+$")`
- Add `validate_short_id(s: str) -> bool`
- Add `parse_short_id(s: str) -> tuple[str, int]` — returns (prefix, number)
- Add `is_short_id(s: str) -> bool` — quick check for CLI resolution

**events.py:**
- Add `"task_short_id_assigned"` to `BUILTIN_EVENT_TYPES`
- NOT a lifecycle event (doesn't go to `_lifecycle.jsonl`)

**tasks.py:**
- In `_init_snapshot`: read `data.get("short_id")` into snapshot
- In `_apply_mutation`: add handler for `task_short_id_assigned` that sets `snap["short_id"] = data["short_id"]`
- Add `"short_id"` to `PROTECTED_FIELDS` (prevents mutation via `lattice update`)
- In `compact_snapshot`: include `short_id`

**Edge cases:**
- Old snapshots without `short_id` render as `None` — all display logic must handle this gracefully
- `task_created` events from before this feature won't have `short_id` in data — `_init_snapshot` handles this via `.get()` defaulting to `None`

**Dependencies:** None — this is the foundation.

---

### Step 2: Storage — Index File Helpers
**Files:** `storage/fs.py` (add to existing), `storage/short_ids.py` (new module)

**fs.py:**
- Update `ensure_lattice_dirs` — no new subdirectories needed (ids.json is a file, not a dir)

**storage/short_ids.py (NEW):**
- `load_id_index(lattice_dir: Path) -> dict` — load and parse `.lattice/ids.json`, return empty default if missing
- `save_id_index(lattice_dir: Path, index: dict) -> None` — atomic write
- `allocate_short_id(lattice_dir: Path, project_code: str) -> tuple[str, dict]` — lock, increment counter, return (short_id, updated_index). Uses file lock `ids_json` in `.lattice/locks/`
- `resolve_short_id(lattice_dir: Path, short_id: str) -> str | None` — return ULID or None
- `register_short_id(index: dict, short_id: str, task_ulid: str) -> dict` — add mapping to index dict (pure, no I/O)

**Lock ordering:** When `create` needs both task lock and ID index lock, acquire `ids_json` lock FIRST, then task lock. Deterministic ordering prevents deadlocks.

**Edge cases:**
- `ids.json` doesn't exist (pre-feature project) → treat as empty index
- Counter file corruption → `rebuild` regenerates from events

**Dependencies:** Step 1.

---

### Step 3: `lattice init` — Project Code Setup
**Files:** `cli/main.py`, tests

**Changes:**
- Add `--project-code` option to `init` command
- If not provided via flag, prompt interactively (allow blank to skip)
- Validate with `validate_project_code`
- Write `project_code` to config.json
- Initialize `ids.json` with `{"schema_version": 1, "next_seq": 1, "map": {}}`

**Existing projects:** `init` is idempotent (skips if `.lattice/` exists). Setting project code on an existing project is handled by Step 4.

**Edge cases:**
- Non-interactive mode (piped input, agent usage) — blank project code means "skip for now"
- Existing tests that assert byte-identical config need updating for the new optional field

**Dependencies:** Steps 1-2.

---

### Step 4: Set Project Code on Existing Projects
**Files:** New `cli/config_cmds.py` or add to `main.py`, tests

**New command:** `lattice set-project-code <CODE>`
- Validates code format
- Writes `project_code` to config.json (atomic update)
- Initializes `ids.json` if it doesn't exist
- Prints instructions: "Run `lattice backfill-ids` to assign short IDs to existing tasks."

**Why a dedicated command (not just "edit config.json"):**
- Validates the code format
- Creates `ids.json` atomically
- Guides the user to backfill

**Edge cases:**
- Code already set and different → warn, require `--force` to change
- Code already set and same → no-op

**Dependencies:** Steps 1-3.

---

### Step 5: `lattice create` — Allocate Short IDs
**Files:** `cli/task_cmds.py`, tests

**Changes to `create` command:**
- After generating ULID, check config for `project_code`
- If project code exists:
  1. Call `allocate_short_id` (under lock) to get next seq number
  2. Format `short_id = f"{project_code}-{seq}"`
  3. Add `short_id` to `event_data` dict
  4. After event+snapshot write, register in `ids.json` index
- If no project code: skip (tasks created without short IDs)

**Idempotency:** If `--id` is provided and task already exists:
- Return existing task (existing behavior)
- Do NOT consume a new sequence number

**Output changes:**
- Human message: `Created task LAT-7 (task_01...) "My title"`
- Quiet mode: output `LAT-7` (short ID takes priority)
- JSON: snapshot now includes `short_id` field

**Edge cases:**
- Crash between event write and index write → index is stale, but `rebuild` fixes it
- Concurrent creates → lock on `ids_json` ensures sequential allocation

**Dependencies:** Steps 1-4.

---

### Step 6: CLI Resolver — Short ID → ULID Everywhere
**Files:** `cli/helpers.py`, `core/ids.py`

**New function in helpers.py:**
```python
def resolve_task_id(lattice_dir: Path, raw_id: str, is_json: bool, *, allow_archived: bool = False) -> str:
```
- If `raw_id` matches ULID pattern (`task_...`): return as-is
- If `raw_id` matches short ID pattern (`LAT-42`): lookup in `ids.json`, return ULID
- Case-insensitive on input, normalized to uppercase for lookup
- If not found: `output_error` with `NOT_FOUND`
- If invalid format: `output_error` with `INVALID_ID`

**Why centralized:** Every command that takes a task ID uses the same resolution logic. One function, one behavior, tested once.

**Dependencies:** Steps 1-2.

---

### Step 7: Wire Resolver Into All Commands
**Files:** Every CLI module that accepts `task_id`

**Commands to update:**
| Command | File | Notes |
|---------|------|-------|
| `update` | `task_cmds.py` | Replace `validate_id` check with `resolve_task_id` |
| `status` | `task_cmds.py` | Same |
| `assign` | `task_cmds.py` | Same |
| `comment` | `task_cmds.py` | Same |
| `show` | `query_cmds.py` | Same; also check archived |
| `event` | `query_cmds.py` | Same; also check archived |
| `link` | `link_cmds.py` | Both source AND target task IDs |
| `unlink` | `link_cmds.py` | Both source AND target |
| `attach` | `artifact_cmds.py` | Task ID arg |
| `archive` | `archive_cmds.py` | Active tasks only |
| `unarchive` | `archive_cmds.py` | Archived tasks only |

**Pattern:** In each command, replace:
```python
if not validate_id(task_id, "task"):
    output_error(...)
```
with:
```python
task_id = resolve_task_id(lattice_dir, task_id, is_json)
```

**Error messages:** Echo original user input alongside canonical ID when helpful:
`"Task LAT-42 (task_01ABC...) not found."`

**Edge cases:**
- `link` and `unlink` take TWO task IDs — both must be resolved before self-link detection
- `show` and `event` search both active and archived — resolver needs `allow_archived` flag

**Dependencies:** Step 6.

---

### Step 8: Output — Show Short IDs Prominently
**Files:** `cli/query_cmds.py`, `cli/task_cmds.py`, `cli/archive_cmds.py`

**`lattice list` (human mode):**
- Show `short_id` as the primary identifier column (left-most)
- Show ULID in a secondary column or omit from default view (add `--verbose` to show)
- Tasks without short IDs (pre-feature) fall back to showing ULID

**`lattice list` (JSON mode):**
- `compact_snapshot` already includes `short_id` from Step 1
- `id` field remains the ULID (backward compatible)

**`lattice show`:**
- Header line: `LAT-42 (task_01ABC...)` — short ID prominent, ULID for reference
- All other fields unchanged

**Success messages across all write commands:**
- Use short ID when available: `"Status: backlog -> in_implementation (LAT-42)"`

**`--quiet` mode:**
- `create` outputs short ID (or ULID if no short ID)
- Other commands: unchanged (`"ok"`)

**Edge cases:**
- Tasks with no `short_id` → display ULID only (graceful degradation)

**Dependencies:** Steps 5, 7.

---

### Step 9: `lattice rebuild` — Regenerate Index
**Files:** `cli/integrity_cmds.py`, `storage/short_ids.py`

**Changes to rebuild pipeline:**
1. Replay events as today → regenerate snapshots
2. NEW: After all snapshots are rebuilt, scan them for `short_id` fields
3. Build `ids.json` from scratch:
   - Collect all `(short_id, task_ulid)` pairs from snapshots
   - Compute `next_seq` as `max(seq_number for each prefix) + 1`
4. Atomic write `ids.json`

**Why derive from snapshots (not raw events):**
- Snapshots are already rebuilt at this point
- Avoids duplicating the event-replay logic
- `short_id` is in the snapshot because `_init_snapshot` and `_apply_mutation` handle it

**Edge cases:**
- Duplicate short IDs detected → emit error, fail rebuild (data corruption)
- Missing short IDs on some tasks → expected for pre-feature tasks, skip them
- Multiple project codes (code changed mid-project) → `next_seq` computed per-prefix

**Dependencies:** Steps 1-2.

---

### Step 10: `lattice doctor` — Alias Integrity Checks
**Files:** `cli/integrity_cmds.py`

**New checks:**
- `ids.json` exists if `project_code` is configured
- Every entry in `ids.json.map` points to an existing task snapshot
- Every task snapshot with `short_id` has a matching entry in `ids.json.map`
- No duplicate short IDs across active + archived tasks
- `next_seq` is greater than the max assigned sequence number
- Short ID format is valid (matches `SHORT_ID_RE`)

**`doctor --fix` behavior:**
- Regenerate `ids.json` from snapshots (same logic as rebuild)
- NEVER modifies events or snapshots

**Dependencies:** Steps 1-2, 9.

---

### Step 11: Migration — Backfill Existing Tasks
**Files:** New `cli/migration_cmds.py` or add to `integrity_cmds.py`

**New command:** `lattice backfill-ids [--code CODE]`
- If `--code` provided and config has no `project_code`: set it
- If config has `project_code`: use it (ignore `--code` unless it matches or `--force`)
- Scan all active + archived tasks missing `short_id`
- Sort by `created_at` then ULID (deterministic order)
- For each:
  1. Allocate next seq number
  2. Emit `task_short_id_assigned` event
  3. Update snapshot
  4. Register in `ids.json`
- Report: "Assigned LAT-1 through LAT-N to N existing tasks."

**Why deterministic order matters:**
- If two people run `backfill-ids` independently, they get the same assignments
- Sorted by creation time means the oldest task gets the lowest number (intuitive)

**Idempotency:**
- Tasks that already have `short_id` are skipped
- Running backfill twice is safe

**Edge cases:**
- Archived tasks need reads from `archive/tasks/` and event writes to `archive/events/`
- Partial failure mid-backfill → already-assigned tasks keep their IDs, counter reflects progress, re-run picks up where it left off

**Dependencies:** Steps 1-5, 9.

---

### Step 12: Dashboard — Display Short IDs
**Files:** `dashboard/static/index.html` (or equivalent), `dashboard/server.py`

**Changes:**
- Board cards show short ID prominently (LAT-42) instead of truncated ULID
- Task detail view shows both short ID and full ULID
- JSON API responses already include `short_id` from snapshot changes

**Dependencies:** Steps 1, 5.

---

### Step 13: Documentation
**Files:** `Decisions.md`, `ProjectRequirements_v1.md`, `CLAUDE.md`

**Decisions.md:** Append decisions D1-D5 from top of this plan.

**ProjectRequirements_v1.md:** Update:
- Identifier section: document short ID format and resolution
- Config section: document `project_code`
- New section on `ids.json` derived index

**CLAUDE.md:** Update:
- On-disk layout diagram (add `ids.json`)
- Note about short ID resolution in CLI commands

**Dependencies:** After implementation settles.

---

## Execution Order

```
Phase 1: Foundation (Steps 1-2)
  └─ Data model + storage helpers — no user-visible changes

Phase 2: New Project Flow (Steps 3-5)
  └─ init + create produce short IDs — new projects get them immediately

Phase 3: CLI Integration (Steps 6-8)
  └─ Resolution + output — short IDs work everywhere

Phase 4: Integrity (Steps 9-10)
  └─ Rebuild + doctor — system is self-healing

Phase 5: Migration (Step 11)
  └─ Backfill — existing projects can adopt

Phase 6: Polish (Steps 12-13)
  └─ Dashboard + docs
```

---

## Testing Strategy

### Unit tests (core/)
- `test_ids.py`: short ID validation, parsing, case normalization
- `test_config.py`: project code validation
- `test_events.py`: `task_short_id_assigned` serialization
- `test_tasks.py`: snapshot materialization with `short_id`, protected field enforcement

### Storage tests (storage/)
- `test_short_ids.py`: index load/save, allocation, resolution, concurrent allocation (threading)

### CLI integration tests (test_cli/)
- `test_init.py`: init with `--project-code`, init without
- `test_task_cmds.py`: create assigns short ID, idempotent create doesn't consume seq
- Every command file: accepts both ULID and short ID
- `test_output.py`: list/show display short IDs prominently
- `test_integrity_cmds.py`: rebuild regenerates `ids.json`, doctor detects corruption

### Migration tests
- `test_backfill.py`: backfill assigns IDs in creation order, idempotent re-run, handles archived tasks

### Rebuild determinism
- Delete `ids.json`, run rebuild, verify byte-identical output
- Create tasks, rebuild, verify short IDs survive

---

## Rejected Alternatives

| Alternative | Proposed By | Rejection Reason |
|-------------|-------------|------------------|
| Per-file aliases (`.lattice/aliases/LAT-42`) | Gemini | Adds filesystem overhead, complicates rebuild, no benefit at v0 scale |
| Bidirectional map in index | Codex | Reverse map (ULID→short) is redundant — snapshots already have `short_id` |
| `field_updated` for migration | Gemini | Conflicts with `short_id` being a protected field; dedicated event type is cleaner |
| Separate `short_ids.py` storage module | Codex | Acceptable but merged into a simpler approach — helpers in `storage/short_ids.py` is fine, just simplified schema |
| Required project code at init | Codex | Too breaking for existing workflows; optional with strong encouragement is better |
