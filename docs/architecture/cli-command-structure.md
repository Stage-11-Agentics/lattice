# CLI Command Structure

## Purpose

The CLI is the primary orchestration layer that connects pure core logic to
filesystem-backed storage.

Entrypoint: `src/lattice/cli/main.py`

## Command Registration Model

`main.py` defines root Click group `cli` and imports command modules at the end
for side-effect registration.

Registered modules include:

- `task_cmds.py`
- `query_cmds.py`
- `link_cmds.py`
- `artifact_cmds.py`
- `criterion_cmds.py`
- `archive_cmds.py`
- `integrity_cmds.py`
- `resource_cmds.py`
- `session_cmds.py`
- `file_cmds.py`
- `stats_cmds.py`
- `weather_cmds.py`
- `dashboard_cmd.py`
- `migration_cmds.py`

This keeps command files modular while exposing a single `lattice` binary.

## Common Command Flow

Since v2, every board-writing command is a thin wrapper over an operation
(`operations.md`):

1. Parse arguments and run today's argument checks, in today's order, before
   looking up the board. Read a `--file` only when it will be used, so a
   directory or unreadable path keeps its `VALIDATION_ERROR`.
2. Build the operation's `Params` and a `Caller` (`cli/ops_bridge.py`).
3. `resolve_board(cwd).execute(...)`: a `LocalBoard` runs the operation in
   process; a hosted checkout posts it to the server. The operation resolves
   the task and actor, runs the rules, and persists through `mutate_task()`
   (lock, strict replay, append, snapshot) or the resource, prose, session,
   and config writers.
4. Render the `OpResult` exactly as before (`human`, `--json`, or `--quiet`),
   or the `OpError` as the usual envelope with exit 1; then run client-local
   effects (auto-review spawn, c11 side effects).

`init`, `demo init`, `rebuild`, `doctor --fix`, `backfill-ids`, and
`migrate` write a data directory directly; on a hosted checkout they refuse
with `LOCAL_ONLY` (`boards.check_local_only`).

Read commands traverse snapshots/events with no mutation.

## Shared Helpers

`src/lattice/cli/helpers.py` centralizes:

- `common_options` decorator (`--actor`, provenance, `--json`, `--quiet`)
- output helpers (`output_result`, `output_error`, JSON envelope)
- root/snapshot/resource resolution helpers
- plan gate helper (`check_plan_gate`)

## Output Contracts

Most commands support:

- human-readable text (default)
- machine envelope (`--json` => `{ok,data}` / `{ok:false,error}`)
- terse `--quiet` mode for automation

Preserve these contracts when adding commands to avoid breaking scripts.

## Acceptance Criteria Commands

`lattice criterion add`, `edit`, `retire`, and `list` manage optional
task-local criteria. Add/edit accept either inline outcome prose or `--file`;
automatic IDs are `AC-N`, while `--id` accepts a validated opaque local ID.
Listing may include retired records and history and works for archived tasks;
mutations are active-only. `comment` and `attach` accept repeatable
`--criterion` links to existing active or retired criteria. These links are
traceability only and do not affect workflow or completion policies.

## Extension Pattern (New Command)

When adding a write command:

1. Add an operation module under `src/lattice/ops/` holding the rules
   (`ops/base.py`'s docstring is the template); keep reusable rules in `core/`
2. Keep fs/locking in `storage/`
3. Keep the Click command thin: argument checks, `Params`, `execute`, output
4. Add tests in `tests/test_ops/` and `tests/test_cli/`, and a parity
   scenario when the command writes the board
5. Ensure idempotency and deterministic output where applicable

## Dashboard Parity

Dashboard write endpoints and MCP tools call the same operations as the CLI,
so they apply the same rules and return the same error codes.

If you change CLI write semantics, check whether dashboard write paths need the
same update.
