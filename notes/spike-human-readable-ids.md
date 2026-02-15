# Spike: Human-Readable Task Identifiers

> Design document for adding short, human-readable slugs (e.g., LATT-1, LATT-42) as an alias layer on top of ULIDs.

**Date:** 2026-02-15
**Type:** Spike / Design
**Status:** Draft

---

## 1. Problem Statement

ULIDs are excellent for uniqueness, time-ordering, and filesystem safety, but they are unusable by humans. A typical task ID looks like:

```
task_01KHG6H668GB96NCWR8QNRM5R3
```

This is 33 characters of opaque noise. Humans cannot:
- Remember them between commands
- Dictate them in conversation
- Spot them in logs at a glance
- Type them without copy-paste

Every competing tool (Linear, Jira, GitHub Issues) uses short sequential IDs (LIN-42, PROJ-7, #123). Lattice needs the same ergonomics while preserving ULIDs as the internal stable identifier.

---

## 2. Design Principles

1. **ULIDs remain the source of truth.** Slugs are an alias layer. All on-disk references (events, relationships, artifact refs) continue to use ULIDs. If the slug index is lost, `rebuild` regenerates it.
2. **Slugs are assigned at creation time and never change.** A task's slug is permanent. No recycling, no renumbering.
3. **The mapping is deterministic from events.** Given the same event stream replayed in order, the same slugs are assigned. This is required for `rebuild` to work.
4. **Minimal new machinery.** One new file, one new config field, one new core module. No database, no complex indexing.

---

## 3. Slug Format

```
<PREFIX>-<SEQUENTIAL_NUMBER>
```

Examples:
- `LATT-1`
- `LATT-42`
- `PRJ-137`
- `OPS-9`

### Rules

- **Prefix:** 1-8 uppercase ASCII letters. Configurable per project. Default: `LATT`.
- **Separator:** Always a hyphen (`-`).
- **Number:** Monotonically increasing positive integer starting at 1. No zero-padding (humans do not expect it; sorting is by number, not string).
- **Case:** The prefix is always uppercase. CLI resolution is case-insensitive (`latt-1` resolves to `LATT-1`).

### Why not auto-derive prefix from the directory name?

Tempting, but fragile. Directory names change, contain spaces/special chars, and are inconsistent. An explicit config value is clearer and avoids surprises when someone renames a folder.

---

## 4. Storage Design

### 4.1 Counter: in `config.json`

Add a new top-level key to `config.json`:

```json
{
  "schema_version": 1,
  "slug_prefix": "LATT",
  "slug_next": 6,
  ...existing fields...
}
```

- `slug_prefix` (string): The project prefix. Set at `lattice init` time. Immutable after first task creation (changing it would break all existing slugs).
- `slug_next` (int): The next number to assign. Starts at 1. Incremented atomically on each `task_created` event.

**Why config.json and not a separate counter file?**

- Config is already loaded on every write command. No extra I/O.
- The counter is small (one integer). It does not justify its own file.
- Config is already atomically written.
- A separate counter file would need its own lock key and atomic write path for marginal benefit.

**Tradeoff acknowledged:** Config is described as "rarely changing" in the current design. Adding a counter that increments on every task creation changes this character. However, the alternative (a separate file) adds complexity without proportional benefit. The config write is already atomic, and the additional data is a single integer.

### 4.2 Slug stored in the task snapshot

Add a `slug` field to the task snapshot:

```json
{
  "schema_version": 1,
  "id": "task_01KHG6H668GB96NCWR8QNRM5R3",
  "slug": "LATT-1",
  "title": "Implement human-readable IDs",
  ...
}
```

- The slug is assigned during `task_created` event processing and stored on the snapshot.
- It is **not** in the event data itself (events are ULID-referenced; the slug is a presentation concern).
- On `rebuild`, slugs are reassigned from events in chronological order.

**Why store it on the snapshot and not in a separate index file?**

Two reasons:
1. The snapshot is already the "fast read" path. Putting the slug there means `lattice list` and `lattice show` need zero extra I/O.
2. A separate index file (slug -> ULID mapping) is still useful for resolution (see 4.3), but the snapshot is the authoritative assignment.

### 4.3 Index file: `.lattice/slug_index.json`

A derived, rebuildable JSON file mapping slugs to ULIDs:

```json
{
  "LATT-1": "task_01KHG6H668GB96NCWR8QNRM5R3",
  "LATT-2": "task_01KHG6XMTN5P3DN76G1BJ2AXR9",
  "LATT-3": "task_01KHG79E3TRE6K0HK4Z7P93W4A"
}
```

Properties:
- **Derived, not authoritative.** Rebuildable from snapshots (both active and archived). If it disagrees with snapshots, snapshots win.
- **Includes archived tasks.** Slugs are never recycled, so archived task slugs must remain in the index to prevent reassignment and to support `lattice show LATT-42` for archived tasks.
- **Atomically written.** Same `atomic_write` pattern as snapshots.
- **Updated on task creation and rebuild.** Not updated on other mutations (status changes, etc.) since the slug never changes after assignment.

**Why a flat JSON file and not a JSONL append log?**

The index is small (one entry per task ever created, including archived). At v0 scale targets (10,000 tasks), the file is roughly 700KB -- comfortably small. A flat file supports O(1) lookup by slug without scanning. JSONL would require scanning the whole file on every resolution.

---

## 5. Slug Assignment Logic

### 5.1 Normal creation path (`lattice create`)

```
1. Load config.json (get slug_prefix and slug_next)
2. Assign slug = f"{slug_prefix}-{slug_next}"
3. Increment slug_next in config
4. Store slug in the task_created event's snapshot
5. Update slug_index.json (add new mapping)
6. Write config.json atomically (with incremented counter)
```

Steps 4-6 happen under the existing task write lock. The config write is a new addition to the write path, requiring a new lock key (`config`).

### 5.2 Idempotent retry with caller-supplied ID

If `--id task_...` is supplied and the task already exists, return the existing task (with its existing slug). No new slug is assigned. This preserves the existing idempotency contract.

If `--id task_...` is supplied and the task does NOT exist, assign the next slug as normal.

### 5.3 Rebuild

During `lattice rebuild`, slugs are reassigned by replaying `task_created` events in timestamp order:

```
1. Collect all task_created events (active + archived) sorted by timestamp
2. Reset counter to 1
3. For each task_created event in order:
   a. Assign slug = f"{prefix}-{counter}"
   b. Store slug on rebuilt snapshot
   c. Increment counter
4. Write slug_index.json from all assignments
5. Update config.json with final slug_next value
```

This guarantees determinism: the same event stream always produces the same slug assignments. ULIDs are time-ordered, so replaying in ULID order is equivalent to replaying in creation order.

### 5.4 Unarchive

When a task is unarchived, its slug comes with it (it is part of the snapshot). The slug_index.json already contains the entry (archived tasks are included). No special handling needed.

### 5.5 Archive

When a task is archived, its slug remains in slug_index.json. The slug is never freed for reuse.

---

## 6. CLI Resolution

### 6.1 Where resolution happens

A single new function in `core/ids.py` or a new `core/slugs.py` module:

```python
def resolve_task_identifier(lattice_dir: Path, identifier: str) -> str:
    """Resolve a task identifier to a ULID.

    Accepts:
    - A full ULID (task_01KHG...) -- returned as-is
    - A slug (LATT-42) -- looked up in slug_index.json
    - A bare number (42) -- interpreted as {prefix}-42

    Returns the ULID string.
    Raises TaskNotFoundError if the slug does not resolve.
    """
```

### 6.2 Integration point

In `cli/helpers.py`, add a wrapper that CLI commands call instead of directly validating with `validate_id`:

```python
def resolve_task_id_or_exit(lattice_dir: Path, identifier: str, is_json: bool) -> str:
    """Resolve identifier (ULID or slug) to a ULID, or exit with error."""
```

Every command that currently takes a `task_id` argument would call this function first. The underlying core and storage layers continue to work exclusively with ULIDs.

### 6.3 Bare number shorthand

For maximum convenience, `lattice show 42` should resolve to `LATT-42` (using the project's configured prefix). This avoids typing the prefix for the most common case.

### 6.4 Ambiguity rules

- If the identifier matches `<PREFIX>-<NUMBER>` pattern (case-insensitive), treat it as a slug.
- If the identifier matches `task_<ULID>` pattern, treat it as a ULID.
- If the identifier is a bare positive integer, treat it as `{slug_prefix}-{number}`.
- Otherwise, error with a helpful message showing valid formats.

No ambiguity is possible because ULIDs never look like `LATT-42` and slugs never look like `task_01KHG...`.

---

## 7. Display Format

### 7.1 `lattice list` (human mode)

Currently:
```
task_01KHG6H668GB96NCWR8QNRM5R3  backlog  medium  task  "Implement auth"  unassigned
```

Proposed:
```
LATT-1  backlog  medium  task  "Implement auth"  unassigned
```

The slug replaces the ULID as the primary identifier in human output. The ULID is still available via `--full` or `--json`.

### 7.2 `lattice show` (human mode)

Currently:
```
task_01KHG6H668GB96NCWR8QNRM5R3  "Implement auth"
Status: backlog  Priority: medium  Type: task
```

Proposed:
```
LATT-1  "Implement auth"
ID: task_01KHG6H668GB96NCWR8QNRM5R3
Status: backlog  Priority: medium  Type: task
```

The slug is the headline. The ULID is shown on the next line for reference. Relationship display also uses slugs:

```
Relationships (outgoing):
  blocks -> LATT-3 "Fix deployment pipeline"

Relationships (incoming):
  LATT-7 "Design API schema" --[depends_on]--> this
```

### 7.3 `lattice create` output

Currently:
```
Created task task_01KHG6H668GB96NCWR8QNRM5R3 "Implement auth"
```

Proposed:
```
Created LATT-1 "Implement auth"
  id: task_01KHG6H668GB96NCWR8QNRM5R3
```

### 7.4 JSON output

JSON output includes both. The `slug` field is added to the snapshot:

```json
{
  "ok": true,
  "data": {
    "id": "task_01KHG6H668GB96NCWR8QNRM5R3",
    "slug": "LATT-1",
    "title": "Implement auth",
    ...
  }
}
```

### 7.5 `--quiet` mode

Currently outputs the ULID. Should continue to output the ULID (scripts and agents depend on stable IDs). Add a `--quiet-slug` flag if slug-only quiet output is needed, or document that `--quiet` always returns ULIDs.

### 7.6 Dashboard

The dashboard should display slugs as the primary identifier in board view and list view, with the ULID available on hover or in the detail pane.

---

## 8. Example CLI Interactions

### Creating a task
```bash
$ lattice create "Fix login redirect bug" --actor human:atin --type bug
Created LATT-6 "Fix login redirect bug"
  id: task_01KHG9XYZ123ABC456DEF789GH
  status: backlog  priority: medium  type: bug
```

### Listing tasks
```bash
$ lattice list
LATT-1  in_progress  high    task   "Implement auth"          agent:claude
LATT-2  backlog      medium  task   "Design API schema"       unassigned
LATT-3  blocked      high    bug    "Fix deployment pipeline"  human:atin
LATT-4  done         medium  chore  "Update dependencies"     agent:codex
LATT-5  review       high    spike  "Evaluate caching layer"  human:atin
LATT-6  backlog      medium  bug    "Fix login redirect bug"  unassigned
```

### Showing a task (by slug)
```bash
$ lattice show LATT-3
LATT-3  "Fix deployment pipeline"
ID: task_01KHG79E3TRE6K0HK4Z7P93W4A
Status: blocked  Priority: high  Type: bug
Assigned: human:atin  Created by: agent:claude
Created: 2026-02-15T03:45:00Z  Updated: 2026-02-15T04:12:00Z

Relationships (outgoing):
  depends_on -> LATT-1 "Implement auth"

Events (latest first):
  2026-02-15T04:12:00Z  status_changed  ready -> blocked  by human:atin
  2026-02-15T03:45:00Z  task_created    by agent:claude
```

### Using bare number shorthand
```bash
$ lattice show 3
LATT-3  "Fix deployment pipeline"
...
```

### Changing status (by slug)
```bash
$ lattice status LATT-6 ready --actor human:atin
Status: backlog -> ready
```

### Linking tasks (by slug)
```bash
$ lattice link LATT-6 depends_on LATT-1 --actor human:atin
Added relationship: LATT-6 depends_on LATT-1
```

### JSON output preserves both identifiers
```bash
$ lattice show LATT-1 --json
{
  "ok": true,
  "data": {
    "id": "task_01KHG6H668GB96NCWR8QNRM5R3",
    "slug": "LATT-1",
    "title": "Implement auth",
    ...
  }
}
```

---

## 9. Concurrency Considerations

### 9.1 Counter atomicity

Two agents creating tasks simultaneously must not get the same slug number. The solution: the config file write (which includes `slug_next`) is protected by a `config` lock key. The write path becomes:

```
Lock: [config, events_{task_id}, tasks_{task_id}]  (sorted)
1. Read config.json, get slug_next
2. Assign slug
3. Append event
4. Write snapshot (includes slug)
5. Update slug_index.json
6. Write config.json (with incremented slug_next)
Unlock
```

Both the config read and write happen inside the lock, preventing TOCTOU races.

### 9.2 Lock ordering

The new `config` lock key sorts before all `events_*` and `tasks_*` keys, so it is always acquired first. This preserves the deterministic ordering invariant and prevents deadlocks.

### 9.3 Slug index write

The slug_index.json write does not need its own lock because it only happens inside the create path (which already holds the config lock) and during rebuild (which is a single-threaded offline operation). If concurrent creates both try to update slug_index.json, they are serialized by the config lock.

---

## 10. Migration Path for Existing Tasks

Existing Lattice instances have tasks with ULIDs but no slugs. Migration:

1. **On first `lattice create` after upgrade:** If `config.json` lacks `slug_prefix`, prompt (or use default `LATT`). Add `slug_prefix` and `slug_next` to config.
2. **Run `lattice rebuild all`:** This replays all events, assigning slugs in creation order. All existing task snapshots gain `slug` fields, and `slug_index.json` is generated.
3. **Alternatively, `lattice doctor --fix`:** Could detect missing slugs and assign them in a lighter-weight pass (without full event replay).

The migration is non-destructive: it only adds new fields and files. No existing data is modified or removed.

---

## 11. Edge Cases and Failure Modes

### 11.1 Counter corruption

If `slug_next` in config.json is somehow wrong (lower than expected), `rebuild` will reset it to the correct value. Between detection and rebuild, new tasks might get duplicate slugs. Mitigation: `lattice doctor` checks that all slugs are unique and that `slug_next` is greater than the highest assigned slug number.

### 11.2 slug_index.json missing or corrupt

The index is derived. Delete it and run `lattice rebuild all` to regenerate. Or: a lighter-weight `lattice reindex` command could regenerate it from snapshots alone (no event replay needed, since slugs are stored on snapshots).

### 11.3 Gaps in numbering

Normal and expected. If a task is created and later its creation is somehow rolled back (e.g., partial write crash before snapshot), the counter has already advanced. The next task gets the next number. Gaps do not indicate data loss -- `doctor` can verify this.

### 11.4 Prefix collision across projects

Two Lattice instances in different directories can use the same prefix. This is intentional -- slugs are project-scoped, just like task data. If a user works across multiple projects, they should configure different prefixes (e.g., `API-`, `WEB-`, `OPS-`).

### 11.5 Very high numbers

At v0 scale targets (10,000 tasks), slugs reach `LATT-10000`. This is still short and readable. No padding or rollover is needed.

---

## 12. Changes NOT Included in This Design

### 12.1 Rename "task" to "item"

The original spike prompt mentioned a potential rename from "task" to "item." This is a separate, larger refactor that touches every file in the codebase. It should be evaluated independently. The slug design is agnostic to this rename -- if "task" becomes "item," slugs work identically (they alias ULIDs regardless of the entity name).

### 12.2 Slug for artifacts and events

This design covers tasks only. Artifacts and events do not need human-readable slugs in v0 (they are referenced far less frequently by humans). If needed later, the same pattern applies: add `art_slug_prefix`, `art_slug_next`, etc.

---

## 13. Implementation Plan (Estimated)

| Step | Description | Files Touched |
|------|-------------|---------------|
| 1 | Add `slug_prefix` and `slug_next` to config schema | `core/config.py` |
| 2 | Add `slug` field to snapshot initialization | `core/tasks.py` |
| 3 | Create `core/slugs.py` with assignment and resolution logic | New file |
| 4 | Create `slug_index.json` read/write in storage layer | `storage/fs.py` |
| 5 | Add `resolve_task_id_or_exit` to CLI helpers | `cli/helpers.py` |
| 6 | Update `lattice create` to assign slugs | `cli/task_cmds.py` |
| 7 | Update all commands to accept slugs as task identifiers | `cli/task_cmds.py`, `cli/query_cmds.py`, `cli/link_cmds.py`, `cli/archive_cmds.py`, `cli/artifact_cmds.py` |
| 8 | Update human output to display slugs | `cli/query_cmds.py`, `cli/helpers.py` |
| 9 | Update `lattice rebuild` to assign slugs and write index | `cli/integrity_cmds.py` |
| 10 | Update `lattice doctor` to check slug consistency | `cli/integrity_cmds.py` |
| 11 | Update `lattice init` to accept `--prefix` | `cli/main.py` |
| 12 | Update dashboard to display slugs | `dashboard/server.py`, `dashboard/static/index.html` |
| 13 | Tests for all of the above | `tests/test_core/`, `tests/test_cli/` |
| 14 | Append decision to `Decisions.md` | `Decisions.md` |

---

## 14. Recommendation

**Proceed with this design.** The approach is minimal, deterministic, and fully consistent with Lattice's existing invariants:

- Events remain authoritative (slugs are derived from event replay order).
- Writes remain atomic and lock-protected (counter is updated under lock).
- Rebuild works (slugs are regenerated deterministically).
- No new dependencies (pure Python, file-based).
- Backward compatible (existing ULIDs continue to work everywhere; slugs are additive).

The biggest open question is whether the counter belongs in `config.json` (recommended above) or a dedicated `.lattice/counter.json` file. The config approach is simpler (fewer files, fewer locks) but changes the "rarely edited" nature of config. Either works; the config approach is recommended for simplicity.

---

## 15. Alternatives Considered

### A. Hash-based short IDs (e.g., first 6 chars of ULID)

- Pro: No counter, no state to manage.
- Con: Not sequential, not memorable, collisions at scale, no project scoping. Rejected.

### B. Title-based slugs (e.g., "implement-auth")

- Pro: Self-describing.
- Con: Titles change, duplicates are common, slugs would need dedup logic (e.g., "implement-auth-2"), and they are long. Rejected.

### C. Separate counter file (`.lattice/counter.json`)

- Pro: Keeps config.json stable.
- Con: One more file to manage, one more lock key, one more thing to break. Marginal benefit for real complexity cost. Not recommended, but acceptable if config purity is prioritized.

### D. Counter in slug_index.json (combined file)

- Pro: One file instead of two for slug state.
- Con: The index is a derived file (rebuildable), but the counter is authoritative state. Mixing derived and authoritative data in one file violates Lattice's design principle. Rejected.

### E. Event-embedded slugs

- Pro: Slug assignment becomes part of the event record.
- Con: Slugs are a presentation concern, not a data integrity concern. Embedding them in events couples display logic to the append-only log. Events should reference tasks by ULID. Rejected.
