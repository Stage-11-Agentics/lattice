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

Write commands generally follow:

1. Resolve root (`require_root`) and actor (`require_actor`)
2. Resolve task identifier (`resolve_task_id`) if needed
3. Put state-dependent validation and event construction in a callback
4. Persist through `mutate_task()`, which locks, strictly replays authority,
   appends events, and materializes the snapshot
7. Render output (`human`, `--json`, or `--quiet`)

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

1. Keep business rules in `core/` when reusable
2. Keep fs/locking in `storage/`
3. Use CLI layer for validation + argument handling + output formatting
4. Add tests in `tests/test_cli/` plus core/storage tests as needed
5. Ensure idempotency and deterministic output where applicable

## Dashboard Parity

Dashboard write endpoints in `dashboard/server.py` intentionally mirror CLI
write behavior but use HTTP JSON requests/responses.

If you change CLI write semantics, check whether dashboard write paths need the
same update.
