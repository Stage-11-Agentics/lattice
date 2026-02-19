# Snapshot Materialization

## Purpose

Snapshots are denormalized, query-friendly task documents in
`.lattice/tasks/<task_id>.json`. They exist for fast reads. They are derived
from events and can be rebuilt.

Primary implementation lives in `src/lattice/core/tasks.py`.

## Single Materialization Path

`apply_event_to_snapshot(snapshot, event)` is the canonical reducer used by both:

- normal write path
- rebuild path (`lattice rebuild`)

Important invariant: snapshot timestamps (`updated_at`, `done_at`) derive from
event timestamps, not wall clock, which preserves deterministic rebuilds.

## Mutation Registry

Event handlers are registered via `_register_mutation("event_type")` and applied
through `_apply_mutation()`.

Representative handlers:

- `status_changed`:
  - updates `status`
  - maintains `done_at`
  - increments `reopened_count` for backward transitions
- `assignment_changed` -> updates `assigned_to`
- `field_updated` -> guarded generic field updates (with protected fields)
- `comment_*` -> maintains `comment_count` and `evidence_refs` role entries
- `artifact_attached` -> deduplicated `evidence_refs` entries
- relationship/branch handlers -> append/remove structured records

## Evidence Model (Current)

Snapshot evidence is unified under `evidence_refs` with `source_type` and `role`.

Role helpers:

- `get_artifact_roles()`
- `get_comment_role_refs()`
- `get_evidence_roles()`

These include legacy fallbacks (`artifact_refs`, `comment_role_refs`) so older
snapshots continue to function until rebuilt.

## Epic Derived Status

`compute_epic_derived_status()` infers epic status from subtask states. Epics are
containers; direct status changes are blocked in CLI and dashboard write paths.

## Rebuild Path

`src/lattice/cli/integrity_cmds.py` handles rebuild:

- `_rebuild_task()` replays task events
- `_rebuild_lifecycle_log()` regenerates lifecycle stream from per-task logs
- `_rebuild_id_index()` regenerates `ids.json`
- resources are rebuilt from their own event logs

Rebuild is the recovery mechanism after partial writes or snapshot drift.

## Operational Rules

- Never patch snapshot files manually to represent state changes
- Add/change behavior by defining events and reducer handlers
- If reducer behavior changes, verify rebuild determinism tests

For correctness discussions, reason in terms of event streams and reducer logic,
not current snapshot shape alone.
