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
  comments/reactions, relationships, artifacts, branch links
- Resource mutation: `resource_created`, `resource_acquired`,
  `resource_released`, `resource_heartbeat`, `resource_expired`, `resource_updated`

Only lifecycle events are duplicated into `_lifecycle.jsonl`.

## Write Path (Durability)

Authoritative write path is `write_task_event()` in `src/lattice/storage/operations.py`:

1. Acquire deterministic lock set (`multi_lock`)
2. Append per-task events JSONL (`jsonl_append`)
3. Append lifecycle events JSONL (if applicable)
4. Atomic-write snapshot
5. Release locks
6. Execute hooks (post-durability)

This ensures event-first durability: if a crash happens between event append and
snapshot write, `lattice rebuild` can recover snapshots from events.

## Provenance and Attribution

The actor on each event is the canonical attribution source. Provenance is sparse
and only included when provided by caller options.

Operational implication:

- Always pass the correct `--actor`
- Use provenance only for traceability, not as a substitute for actor ownership

## Rework/Review Signals

`count_review_rework_cycles()` scans task events for `review -> in_progress` and
`review -> in_planning` transitions. This powers the review-cycle safety valve
in status transitions.

## Hooks

`src/lattice/storage/hooks.py` executes configured shell hooks after writes are
already durable. Hook failures never roll back events/snapshots.

Hook order:

1. `hooks.post_event`
2. `hooks.on.<event_type>`
3. transition hooks (`from -> to`, wildcard patterns) for `status_changed`

## Practical Debugging Flow

For any task-state bug:

1. Inspect `.lattice/events/<task_id>.jsonl`
2. Confirm event ordering and payload correctness
3. If snapshot looks wrong, run `lattice rebuild <task_id>`
4. Re-check snapshot and CLI behavior

Start with events, not snapshots.
