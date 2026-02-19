# Storage Layer

## Purpose

`src/lattice/storage/` implements filesystem persistence, lock discipline, and
durability primitives.

## Filesystem Root and Discovery

`src/lattice/storage/fs.py`:

- root folder constant: `.lattice/`
- root override env var: `LATTICE_ROOT`
- `find_root()` walks upward like git if env var is not set
- `ensure_lattice_dirs()` creates canonical directory tree

## Atomic Snapshot Writes

`atomic_write(path, content)` performs:

1. write to same-directory temp file
2. `fsync` temp file
3. atomic replace (`os.replace`)
4. parent directory `fsync`

This prevents torn snapshot writes and improves crash safety.

## JSONL Event Appends

`jsonl_append(path, line)`:

- assumes caller already holds lock
- appends newline-terminated JSON record
- flushes + `fsync`s file and parent directory
- defensively inserts separator newline if previous line was truncated

## Locking

`src/lattice/storage/locks.py` provides:

- `lattice_lock()` for single lock
- `multi_lock()` for multiple keys, sorted deterministically

Deterministic ordering prevents deadlocks for composite writes.

## Canonical Write Operations

`src/lattice/storage/operations.py` contains shared write paths used by CLI and
dashboard:

- `write_task_event()`
- `write_resource_event()`
- `resource_write_context()` for read-check-write critical sections

Task write path is event-first, then snapshot write, then hook execution.

## Hooks

`src/lattice/storage/hooks.py` runs shell hooks after writes are durable:

- catch-all `post_event`
- event-specific hooks
- transition hooks for status changes

Hook errors are logged to stderr and do not fail the originating command.

## Non-Authoritative Files

Plans and notes are intentionally outside event sourcing:

- `.lattice/plans/<task_id>.md`
- `.lattice/notes/<task_id>.md`

They are supplementary docs, not authoritative state. Corruption there does not
break rebuild invariants.

## Recovery Model

If snapshots drift, `lattice rebuild` replays event logs to regenerate snapshots,
rebuild lifecycle log, and regenerate short ID index.

For storage bugs, verify lock usage and write order first.
