# LAT-88 Review Findings

## Critical

1. Non-atomic read/modify/write allows double-acquire and snapshot/event-log divergence.
- `resource_acquire` reads snapshot and decides availability before any lock is held (`src/lattice/cli/resource_cmds.py:220`, `src/lattice/cli/resource_cmds.py:297`).
- The lock is only acquired during final append/write in `write_resource_event` (`src/lattice/storage/operations.py:135`).
- The same TOCTOU pattern appears in create/release/heartbeat (`src/lattice/cli/resource_cmds.py:78`, `src/lattice/cli/resource_cmds.py:393`, `src/lattice/cli/resource_cmds.py:466`).
- Impact: two contenders can both "successfully" acquire a singleton resource; on-disk snapshot can disagree with replayed event stream under concurrency.
- Recommendation: hold `resources_<name>` + `events_<id>` lock across read-check-apply-write, re-read under lock, and write under the same critical section.

## High

1. Caller-supplied `--id` is format-validated but not uniqueness-validated.
- `resource_create` validates syntax only (`src/lattice/cli/resource_cmds.py:97`) and never checks whether another resource already uses that ID.
- Event logs are keyed by resource ID (`src/lattice/storage/operations.py:137`).
- Impact: two resource names can share one event stream, corrupting materialization/rebuild semantics.
- Recommendation: reject creation when an existing snapshot already has the provided ID (unless same logical resource and fully idempotent payload match).

2. Auto-create from config is race-prone and can split one name across multiple resource IDs.
- Config-only resolution returns "needs create" (`src/lattice/cli/helpers.py:236`).
- `_auto_create_resource` always generates a fresh ID and writes immediately (`src/lattice/cli/resource_cmds.py:562`, `src/lattice/cli/resource_cmds.py:585`) without a lock-guarded existence recheck.
- Impact: concurrent first acquires can create multiple resource IDs for the same resource name.
- Recommendation: lock by resource name before create; re-read snapshot under lock and only create if still missing.

## Medium

1. JSON status/list semantics are inconsistent with human output for stale holders.
- `_show_single_resource` filters stale holders for text output (`src/lattice/cli/resource_cmds.py:602`) but returns raw snapshot in JSON (`src/lattice/cli/resource_cmds.py:607`).
- `_show_all_resources` similarly filters only in text path (`src/lattice/cli/resource_cmds.py:661`) while JSON returns raw resources (`src/lattice/cli/resource_cmds.py:647`).
- Impact: JSON consumers can see expired holders and infer "held" while text output reports availability.
- Recommendation: normalize active/stale handling in both output modes.

2. Heartbeat can revive an already-expired holder instead of requiring reacquire.
- `resource_heartbeat` checks only holder presence (`src/lattice/cli/resource_cmds.py:471`) and extends TTL directly (`src/lattice/cli/resource_cmds.py:482`).
- Impact: TTL expiry is softened; an actor can extend after expiry if no one has evicted yet.
- Recommendation: fail heartbeat for stale holders (or force explicit acquire flow).

3. Critical contention/expiry paths are under-tested.
- `tests/test_cli/test_resource_cmds.py` covers sequential happy paths, but not concurrent acquire races, wait/poll timeout behavior, stale-eviction-on-acquire races, or rebuild determinism vs event log order.
- Recommendation: add integration tests with two concurrent contenders and an explicit rebuild consistency assertion.

## Low

1. Resource name is used directly as a filesystem path component without validation.
- Resource directory path is built from raw name (`src/lattice/storage/operations.py:128`, `src/lattice/cli/helpers.py:248`).
- Impact: names containing path separators/absolute forms can escape expected layout or produce hard-to-resolve resources.
- Recommendation: enforce a constrained resource-name regex.

## Positive

1. Strong event-sourcing shape in core materialization.
- Single materialization entrypoint with event timestamp-driven updates (`src/lattice/core/resources.py:15`) supports deterministic rebuild behavior.

2. Durable write sequence is sound inside the write primitive.
- Event-first append + atomic snapshot write under sorted multi-locks (`src/lattice/storage/operations.py:132`, `src/lattice/storage/operations.py:143`) is the right persistence pattern.

3. Pure core TTL/materialization helpers have focused unit coverage.
- `tests/test_core/test_resources.py` gives solid baseline validation for snapshot mutation and TTL helper behavior.
