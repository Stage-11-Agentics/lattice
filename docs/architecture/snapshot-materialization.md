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
- `comment_*` -> maintains `comment_count` and full `evidence_refs`
- `artifact_attached` -> deduplicated full `evidence_refs`
- `acceptance_criterion_*` -> maintains stable, ordered criteria plus immutable
  outcome revision history and retirement attribution
- relationship/branch handlers -> append/remove structured records
- `file_linked` -> appends to `linked_files` (objects with `path` and optional `reason`), deduplicates by path
- `file_unlinked` -> removes matching paths from `linked_files`

## Evidence Model (Current)

Snapshot evidence is unified under `evidence_refs` with `source_type`, nullable
`role`, and optional `criterion_ids`.

Role helpers:

- `get_artifact_roles()`
- `get_comment_role_refs()`
- `get_evidence_roles()`
- `get_artifact_evidence_refs()`

These include legacy fallbacks (`artifact_refs`, `comment_role_refs`) so older
snapshots continue to function until rebuilt.

`acceptance_criteria` is an additive schema-version-1 view. A missing field reads
as an empty list. Each record carries its stable task-local ID, current outcome
and revision, retirement state/attribution, and ordered revision history.
Compact snapshots expose only active and retired counts.

Criterion-linked evidence remains a neutral traceability record. Materialization
does not infer pass/fail or satisfaction, and completion-policy helpers continue
to inspect roles only.

## Rebuild Path

`src/lattice/cli/integrity_cmds.py` handles rebuild:

- `_rebuild_task()` strictly resolves active/archive authority and replays task events
- `_rebuild_lifecycle_log()` regenerates lifecycle stream from per-task logs
- `_rebuild_id_index()` regenerates `ids.json`
- resources are rebuilt from their own event logs

Rebuild is the recovery mechanism after partial writes or snapshot drift.
Malformed or divergent authoritative history is not repairable by rebuild and
is never overwritten.

## Operational Rules

- Never patch snapshot files manually to represent state changes
- Add/change behavior by defining events and reducer handlers
- If reducer behavior changes, verify rebuild determinism tests

For correctness discussions, reason in terms of event streams and reducer logic,
not current snapshot shape alone.
