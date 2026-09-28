# Operations

## Purpose

Every board write in Lattice v2 is a named **operation**: a typed,
transport-free unit of business logic that whichever process owns the board
executes. On a local board that is the CLI (or MCP, or dashboard) process; on a
hosted board it is the server. The code is the same in both. Decision record:
`Decisions.md`, "The operation is the write seam". Contract: `docs/hosted/SPEC.md` §3.

## Where it lives

| Path | Contents |
|---|---|
| `src/lattice/ops/base.py` | The framework: `@operation`, `Params` parsing, `Caller`, `OpContext`, `OpResult`, `execute`. Its module docstring shows one complete operation; read it first. |
| `src/lattice/ops/<group>_<verb>.py` | One operation per module (`task_status.py` is `task.status`). |
| `src/lattice/ops/discovery.py` | Finds operations: every module in `lattice.ops`, then every entry point in the `lattice.operations` group. |
| `src/lattice/ops/*_common.py`, `plan_gate.py`, `attestation_check.py` | Rules shared by several operations. |
| `src/lattice/core/errors.py` | `OpError(code, message, details)` and the code-to-HTTP-status map. |
| `src/lattice/boards.py` | `resolve_board(start)` and `LocalBoard.execute`; origin collection (`reported_origin`); the `LOCAL_ONLY` check. |
| `src/lattice/cli/ops_bridge.py` | The CLI side: build `Caller` from the Click context, run the operation, render the result or the error envelope. |

## The contract

- **Registration.** `@operation("<group>.<verb>")` on a class with a `Params`
  dataclass and a `run(ctx, params)` method. Adding an operation edits no shared
  file.
- **Params** are the CLI command's arguments and options, same names in
  snake_case, same defaults, minus `--json`, `--quiet`, `--actor`, and `--name`
  (the actor travels in `Caller`). `--file PATH` becomes the file's text;
  `attach`'s payload becomes `{filename, content_b64, sha256}`. Task IDs are
  passed as the caller gave them and resolved under the lock. `parse_params`
  rejects unknown keys, wrong types, and missing required keys with
  `VALIDATION_ERROR` (a server says `UNSUPPORTED_PARAM` for an unknown key).
- **Caller**: `actor`, `actor_name` (a session name), `origin`,
  `attestations` (facts only the caller's machine can check, such as a
  reviewed commit's reachability), `expect_last_event_id`.
- **OpResult**: `task` (snapshot), `events` (appended, in order), `value` (the
  command's `--json` data), `idempotent` (nothing to do), `replayed` (a server
  returned a stored result), plus `resource_id` / `resource_name` for resource
  hooks and `paths` (every durable path written).
- **Errors.** Every rejection is an `OpError` with the code and message the CLI
  has always printed. `lattice.ops` imports nothing from `lattice.cli`, so no
  `SystemExit` can happen below the CLI. Task-state rejections carry the task's
  compact snapshot in `details.snapshot`. Storage errors map uniformly:
  `AuthoritativeLogError` for "is archived / is active / does not exist" is
  `NOT_FOUND`, any other is `INTEGRITY_ERROR`, `BoardPathError` is
  `VALIDATION_ERROR`.

## Execution

`lattice.ops.execute(board_dir, op_name, params, caller, *, run_hooks, ...)`:

1. Refuse a client cache, or a server-owned board when the caller is not its
   server (the ownership markers in `storage/ownership.py`).
2. Look up the operation (`UNKNOWN_OP`), parse params, check path-bearing
   inputs (resource and session names must be one safe path component).
3. Resolve and authorize the actor, writing nothing: a session name is read
   (`SESSION_NOT_FOUND`), a string actor validated (`INVALID_ACTOR`); on a
   server the permission identity is checked against the token.
4. Set the origin context, so every event appended carries `origin`.
5. Run the operation. It writes through `ctx.mutate` (`mutate_task`) or the
   resource, prose, session, and config writers, which lock, strictly replay,
   run the decision against fresh state, append all of a decision's events in
   one write, then write the snapshot.
6. Return the `OpResult`. The front end renders it; client-local effects
   (hooks when hosted and opted in, c11 side effects, auto-review spawning)
   run after, on the client.

Every write goes through the storage primitives in `storage/fs.py`, which
check the ownership markers, confine paths to the board, and call the write
recorder before each durable mutation. On a server, that recorder callback is
the transaction's undo log (`docs/architecture/hosted.md`).

## Front ends

| Front end | How it calls an operation |
|---|---|
| CLI | `resolve_board(cwd).execute(...)` through `cli/ops_bridge.py`; renders exactly the pre-v2 output |
| MCP | the same, with the tool call's `lattice_root` as the starting directory |
| Local dashboard | POST handlers translate to operations (`dashboard/api.py`) |
| Server | `POST /v1/projects/<slug>/ops/<op>` runs `execute` inside a transaction (`server/app.py`, `server/transactions.py`) |
| Hosted client | `HostedBoard.execute` posts the operation with a fresh `op_id` and retries with the same one (`remote/`) |

## Plugins: the `lattice.operations` entry-point group

A separately installed package can ship operations. Name the module that
defines them in its packaging metadata:

```toml
[project.entry-points."lattice.operations"]
my_ops = "my_package.lattice_ops"
```

Discovery runs once, on the first registry lookup (not at import, so read
commands pay nothing). It imports Lattice's own modules in sorted order, then
each entry point sorted by name. A plugin that fails to import, or registers a
name already taken, is reported on stderr and skipped; it never breaks the
built-in operations. `lattice plugins` lists what is installed.

The server has no per-operation code: it runs whatever its installed Lattice
registers and relays whatever events they append. A hosted board therefore runs
a plugin operation only when the plugin is installed **on the server**, and a
new event family needs a reducer and an operation module, nothing else.

## Tests that hold the seam

- `tests/parity/`: golden recordings of every board-writing command, plain and
  `--json`, from pre-v2 code. Local output must not change.
- `tests/test_ops/test_import_graph.py`: `lattice.ops` imports nothing from
  `lattice.cli` or `lattice.integrations`.
- `tests/test_ops/`: one file per operation family.
