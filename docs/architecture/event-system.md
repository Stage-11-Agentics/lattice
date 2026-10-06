# Event System

## Purpose

Lattice is event-sourced. Every authoritative change is recorded as an immutable
event in `.lattice/events/<task_id>.jsonl` (or resource event logs). Task and
resource snapshots are derived caches.

## Core Event Model

Primary implementation lives in `src/lattice/core/events.py`.

- Event shape:
  - `schema_version`
  - `id` (`ev_...`)
  - `ts` (UTC RFC3339 string)
  - `type`
  - `task_id` or `resource_id`
  - `actor` (string or structured identity dict)
  - `data` (event payload)
  - optional `agent_meta` (legacy compatibility)
  - optional `provenance` (`triggered_by`, `on_behalf_of`, `reason`)
- Serialization is compact JSONL via `serialize_event()`.
- Built-in event names are centrally enumerated in `BUILTIN_EVENT_TYPES`.

## Event Categories

- Task lifecycle: `task_created`, `task_archived`, `task_unarchived`
- Task mutation: `status_changed`, `assignment_changed`, `field_updated`,
  comments/reactions, relationships, artifacts, branch links, file links
  (`file_linked`, `file_unlinked`), and acceptance criteria
  (`acceptance_criterion_added`, `acceptance_criterion_edited`,
  `acceptance_criterion_retired`)
- Resource mutation: `resource_created`, `resource_acquired`,
  `resource_released`, `resource_heartbeat`, `resource_expired`, `resource_updated`

Only lifecycle events are duplicated into `_lifecycle.jsonl`.

## Write Path (Durability)

Authoritative write path is `mutate_task()` in `src/lattice/storage/operations.py`:

1. Acquire the deterministic event/snapshot lock set (`multi_lock`), plus
   lifecycle or ID-index locks when declared
2. Resolve all active/archive event-log candidates and strictly replay the
   complete authoritative history
3. Run the caller callback against that replayed state; the callback returns
   proposed events, not a precomputed snapshot
4. Validate and append newly proposed per-task events
5. Re-reduce and atomic-write the canonical snapshot, repairing stale or
   missing caches even for a zero-event retry
6. Reconcile declared lifecycle/placement state, release locks, then execute
   hooks only for events appended by this invocation

This ensures event-first durability: if a crash happens between event append and
snapshot write, `lattice rebuild` can recover snapshots from events.

Acceptance-criterion history is immutable and task-local. Criterion IDs are
stable opaque tokens; automatic allocation considers only exact `AC-N` IDs.
Optional `criterion_ids` on `comment_added` and `artifact_attached` are
traceability links to criteria that already exist at that point in history.
They never mean that a criterion passed or was satisfied.

## Provenance and Attribution

The actor on each event is the canonical attribution source. Provenance is sparse
and only included when provided by caller options.

Operational implication:

- Always pass the correct `--actor`
- Use provenance only for traceability, not as a substitute for actor ownership

## Rework/Review Signals

`count_review_rework_cycles()` scans task events for transitions from `review`,
`in_validation` or `pr_open` to `in_progress` or `in_planning`. Each such
transition records `review_cycle` on its event. `latest_review_auto_fired()`
decides whether the limit is enforced: only when Lattice auto-fired the review
for the latest entry into `review`.

## Hooks

`src/lattice/storage/hooks.py` executes configured shell hooks after writes are
already durable. Hook failures never roll back events/snapshots.

Hook order:

1. `hooks.post_event`
2. `hooks.on.<event_type>`
3. transition hooks (`from -> to`, wildcard patterns) for `status_changed`

Hook environment (`_build_env` / `_build_resource_env`): `LATTICE_ROOT` is the
project root that contains `.lattice/` (the same meaning `find_root` gives it),
and `LATTICE_DIR` is the `.lattice/` directory. Also set: `LATTICE_EVENT_TYPE`,
`LATTICE_EVENT_ID`, `LATTICE_ACTOR`, plus `LATTICE_TASK_ID` (task hooks),
`LATTICE_FROM_STATUS` / `LATTICE_TO_STATUS` (transition hooks), or
`LATTICE_RESOURCE_ID` / `LATTICE_RESOURCE_NAME` (resource hooks).

## Practical Debugging Flow

For any task-state bug:

1. Inspect `.lattice/events/<task_id>.jsonl`
2. Confirm event ordering and payload correctness
3. If snapshot looks wrong, run `lattice rebuild <task_id>`
4. Re-check snapshot and CLI behavior

Start with events, not snapshots.
