# Lattice Hosted: Specification

**Status:** Draft for operator review · **Release:** Lattice v2 · **Tracking:** LAT-283
**Contract set:** `SPEC.md` (this file), `EVALUATION.md`, `BUILDPLAN.md`, `sequence/USER_STORIES.md`, `sequence/run-state.md`
**Supersedes:** `docs/design-lattice-remote.md` (Feb 2026). Its beliefs stand; its `StorageBackend.write_task(events, snapshot)` seam does not (see §2).

Acceptance criteria are cited by ID (AC-n) from `sequence/USER_STORIES.md`. Guardrails are G-n (§14). Every "must" is testable; `EVALUATION.md` names the test.

---

## 1. What this is

Lattice v2 adds an optional server. One server process per host owns every project board it hosts and is the only process that writes them. Clients send every write to the server as a named operation over HTTP. Full clients keep a read-only mirror of the board (the cache) in the checkout's `.lattice/`, kept fresh by a change stream or by polling. Local Lattice, with no server, is unchanged and remains the default.

The design rests on three facts about today's code (`BUILDPLAN.md` §2 has the file:line audit):

1. `mutate_task` already makes storage single-writer-safe on one machine: it locks, strictly replays a task's log, runs the caller's decision against fresh state, validates every proposed event, appends first, then writes the snapshot.
2. The decision is a Python callback embedded in each command. It cannot cross a network, so the process that owns the board must run it.
3. The business rules exist three times (CLI, MCP, dashboard) and already disagree. MCP and dashboard status changes skip the plan gate and review-cycle limit today.

So the seam is the **operation**: a named, typed, transport-free unit of business logic that whichever process owns the board executes. Local mode runs it in the CLI process. Hosted mode runs the identical code in the server.

### Non-goals (v2)

- Offline write queue. Offline writes fail (AC-8).
- Roles or permissions beyond "this token may act as these actors on these projects."
- User accounts, passwords, OAuth, SSO. Admin is shell access to the server host.
- An admin web UI. Admin is the `lattice server` CLI.
- Horizontal scaling, replication, or failover. One process per host.
- TLS inside the server. A reverse proxy terminates TLS.
- Server-side hooks. The server never executes board-configured shell commands (G-10).
- Event log compaction or journal truncation.
- Read-path performance work beyond today's.
- Moving any specific existing board (migration order is the operator's call at trial time).
- Judgment, decision, and spawn event families (LAT-275, LAT-276, LAT-277) and the c11 ingester family (LAT-273, LAT-274, LAT-281, LAT-282). They ride on v2 unchanged once built (§3.6).
- Filtering by machine, user, or worktree (AC-39) ships as a follow-up ticket, not in the core cut.

---

## 2. Architecture

```
  laptop / seat (full client)            box (thin client)             browser
  CLI · MCP · local dashboard            CLI, no follower               dashboard
  cache: .lattice/ (read-only)           cache dies with the box        cookie session
         │ ops (POST)  ▲ sync/stream            │ ops  ▲ sync (poll)          │
         ▼             │                        ▼      │                      ▼
 ┌───────────────────────────────── lattice server ─────────────────────────────────┐
 │ auth (token → user, machine, permitted actors) · per-project write lock          │
 │ ops executor (same lattice.ops code as local) · journal (seq, epoch) · sync      │
 │ SSE stream · per-project dashboard · audit git (debounced) · owner lease         │
 └──────────────────────────────────────────────────────────────────────────────────┘
      <server_root>/projects/<slug>/.lattice/   standard boards, plain files
```

- **Writes:** client → `POST /v1/projects/<slug>/ops/<op>` → server runs `lattice.ops.execute` on its board under the project lock → appends a journal entry → notifies streams.
- **Reads:** client catches its cache up (`GET .../sync?since=<seq>`), then runs today's read code against the cache unchanged.
- **Followers:** hold `GET .../stream` (SSE). An entry is both a notification (sync now) and the appended events (for panels).

### Package layout

| Path | Contents |
|---|---|
| `src/lattice/ops/` | Operation framework (`base.py`) and one module per operation group. Auto-discovered. |
| `src/lattice/boards.py` | `resolve_board(start) -> LocalBoard \| HostedBoard`: the single entry every CLI, MCP, and dashboard write uses. |
| `src/lattice/remote/` | Client: config, binding, HTTP (stdlib `urllib`), cache syncer, follower. Standard library only. |
| `src/lattice/server/` | Server: app, auth, tokens, projects, journal, sync, stream, audit, hosted dashboard, admin CLI. Imports Starlette only here. |
| `src/lattice/dashboard/api.py` | Transport-free dashboard read functions and write translation, shared by the stdlib local dashboard and the hosted dashboard. |

Names checked: `ops`, `boards`, `remote`, `server` are not Python keywords and shadow no stdlib module used by Lattice.

---

## 3. Operations (the write seam)

### 3.1 Contract

`src/lattice/ops/base.py` defines:

- `@operation("<group>.<verb>")` class decorator. Registers the class in a module-level registry. `lattice/ops/__init__.py` imports every submodule with `pkgutil.iter_modules`, so adding an operation edits no shared file.
- `Params`: a frozen dataclass per operation. **Derivation rule:** an operation's params are exactly its CLI command's arguments and options, same names in snake_case, same types, same defaults, minus presentation options (`--json`, `--quiet`) and actor options (which travel in `Caller`). File-path options become content: `--file PATH` becomes the file's text, and `attach`'s payload becomes `payload` (§3.8). Task identifiers are passed as the caller gave them (ULID or short ID) and resolved by the operation under the lock. `parse_params(cls, json_obj)` rejects unknown keys and wrong types with `VALIDATION_ERROR`.
- **Operations without a one-to-one CLI command** have these params:
  - `task.record_auto_review`: `{task, review_type, mode, log_path, spawned_at, pid, trigger_status_event_id, reviewed_worktree?}`, exactly the `auto_review_spawned` event data built today at `cli/task_cmds.py:944-975`; the actor is `agent:lattice-auto-review`.
  - `board.set_dashboard_config`: `{settings}`, an object restricted to the keys today's dashboard settings POST accepts.
  - `board.next_claim`: the options of `lattice next` (the claiming actor comes from `Caller`).
  - `session.start`: the options of `lattice session start`, where `name` is the new session's base name (a creation parameter), not the `--name` actor selector.
- `Caller`: `actor` (string or `None`), `actor_name` (a `--name` session name, or `None`), `origin` (§4), `attestations` (§3.4), `expect_last_event_id` (string or `None`).
- `OpResult`: `task` (the resulting snapshot, when the op targets one task), `events` (the events appended, in order), `value`, `idempotent` (bool, today's meaning: the operation had nothing to do), `replayed` (bool, §8.6). **`value` is the `data` object the command prints under `--json` today, minus any fields the CLI adds from client-local effects** (for example the auto-review spawn details `status` prints, `cli/task_cmds.py:992-1000`); the CLI merges those in after the operation returns, exactly as today.
- `OpError(code, message, details)` hierarchy. Every business-rule rejection raises one.

**Error codes.** Every code the CLI emits today is preserved verbatim, with the same message text, for the same condition (G-6). The HTTP mapping below applies to the server; the CLI keeps exit code 1 for every error, as today.

| Code | HTTP | Meaning |
|---|---|---|
| `VALIDATION_ERROR`, `MISSING_ARGS`, `INVALID_ID`, `INVALID_ROLE`, `INVALID_ACTOR` | 400 | Bad input (existing codes) |
| `MISSING_ACTOR` | 400 | No actor and none can be defaulted (existing) |
| `LOCAL_ONLY` | 400 | *New.* Maintenance command on a hosted board (§3.5) |
| `PROTOCOL_MISMATCH` | 400 | *New.* Client protocol version differs (§15) |
| `UNAUTHENTICATED` | 401 | *New.* Missing, invalid, or revoked credential |
| `FORBIDDEN` | 403 | *New.* Credential lacks the project |
| `ACTOR_NOT_PERMITTED` | 403 | *New.* Actor outside the token's permitted list |
| `NOT_FOUND`, `NOT_INITIALIZED`, `PLAN_NOT_FOUND`, `SESSION_NOT_FOUND` | 404 | Absent task, board, plan, or session (existing) |
| `UNKNOWN_OP` | 404 | *New.* Server lacks the operation; the message names client and server versions (AC-48) |
| `CONFLICT` | 409 | Existing uses unchanged, plus: declared expectation failed, `from` mismatch, operation ID reused (`details.reason: "OP_ID_REUSED"`) |
| `ALREADY_CLAIMED`, `RESOURCE_HELD`, `NOT_HELD`, `EXPIRED`, `FLAG_ALREADY_SET`, `FLAG_NOT_SET` | 409 | State conflicts (existing) |
| `STALE_VERSION` | 412 | *New.* A files-endpoint read whose `sha256` no longer matches (§8.8) |
| `PAYLOAD_TOO_LARGE` | 413 | *New.* Body over `limits.max_body_bytes` |
| `INVALID_TRANSITION`, `PLAN_REQUIRED`, `COMPLETION_BLOCKED`, `REVIEW_CYCLE_LIMIT` | 422 | Workflow rules (existing) |
| `TASK_ERASED` | 422 | *New.* Write to a tombstoned task |
| `INTEGRITY_ERROR` | 500 | Authoritative log fails strict replay (existing) |
| `BOARD_BUSY` | 503 | *New.* Project lock not acquired within `limits.lock_timeout_seconds`; `Retry-After: 2` |
| `BOARD_UNAVAILABLE` | 503 | *New.* Project failed its startup integrity check (§8.7) |
| `BOARD_IS_HOSTED`, `BOARD_IS_CACHE`, `BINDING_CONFLICT`, `SERVER_UNREACHABLE` | CLI only | *New.* §6, §9 |

Storage exceptions map as follows wherever an operation runs: `AuthoritativeLogError` for "is archived", "is active", or "does not exist" (`storage/operations.py:670-676`) becomes `NOT_FOUND` with today's message; any other `AuthoritativeLogError` becomes `INTEGRITY_ERROR`; on the server, `LockTimeout` becomes `BOARD_BUSY` (local mode keeps today's behavior).

Codes that arise only on the client (`MISSING_SURFACE`, `TIMEOUT`, `REVIEW_IN_FLIGHT`, `REVIEW_FAILED`, `HEAD_SHA_UNKNOWN`, `DIFF_RESOLUTION_FAILED`, `EMPTY_DIFF`, `REBUILD_ERROR`) never cross the wire.

Every rejection about a task's state (`CONFLICT` from an expectation or `from` mismatch, `ALREADY_CLAIMED`, `FLAG_ALREADY_SET`, `FLAG_NOT_SET`, and the 422 rows) carries `details.snapshot`: the task's current compact snapshot (AC-1). Resource conflicts carry the resource's state instead, and `OP_ID_REUSED` carries neither. The exact envelope for a reused operation ID:

```json
{"ok": false, "error": {"code": "CONFLICT", "message": "operation id op_01J9Z... was already used with different arguments", "details": {"reason": "OP_ID_REUSED", "seq": 118}}}
```

- **No `SystemExit` below the CLI layer.** `lattice.ops` imports nothing from `lattice.cli`; rule helpers that live in the CLI today and exit on failure (`check_plan_gate`, `cli/helpers.py:514-592`) move to `lattice.ops` or `lattice.core` and raise `OpError`. The CLI catches `OpError` and renders today's envelope and exit code (G-6).

### 3.2 Execution

`lattice.ops.execute(board_dir, op_name, params, caller, *, run_hooks: bool) -> OpResult`. `LocalBoard` passes `run_hooks=True` (hooks run in-process after the lock is released, exactly as today). The server passes `run_hooks=False`; the board's config is still loaded for workflow and policy rules. `mutate_task` and `write_resource_event` gain the same `run_hooks` parameter, replacing today's "hooks run when `config` is truthy" coupling. Steps:

1. Refuse if the board is a client cache, or is server-owned and the caller is not the owning server (§6, G-1).
2. Look up the operation (`UNKNOWN_OP`), parse params.
3. Resolve and authorize the actor without writing anything (§3.7).
4. Set the origin context (§4) for the duration of the call, so every event appended carries it.
5. Run the operation. It calls `mutate_task` (or the resource, prose, session, and config writers) with a callback holding exactly the rules that used to live in the CLI command, raising `OpError` instead of exiting. It follows the commit-point rules of §3.10.
6. Return `OpResult`. The caller (CLI, MCP, dashboard, server) renders it.

`mutate_task` changes (behavior-preserving for local mode):

- Appends all of a decision's events with **one** `write()` of the concatenated lines and one fsync, instead of one append per event. The resulting file bytes are identical to today's.
- Stamps `origin` from the context onto each proposed event that lacks one, before validation (§4).
- Converts the bare `ValueError` from a `from` mismatch into `OpError("CONFLICT")` with the current snapshot.
- Accepts `expect_last_event_id`. When set and different from the replayed snapshot's `last_event_id`, raise `CONFLICT` before running the callback.

### 3.3 Command to operation map

Every board-writing command becomes a thin Click wrapper: parse arguments, build `Params` and `Caller`, call `resolve_board(cwd).execute(...)`, render `OpResult` exactly as today. The MCP tools and the dashboard's POST handlers call the same operations and delete their private rule copies.

| CLI command | Operation |
|---|---|
| `create` | `task.create` |
| `update`, `edit-description` | `task.update`, `task.edit_description` |
| `status` | `task.status` (graph, review-cycle limit, completion policy, plan gate, auto-assign, plan-reset append) |
| `assign`, `needs-human` | `task.assign`, `task.needs_human` |
| `comment`, `comment-edit`, `comment-delete`, `react`, `unreact` | `task.comment`, `task.comment_edit`, `task.comment_delete`, `task.react`, `task.unreact` |
| `complete` | `task.complete` (review comment, review artifact, transitions; payload validated before any file is written) |
| `link`, `unlink` | `task.link`, `task.unlink` |
| `branch-link`, `branch-unlink`, `file-link`, `file-unlink` | `task.branch_link`, `task.branch_unlink`, `task.file_link`, `task.file_unlink` |
| `criterion add`, `edit`, `retire` | `task.criterion_add`, `task.criterion_edit`, `task.criterion_retire` |
| `archive`, `unarchive` | `task.archive`, `task.unarchive` |
| `claim`, `unclaim`, `next --claim` | `task.claim`, `task.unclaim` (these bind and unbind a c11 surface and are not exclusive, as today), `board.next_claim` (selection and assignment in one call under the project lock, so concurrent callers never receive the same task) |
| `attach` | `task.attach` (payload travels in params, §3.8) |
| `event` | `task.event` (custom `x_` types) |
| status auto-fire record | `task.record_auto_review` (the `auto_review_spawned` event) |
| **new** `plan write`, `notes write` | `task.plan_write`, `task.notes_write` (§3.9) |
| **new** `erase` | `task.erase` (§7) |
| `set-project-code`, `set-subproject-code` | `board.set_project_code`, `board.set_subproject_code` |
| dashboard settings POST | `board.set_dashboard_config` |
| `resource create`, `acquire`, `release`, `heartbeat` | `resource.create`, `resource.acquire` (one non-blocking attempt; `RESOURCE_HELD` when held), `resource.release`, `resource.heartbeat`. `acquire --wait` is a client-side loop: it calls `resource.acquire` repeatedly with today's backoff and timeout, each attempt a separate operation with its own `op_id`, holding no lock between attempts (today's loop at `cli/resource_cmds.py:375` releases its lock between attempts the same way) |
| `session start`, `session end` | `session.start`, `session.end` |
| `code-review`, `plan-review` | Composite: run on the client and write only through the operations above (their direct `mutate_task` calls become op calls; their `lattice` subprocesses route through the binding like any CLI call) |

### 3.4 Client-local facts and effects

Some rules depend on facts only the caller's machine can observe. The client computes them and passes them as `attestations`. The operation validates them against the board's current state, records them in the event data it writes, and the policy evaluates them. Local mode computes and passes the same attestations, so there is one code path.

- `reachable_review_commits`: a list of `{sha, branch, exists, reachable}`, one entry per `Lattice-Reviewed-Commit` marker in the task's review artifacts and in any payload the same operation attaches, evaluated by today's `git cat-file -e` / `git merge-base --is-ancestor` logic (`core/config.py:671-720`) in the caller's worktree against the task's latest branch link. The operation rejects the attestation as stale (`COMPLETION_BLOCKED`, message naming the mismatch) unless every entry's `branch` equals the task's current latest branch link and the entries cover exactly the marker SHAs the operation finds. The client re-syncs, recomputes, and retries once on a stale attestation. The `require_reachable_review_commit` policy passes when any entry has `exists` and `reachable`.

These effects run on the client after a successful write, never on the server:

1. **Hooks.** Locally: exactly as today (task and resource hooks, in-process, after the lock is released). Hosted: the client runs the board's hooks (from the cache's `config.json`) for `OpResult.events`, with the same executor and timeout, **only if its remote entry sets `"run_board_hooks": true`** (default false), because a hosted board's hook commands are chosen by whoever administers the server. The hook environment never contains `LATTICE_REMOTE_*` variables, the token, or any proxy header variable. Resource operations return `resource_id` and `resource_name` in `OpResult` so the client can run resource hooks.
2. **c11 bridge side effects** of `status`, `needs-human`/flag, and `claim`, keyed on the caller's environment as today.
3. **Auto-review spawning** on transitions to `review` or `planned`, in the caller's worktree, followed by a `task.record_auto_review` operation. `agent:lattice-auto-review` is permitted for every token as a built-in allowance (§8.3); the authenticated origin still records the token.
4. **Output rendering.** A replayed result (§8.6) renders and triggers effects exactly like a fresh success, because the client never saw the first response.

Machine-local runtime state stays on the client and is never synced (§6.1).

### 3.5 Local-only maintenance commands

`init`, `demo init`, `rebuild`, `doctor --fix`, `backfill-ids`, and `migrate needs-human` operate directly on a data directory. On a hosted checkout they fail with `LOCAL_ONLY` and a message naming the server-side procedure: stop the server, then run the command on the server host against `<server_root>/projects/<slug>` with `--offline-maintenance`, which is refused while any process holds the owner flock, takes the flock itself for its duration, and writes `hosted/maintenance.json` (`{at, command}`). The next server start sees that record, rotates the epoch, and removes it (§8.7), so every cache resyncs. `doctor` without `--fix` runs read-only on a cache (§9.6).

### 3.6 New event families need no server code

The server executes whatever operations its installed Lattice registers. It has no per-operation code, no per-event-type code, and relays any event an operation appends. Adding a family (LAT-275 judgments, LAT-276 decisions, LAT-277 spawn events) means adding an operation module and a reducer. Hosting it requires upgrading the server's Lattice install, nothing else (G-11).

### 3.7 Actors and sessions

`execute` moves today's `require_actor` logic into the writer and keeps its precedence exactly (`cli/helpers.py:206-262`). Order, with nothing written until step 4:

1. If `caller.actor_name` is set, read the session (`SESSION_NOT_FOUND` if absent) and build the structured actor with `_build_actor_dict`. Otherwise take `caller.actor` and validate it with `validate_actor` (`INVALID_ACTOR`).
2. Compute the actor's **permission identity**: a string actor is itself; a structured session actor is `agent:<base_name>`.
3. On a server, authorize the permission identity against the token (§8.3). Locally there is no token and this step is skipped.
4. Only now, for a session actor, touch the session (update its last-seen time) under the `sessions_index` lock. This replaces today's unlocked read-modify-write. On a server the touch is part of the operation's transaction, so a rejected or failed operation leaves the session untouched (§8.6).

`session.start` keeps today's semantics: each start allocates a new serial under the `sessions_index` lock. `session start` and `session end` take no actor today (`cli/session_cmds.py`) and keep that local interface; steps 1 to 4 do not apply to them. On a server, the token authorizes them as its own default actor, recorded in `origin.authenticated`, independent of the session being created or ended. `sessions/` is board data (synced when hosted).

### 3.8 Artifacts

`task.attach` params carry `payload: {filename, content_b64, sha256}`. The operation verifies the hash, writes the payload with `atomic_write` (replacing today's non-atomic copy at `cli/artifact_cmds.py:257`; metadata is already atomic), writes the metadata, and appends `artifact_attached`. `task.complete` validates everything before writing any file, so a refused completion leaves nothing to unlink (removing today's unlink at `cli/task_cmds.py:1828-1836`). Payloads larger than `limits.max_body_bytes` after base64 fail with `PAYLOAD_TOO_LARGE`.

### 3.9 Plans and notes

- `lattice plan write <task> (--file PATH | --stdin)` and `lattice notes write <task> (--file PATH | --stdin)`, with `--expect-sha256 HEX` (reject with `CONFLICT` if the current file hash differs) and the common options. Today `lattice plan <task> [--json]` is a read command (`cli/query_cmds.py:1325`); it becomes a group whose dispatcher treats a first argument that is not a subcommand name as the task of that legacy read, so `lattice plan LAT-5 --json` keeps working unchanged. `notes` is a new group.
- The operation writes the content with `atomic_write` to the task's plan (or notes) file at its current placement, then appends a `plan_written` (or `notes_written`) event with `data: {sha256, bytes}`. Both event types are no-ops for snapshot materialization and are registered in `BUILTIN_EVENT_TYPES`.
- Local users may still edit plan files directly. On a hosted cache, board files are mode 0444 and board directories 0555 (§9.4), so a direct write, a rename-based save, or a new file all fail. The server-side plan gate reads the server's copy, so `PLAN_REQUIRED` in hosted mode appends: "write the plan with `lattice plan write <task> --file <path>`."
- The lattice skill and the CLAUDE.md template teach `lattice plan write` as the method that works in every mode.

### 3.10 Atomicity

Local mode keeps today's write ordering and crash behavior exactly (events first, then snapshot and placement; `rebuild` and `doctor` as today). On a server, every operation runs as a transaction with an undo log and a single commit point, so a crash or a failure leaves it wholly applied or wholly absent (§8.6, AC-4). Operations need no per-family recovery code for this: the transaction wraps whatever durable writes they make.

---

## 4. Origin: where every change came from

Every event appended by v2 carries a top-level `origin` object, following the sparse optional pattern of `agent_meta` and `provenance`. `schema_version` stays 1: the field is additive, and replay and materialization ignore unknown top-level keys (G-6 test).

```json
"origin": {
  "op": "task.status",
  "op_id": "op_01J9Z...",
  "reported": {
    "host": "laptop-name",
    "os_user": "alice",
    "worktree": "/home/alice/src/proj-wt-auth",
    "branch": "feat/PRJ-12-auth",
    "client_version": "2.0.0"
  },
  "authenticated": {
    "token_id": "tok_01J9Y...",
    "user": "human:alice",
    "machine": "alice-laptop"
  }
}
```

- `op`, `op_id`: set by the writer. `op_id` is a client-generated ULID with prefix `op_`, one per operation call, reused only when that same call is retried (§8.6). A command that performs several operations (for example `status` followed by `task.record_auto_review`) generates one `op_id` for each.
- `reported`: collected by the client once per process and cached. `host` is `socket.gethostname()`. `os_user` is `getpass.getuser()`. `worktree` is `git rev-parse --show-toplevel` of the caller's cwd and `branch` is `git rev-parse --abbrev-ref HEAD`. Any field whose lookup fails is omitted, never an error. `client_version` is the Lattice version. Local mode records `reported` too (AC-36).
- `authenticated`: stamped only by a server, from the token (§8.3). The server discards any `authenticated` a client sends (AC-37).
- Resource events carry the same `origin`.
- `lattice show --events` prints, per event, `actor · user@machine · worktree (branch)`, using `authenticated` when present, else `reported`. `--json` output includes `origin` verbatim. The dashboard's task event view shows the same line (AC-38).

---

## 5. Short IDs

LAT-280 and LAT-269, generalized.

- **Floor.** Allocation computes `next = max(ids.next_seqs[prefix], 1 + max short-ID sequence observed for prefix in every task log, active and archived)`, then skips any ID present in the map. A short ID that appears in any task log is never issued to another task (AC-2).
- **Server.** Each project keeps `max_observed[prefix]` in memory, computed from the logs at project load and updated on every allocation, so a server create does not rescan. Allocation runs under the project lock.
- **Local.** The floor is computed per create by reading each task log's creation event and any short-ID-assignment events. It runs under the board-wide allocation locks, so it must stay cheap: it adds no more than 100 ms to a create on a 1,000-task board (the owner may cache by directory mtime).
- **Doctor.** `_validate_authoritative_short_ids` collects every problem instead of raising on the first. Doctor reports every unresolvable short ID, every short ID held by two tasks, and a `next_seqs` value at or below the maximum observed in the logs (AC-28).
- **Repair.** `rebuild --all` already rebuilds `ids.json` under the allocator lock with the log floor. Import (§11) runs the same repair.

---

## 6. Board ownership

### 6.1 Path classes

Every path under a `.lattice/` directory belongs to exactly one class. The class decides whether it syncs, whether writes to it are checked and recorded, and whether it may be deleted.

| Class | Paths | Synced | Marker-checked and recorded | Deletion on a hosted board |
|---|---|---|---|---|
| Durable board data | `tasks/`, `events/`, `archive/`, `plans/`, `notes/`, `artifacts/`, `resources/`, `sessions/`, `templates/` (review prompt overrides, `src/lattice/templates/__init__.py:8-21`), `config.json`, `ids.json`, `context.md`, `.gitignore` | yes | yes | only as §7 permits |
| Runtime | `locks/`, `review_state/`, `tmp-prompts/`, `.daemon/` | no | no | allowed |
| Temporary | `atomic_write` temp files (`.tmp.*` beside their target, `storage/fs.py:45`) | no | no | allowed |
| Server control | `hosted/` (owner lease, journal, receipts, undo logs, control requests, maintenance record) | no | server and offline maintenance only | never by a client |
| Cache control | `cache/` (holds `state.json`, the cache marker) | no | syncer and follower only | allowed |

### 6.2 Markers

Two markers make "one writer" structural (G-1, AC-3):

- **Server-owned board:** `<board>/.lattice/hosted/owner.json` (`{server_id, host, pid, started_at}`) plus an exclusive `fcntl.flock` on `<board>/.lattice/hosted/owner.lock`, held for the server's lifetime and released by the kernel on death. A second server fails to start on a held board. A durable-path write to a board with `hosted/owner.json` fails with `BOARD_IS_HOSTED` unless it runs inside the owning server process (an in-process flag set by the server).
- **Client cache:** `<checkout>/.lattice/cache/state.json`. A durable-path write fails with `BOARD_IS_CACHE` ("this is a read-only mirror of `<alias>/<project>`; writes go through the server") unless it comes from the cache syncer. Runtime and temporary paths stay writable, so reads (which take lock files) work on a cache.
- The check lives in the storage write primitives (`atomic_write`, `jsonl_append`, the placement copy and unlink), so no command can bypass it. Every durable write in `src/` must use one of them (H-8 audits this). The owner flag and the syncer flag are `contextvars` values, never process globals.
- **Offline maintenance:** a durable write to a server-owned board is also allowed when the command runs with `--offline-maintenance` and holds the owner flock itself (§3.5).
- Stale owner markers: `owner.json` whose flock is free is stale (a crashed server, or a finished offline maintenance). The next server takes it over and logs the takeover. `lattice server project unlock <slug>` removes a stale marker when no server holds the flock.

---

## 7. Tombstones and the no-delete rule

- `lattice erase <task> --reason TEXT` → `task.erase` → appends `task_tombstoned` `{reason}`. Snapshot gains `tombstoned: true`, `tombstoned_at`, `tombstone_reason` (present only when tombstoned). Nothing is removed from disk.
- Tombstoned tasks are excluded from `list`, `next`, stats, and dashboard boards by default. `list --include-tombstoned` shows them. `show` works and prints `ERASED: <reason>`. Any further write returns `TASK_ERASED`. There is no un-erase in v2.
- **No-delete rule on hosted boards (G-2, AC-27):** the only removals of durable board data permitted under a hosted board are (a) archive and unarchive relocation, which copies before it unlinks the source; (b) session-end relocation into `sessions/archive/`, also copy-first; and (c) rollback of an uncommitted operation from its undo log (§8.6), which restores pre-images and so may truncate or remove only what that same operation wrote. Rollbacks are logged. Runtime and temporary paths (§6.1) are not board data. `doctor --fix` is `LOCAL_ONLY`.
- **Doctor:** reports any task referenced by `_lifecycle.jsonl` or `ids.json` whose log file is absent (`missing_task_file`). A tombstoned task keeps its files, so a missing file is always a finding.
- **Enforcement:** in server tests, the write recorder (§8.5) fails the test on any unlink under a board that is not one of the permitted cases.

---

## 8. The server

### 8.1 Install and process

- Install: `uv tool install 'lattice-tracker[server]'` (or pip). The `server` extra adds `starlette`, `uvicorn`, `sse-starlette`, pinned to the ranges the `mcp` extra already resolves. The base install gains nothing (AC-30, G-4).
- Run: `lattice server serve [--root PATH] [--host H] [--port P]`. Foreground. uvicorn, one process, `--workers 1`. Without the extra, it exits 1 with an install hint. Other `lattice server` admin commands need no extra.
- Server root: `--root`, else `$LATTICE_SERVER_ROOT`, else `$XDG_DATA_HOME/lattice-server` (default `~/.local/share/lattice-server`).

```
<server_root>/
  server.json                    # config, below
  tokens.json                    # token registry, mode 0600
  web_sessions.json              # dashboard sessions (hashed), mode 0600
  projects/<slug>/               # a git repo when audit is enabled (§8.10)
    .lattice/                    # a standard board
      hosted/owner.json, owner.lock, journal.jsonl, journal_meta.json
```

`server.json` (all keys optional, defaults shown):

```json
{
  "bind": "127.0.0.1",
  "port": 8740,
  "trusted_proxy": false,
  "public_origins": [],
  "audit": {"enabled": true, "debounce_seconds": 5, "max_interval_seconds": 60, "push": null},
  "limits": {"max_body_bytes": 16777216, "inline_file_bytes": 1048576, "lock_timeout_seconds": 30},
  "stream": {"heartbeat_seconds": 15}
}
```

`audit.push` is `null` or `{"remote": "<git remote name>", "branch": "<branch>"}` applied per project (a project's `.lattice/hosted/audit.json` may override it). `trusted_proxy: true` makes the server honor `X-Forwarded-Proto` and `X-Forwarded-For` for cookie security and logs. `public_origins` lists the browser origins (for example `https://lattice.example.internal`) accepted by the dashboard's `Origin` check when a proxy rewrites `Host`. `limits.lock_timeout_seconds` may not exceed 60.

### 8.2 Admin CLI

All under `lattice server`; all accept `--root`; all have `--json`.

| Command | Effect |
|---|---|
| `init [--root]` | Create the root, `server.json`, empty `tokens.json` (0600). Idempotent. |
| `serve` | Run the server (§8.1). |
| `project create <slug> [--code CODE] [--subproject-code C]` | Create `projects/<slug>/.lattice/` exactly as `lattice init` would, then the journal (epoch, seq 0) and audit repo. Slug: `^[a-z0-9][a-z0-9-]{0,62}$`. |
| `project import <slug> --from DIR [--code CODE]` | §11. |
| `project list` | Slug, project code, head seq, task count, owner state. |
| `project unlock <slug>` | Remove a stale owner marker (§6). |
| `project rotate-epoch <slug>` | Start a new journal epoch (below). |
| `project recover <slug> --rollback \| --keep` | Resolve undo logs left without a journal (§8.7 step 3): roll them back, or keep the files as they are and delete the logs. Requires the owner flock to be free. |
| `token create --user human:NAME --machine LABEL [--actor PATTERN]... [--project SLUG]... [--all-projects]` | Mint a token; print it once. Default `--actor` is the `--user` value. |
| `token list` | Id, user, machine, actors, projects, created, revoked. Never the secret. |
| `token revoke <token_id>` | Set `revoked_at`. Effective on the next request (AC-13). |

Admin commands edit files the running server reads (`tokens.json` reloads on mtime change, checked per request), under `<server_root>/admin.lock` with `atomic_write`. They never write an existing board directly. Two commands act on a board, and both respect its owner:

- `project rotate-epoch <slug>` starts a new journal epoch so every cache resyncs (use it after restoring a backup). With the owner flock free it rotates directly, but refuses while any undo log exists: start the server once so recovery settles them, or run `project recover`. While a server owns the project, it writes a control request `hosted/control/<ULID>.json` (`{"action": "rotate-epoch"}`) and waits up to 30 s for `<ULID>.done`; the server checks `hosted/control/` at each admission and every 2 seconds, performs the rotation under the project's locks, and broadcasts `reset` (§8.9).
- `project import --replace` requires the owner flock to be free (stop the server first).

Rotation is itself recoverable. In order, each step fsynced: (1) write `hosted/rotation.json` `{old_epoch, new_epoch}`; (2) rename `journal.jsonl` to `hosted/journal.<old epoch>.jsonl` (kept, never deleted); (3) write a new `journal_meta.json` with `new_epoch` and a `baseline` of the current log lengths; (4) create an empty `journal.jsonl`; (5) delete `rotation.json`. Each step is idempotent, so startup completes an interrupted rotation from the marker before anything else (§8.7). The new epoch starts at seq 1.

### 8.3 Tokens and identity

- Token string: `lat_<token_id>_<secret>`, where `token_id` is `tok_` + ULID and `secret` is 32 random bytes, base64url. `tokens.json` stores `{id, sha256_hex(secret), user, machine, actors: [patterns], projects: [slugs] | ["*"], created_at, revoked_at}`. Comparison uses `hmac.compare_digest`. Secrets never appear in logs or error messages (AC-14, G-7).
- A token is issued to one person (`user`, a `human:` actor) for one machine or seat (`machine`, a free label). `user` and `machine` are therefore authenticated and stamped into `origin.authenticated` (AC-37).
- `actors` are `fnmatch` patterns over actor base IDs (`agent:*`, `agent:owner-3`, `human:alice`). A request's actor must match one pattern, else `ACTOR_NOT_PERMITTED` (AC-12). A session actor is checked by its permission identity `agent:<base_name>` (§3.7), before the session is touched.
- Default actor: when a request carries no actor and the token has exactly one pattern with no wildcard, that is the actor. Otherwise `MISSING_ACTOR`.
- Built-in allowance: every token may also act as `agent:lattice-auto-review` (`core/auto_review.py:26`), so auto-review works under strict tokens (§3.4). Review subprocesses already write as the caller's own actor.
- Auth header: `Authorization: Bearer <token>`. Missing or invalid: 401. Valid but project not listed: 403 (AC-11).

### 8.4 HTTP API (protocol 1)

Every response carries `Lattice-Server-Version` and `Lattice-Protocol: 1`. JSON bodies use the CLI envelope: `{"ok": true, "data": ...}` or `{"ok": false, "error": {"code", "message", "details"?}}`.

| Method and path | Auth | Purpose |
|---|---|---|
| `GET /healthz` | none | `{"ok": true, "version", "protocol": 1}` |
| `GET /v1/info` | token | Server version, protocol, the caller's identity (token id, user, machine, actors), visible projects, registered op names, audit state |
| `GET /v1/projects` | token | Visible projects: slug, project code, head seq |
| `POST /v1/projects/{slug}/ops/{op}` | token | Execute an operation (§8.6) |
| `GET /v1/projects/{slug}/sync?since=N&epoch=E` | token | Cache delta (§8.8) |
| `GET /v1/projects/{slug}/files/{path}` | token | Raw board file (large-file fetch), board paths only |
| `GET /v1/projects/{slug}/stream` | token or session | SSE (§8.9) |
| `GET /v1/projects/{slug}/tasks/{id}` | token | `{snapshot, events, plan}`; `id` may be a short ID. For curl and agents without a cache. |
| `GET /v1/projects/{slug}/tasks?status=&assigned=&include_archived=` | token | Compact snapshots |
| `GET /` | session | Project index page |
| `GET /login`, `POST /login` | none (`POST` authenticates by the token it submits) | Dashboard login (§10) |
| `POST /logout` | session | End the session |
| `GET /p/{slug}/`, `/p/{slug}/static/*`, `/p/{slug}/api/*` | session or token | Hosted dashboard (§10) |

Op request body:

```json
{
  "op_id": "op_01J9Z...",
  "params": { "...": "..." },
  "actor": "agent:claude-owner",
  "actor_name": null,
  "origin": {"reported": {"host": "...", "os_user": "...", "worktree": "...", "branch": "...", "client_version": "2.0.0"}},
  "attestations": {},
  "expect": {"last_event_id": "ev_01..."}
}
```

`actor`, `actor_name`, `attestations`, and `expect` are optional. Op response: `{"ok": true, "data": {"result": <OpResult as JSON>, "seq": <journal seq>}}`.

Request headers: `Authorization`, `Content-Type: application/json` (required for POST), `Lattice-Protocol: 1` (optional; if present and different, `PROTOCOL_MISMATCH` before anything runs), `Lattice-Client-Version`.

### 8.5 Serialization and the write recorder

- **Admission, then work.** Each project has an `asyncio.Lock` (admission) and a `threading.Lock` (work). A request first awaits the project's admission lock on the event loop, with a timeout of `limits.lock_timeout_seconds` (`BOARD_BUSY` on expiry). Only after admission does it take a worker thread (`anyio.to_thread.run_sync`) and the work lock. Waiting requests therefore never occupy worker threads, and a stalled project cannot starve another (AC-15). The audit committer thread takes only the work lock.
- Everything that reads or writes a board's durable files runs under the work lock. Requests (operations, sync assembly, file reads) pass admission first; the audit committer thread takes only the work lock. Because an operation holds the work lock for its whole transaction, audit staging never sees a partial transaction.
- A **write recorder** in `storage/fs.py` records every durable path (§6.1) written, appended, or unlinked by the storage primitives, and calls a registered callback *before every* durable mutation with the path and the mutation kind (`append`, `create`, `replace`, `unlink`). On a server, the transaction's callback decides which undo entries that requires (§8.6). It is a `contextvars` value created inside the worker thread by the op executor, and the executor returns its path set alongside the `OpResult`. It never crosses threads implicitly.

### 8.6 Transactions, journal, and receipts

Every operation on a server is a transaction, run under the project's work lock. Three server-control files carry it, all append-only JSONL with one fsync per line: the undo log `hosted/undo/<op_id>.jsonl`, the receipt file `hosted/receipts/<UTC YYYY-MM-DD>.jsonl`, and the journal.

1. **Begin.** Record the byte lengths of the journal and of today's receipt file (in memory), then create the undo log, whose first line is `{"epoch": <current epoch>}`.
2. **Undo entries.** The storage primitives append an entry to the undo log, and fsync it, *before* each change they guard. The log is a sequence, replayed in reverse on rollback:
   - before the first append to a path in this operation: `{"path", "kind": "length", "existed", "length"}`;
   - before creating, replacing, or unlinking a path, unless this operation already recorded a `content` entry for it: `{"path", "kind": "content", "existed", "content_b64"}` holding the path's bytes at that moment (so a log appended to and then unlinked by archive placement gets a `length` entry and then a `content` entry).

   A torn final undo line guards a change that was never made (the change waits for the fsync), so it is ignored.
3. **Work.** The operation runs, writing durable files through the normal storage code.
4. **Receipt.** Append `{"op_id", "fp", "epoch", "seq", "result"}` (the full `OpResult` JSON) to the receipt file.
5. **Commit point.** Append the journal line and fsync. In-process, the operation counts as committed only when both the write and the fsync succeed. At startup, the only evidence is on disk: a complete journal line means committed (§8.7).
6. **Finish.** Update the in-memory idempotency index, delete the undo log, then hand the entry to the stream broadcaster, all still under the locks, so streams see entries in `seq` order. Any failure here is handled by transaction recovery below, which never rolls back a committed operation.

**Failures: one recovery path.** On any failure after step 1 (an `OpError`, any other exception, or a failed write or fsync of a board or control file), the server runs **transaction recovery** for this operation before admitting another request to the project, then returns the error to the caller:

1. If the operation's journal line was written in full and its fsync succeeded, the operation is committed and is never rolled back: complete step 6 (index entry, undo deletion), and if publication failed, close the project's open streams so followers reconnect and replay from the journal (§8.9).
2. Otherwise it is uncommitted: truncate the journal and the receipt file back to the lengths recorded at step 1 and fsync them; roll back from the undo log by replaying its entries in reverse (a `content` entry restores the bytes, or removes the path if it did not exist; a `length` entry truncates the log to its length, or removes it if it did not exist; a torn final entry is ignored); delete the undo log.
3. If the journal fsync failed (durability unknown), or any step of recovery fails, mark the project `BOARD_UNAVAILABLE` (503 on every route for it) and log the reason; startup recovery decides from what is on disk (§8.7).

A rejected operation therefore leaves nothing behind, including a session touch or a short-ID reservation. Most rejections happen before any write, so their recovery is only the deletion of an empty undo log.

**Durability errors propagate on the server.** Today `_fsync_directory` swallows `OSError` (`storage/fs.py`). Inside a server transaction, and for every server-control write, a failed file or directory fsync raises, so recovery can react. Local mode keeps today's behavior.

**Journal and metadata.**

- `<board>/.lattice/hosted/journal.jsonl`, one line per committed operation, including no-ops: `{"seq", "ts", "op", "op_id", "fp", "token_id", "task_id", "event_ids", "paths", "lengths"}`. `ts` has millisecond precision; `seq` starts at 1 per epoch and increases by 1. `paths` lists every durable path changed. `lengths` maps each append-only log the operation appended to its byte length afterward.
- `fp` is the request fingerprint: the first 32 hex characters of SHA-256 over the canonical JSON (sorted keys, separators `(",", ":")`) of `{"op", "params", "actor", "actor_name", "attestations", "expect_last_event_id"}`.
- `journal_meta.json`: `{"epoch": "ep_<ULID>", "created_at", "baseline": {<log path>: <byte length>}, "clean_shutdown": null | {"head_seq", "tree_fingerprint"}}`. `baseline` records every log's length when the epoch began; with `lengths` it gives the last known length of every log, which startup uses to detect foreign appends (§8.7).

**Replay (AC-46).**

- The idempotency index maps `op_id → (fp, epoch, seq, receipt location)` for the last 7 days of receipts. At load it is rebuilt from the receipt files: a receipt counts only if the journal of its `epoch` (the current `journal.jsonl` or a retained `journal.<epoch>.jsonl`) holds a line with the same `seq` and `op_id`; any other receipt line is an orphan of an uncommitted operation and is removed. Receipt files older than 7 days are deleted (server control data, not board data); a retry of an operation older than that runs again. Epoch rotation does not affect deduplication.
- The server checks the index **after** admission (§8.5), so a retry that queued behind its own first attempt sees the committed result.
- A known `op_id` with the same `fp` does not run again: the server returns the stored `OpResult` verbatim with `replayed: true`. A known `op_id` with a different `fp` returns the `OP_ID_REUSED` envelope (§3.1).
- **Client retries.** The client retries an operation only on connection failure or a read timeout, reusing its `op_id`, at most twice (0.5 s, then 1.5 s backoff). The read timeout for operation requests is 90 seconds, above the maximum `lock_timeout_seconds` plus work time, so a slow admission is never mistaken for a lost request.

### 8.7 Startup, integrity, and crash recovery

At project load (server start, or first request after `project create` / `import`), under the project's locks, in this order:

1. **Lease.** Acquire the owner lease (§6.2). If `hosted/rotation.json` exists, finish that rotation's remaining steps.
2. **Journal tail.** Drop a truncated final line of `journal.jsonl` (its operation never committed). Drop torn final lines of receipt files and undo logs.
3. **Missing journal.** If `journal.jsonl` or `journal_meta.json` is missing or unparseable beyond its final line: with no undo logs present, rotate the epoch; with undo logs present, mark the project `BOARD_UNAVAILABLE` and log that `lattice server project recover <slug> --rollback | --keep` must decide (committed state cannot be known without the journal).
4. **Transactions.** For each `hosted/undo/<op_id>.jsonl`: if the journal of the epoch named on its first line (the current `journal.jsonl`, or `journal.<epoch>.jsonl` if that epoch was rotated away) holds `op_id` in a complete line, the operation committed; delete the undo log. Otherwise roll it back (§8.6) and log `recovery_rollback`. Then rebuild the idempotency index, removing orphan receipts (§8.6).
5. **Maintenance and restores.** If `hosted/maintenance.json` exists, rotate the epoch and remove it. Else if `clean_shutdown` is set and the durable tree's fingerprint (a hash over each durable file's path, size, and mtime) differs from it, rotate the epoch. Clear `clean_shutdown`.
6. **Foreign changes.** Any log longer than its last known length (§8.6), or any durable file changed outside the journal as detected by step 5's fingerprint when the epoch was not rotated, gets an `external` journal entry listing the paths, and a warning log.
7. **Discovery.** Run strict discovery. If it fails (for example a foreign writer left a truncated line), mark the project `BOARD_UNAVAILABLE` (503 on every route for it), log the failing path, and keep serving other projects. The admin repairs it with offline maintenance (`doctor --fix --offline-maintenance`).
8. Compute the `max_observed` short-ID floors.

At graceful shutdown the server writes `clean_shutdown` for each project after its final audit commit.

`config.json` and `context.md` changed by hand while the server runs are detected by mtime at each admission and journaled as `external` entries, so caches receive them.

Unknown-event-type warnings go through a module-level reporter in `core/tasks.py` whose default prints to stderr exactly as today; the server installs a reporter that logs once per (project, type) per process.

### 8.8 Sync

`GET /v1/projects/{slug}/sync?since=N&epoch=E`:

- If `epoch` matches and `N` ≤ head: return `{"epoch", "head_seq", "reset": false, "files": {...}, "removed": [...]}` covering every path in journal entries `N+1..head`, coalesced to the current content of each path.
- If `epoch` is absent or differs, or `N` > head: `reset: true` and every board file (AC-23). A reset inlines files until a cumulative 32 MiB of content and returns the rest as `href`.
- `files[path]` is `{"sha256", "size", "content_b64"}` when `size ≤ limits.inline_file_bytes`, else `{"sha256", "size", "href"}` where `href` is the files endpoint with `?sha256=<hash>`. The files endpoint returns 412 `STALE_VERSION` when the file no longer has that hash; the client then re-runs sync from its current `head_seq`.
- Only durable board paths (§6.1) are ever returned.
- Path traversal is rejected; every path must resolve under the board.
- The response (head, then file bytes and hashes) is assembled under the project's locks, so it reflects exactly the state at `head_seq` and never a write in progress. The files endpoint also reads under the locks. Inline limits keep lock hold times short.

### 8.9 Stream

`GET /v1/projects/{slug}/stream` (SSE, `sse-starlette`):

- A new stream subscribes to the project's broadcaster first, then replays journal entries after its resume point, then delivers live entries, dropping any `seq` it has already sent. Entries arrive in `seq` order with no gap and no duplicate (AC-22).
- Resume point: `Last-Event-ID: <epoch>:<seq>` header or `?since=<seq>&epoch=<epoch>`. If more than 10,000 entries separate it from head, the server sends `reset` instead of replaying.
- Each entry: `id: <epoch>:<seq>`, `event: journal`, `data:` the journal entry plus `"events"`: the full appended events, read back from the logs by `event_ids`.
- Epoch mismatch or rotation: one `event: reset` with the new epoch, then live entries (AC-23).
- A comment line immediately on connect and then every `stream.heartbeat_seconds`.
- The credential is rechecked at each heartbeat; a revoked token or expired session closes the stream (AC-13).

### 8.10 Audit history

- With `audit.enabled` and `git` on `PATH`, each `projects/<slug>/` is a git repository (created at `project create`/`import`) whose `.gitignore` excludes `.lattice/hosted/owner.*`, `.lattice/locks/`, and runtime dirs.
- A per-project committer thread stages (`git add -A`) under the project lock, then commits outside it, `debounce_seconds` after the last write and at most `max_interval_seconds` after the first uncommitted one. Message: `audit: seq <a>-<b> (<n> ops)`. Author: `Lattice Hosted <lattice-hosted@localhost>`.
- If `push` is configured, push after each commit. Failures are logged and retried at the next commit; they never block writes (AC-26).
- If `git` is missing, audit is disabled with one startup warning, and `/v1/info` says so. Shutdown makes a final commit.

### 8.11 Logging, health, shutdown

- One JSON object per line to stdout. Request lines: `{ts, level, event: "request", method, path, status, duration_ms, project, op, op_id, token_id, actor, seq, error_code}`. Lifecycle lines for startup, project load, recovery, audit, lease, and config reload. Never token secrets, payload contents, or plan text; sizes only (AC-31, G-7).
- `GET /healthz` is unauthenticated and touches no board.
- SIGTERM: stop accepting, let in-flight ops finish (up to 30 s), close streams, make final audit commits, release leases, exit 0.
- The server imports nothing from `lattice.integrations` or `lattice.cli.c11_bridge`, opens no c11 socket, spawns no agent, and runs no hook (G-5, G-10).

---

## 9. The client

### 9.1 Per-user remotes

`$XDG_CONFIG_HOME/lattice/remotes.json` (default `~/.config/lattice/remotes.json`), mode 0600. Lattice refuses to read a token from it if it is group- or world-readable.

```json
{
  "remotes": {
    "team": {
      "url": "https://lattice.example.internal",
      "token": {"env": "LATTICE_TOKEN_TEAM"},
      "headers": {"CF-Access-Client-Id": {"env": "PROXY_ID"}, "CF-Access-Client-Secret": {"env": "PROXY_SECRET"}},
      "run_board_hooks": false
    }
  }
}
```

- `token` is a literal string or `{"env": "VAR"}`. Header values are always `{"env": "VAR"}`, never literals (AC-20). `run_board_hooks` (default `false`) opts this machine into running the hosted board's hooks (§3.4).
- Environment overrides, for environments with no config file (thin clients): `LATTICE_REMOTE_<ALIAS>_URL`, `LATTICE_REMOTE_<ALIAS>_TOKEN`, `LATTICE_REMOTE_<ALIAS>_HEADERS` (a JSON object mapping header name to the name of the environment variable holding its value). `<ALIAS>` is uppercased with non-alphanumerics replaced by `_`. Environment wins over the file.
- `lattice remote add <alias> <url> [--token-env VAR | --token-stdin] [--header NAME=ENVVAR]...` writes the file. `lattice remote list` shows aliases and URLs, never tokens.

### 9.2 Binding

- `<repo>/.lattice-remote.json`, committed: `{"remote": "<alias>", "project": "<slug>"}`. No hostname, no credential (AC-18, G-3).
- `lattice remote attach <alias> <project> [--migrate]`: verify the alias resolves and the project exists and is visible to the token; write the binding; add `/.lattice/` to the repo's `.gitignore` (idempotent); run the initial sync. With an existing local board it refuses unless `--migrate` (§11).
- `lattice remote status [--json]`: binding, URL, identity from `/v1/info`, cache epoch and seq, last sync time, follower state, and whether the cache is stale.

### 9.3 Root discovery

`find_root` keeps its order (`LATTICE_ROOT`, then the linked-worktree jump to the primary worktree, then walk up) and recognizes a directory holding `.lattice-remote.json` as a hosted root, including when `LATTICE_ROOT` names it before its cache exists. The worktree jump resolves a relative `gitdir:` against the directory of the `.git` file, not the process cwd (fixing `storage/fs.py:206-211`). The cache is that directory's `.lattice/`, created by the first sync. Because the worktree jump already resolves every linked worktree to the primary checkout, every worktree shares one binding and one cache with no setup (AC-10). The binding must be present on the primary checkout's branch. A directory holding both a binding and a local board without `cache/state.json` fails with `BINDING_CONFLICT` and names `attach --migrate`.

### 9.4 Cache

- Layout identical to a local `.lattice/` (AC-9), plus `cache/state.json` (the marker): `{remote, project, epoch, head_seq, synced_at, fingerprint}`, written only by syncs; and `cache/follower.json`: `{pid, stream_live_until}`, written only by the follower.
- **Read-only on disk.** The syncer writes board files atomically and then sets them to mode 0444 (`atomic_write` creates 0600, so the chmod is explicit). Durable directories and `.lattice/` itself are mode 0555; the syncer adds the owner write bit only while it applies a delta, under its locks. A direct write, a rename-based save (`sed -i`, most editors), and a new file all fail. Runtime directories and `cache/` stay writable.
- **One sync at a time.** Every sync (a command's catch-up, a write's post-write sync, the follower's syncs) holds `locks/cache_sync.lock` exclusively for its whole cycle: read `state.json`, fetch, verify, apply. Two syncs can therefore never apply out of order or interleave paths.
- **Applying a delta** additionally takes the cache's reader-writer lock exclusively (`fcntl.flock(LOCK_EX)` on `locks/cache_rw.lock`), writes files, unlinks `removed` paths, and updates `cache/state.json` last. Every read on a hosted checkout (each read command after its catch-up, and each hosted-checkout dashboard request) holds the same lock shared (`LOCK_SH`) from its first directory enumeration through its last file read. A reader therefore never sees a sync half-applied, including an archive relocation between enumeration and a prose read (`storage/operations.py:337-368`, `cli/query_cmds.py:1337-1352`). A `reset` replaces the board files wholesale under the same exclusive lock (files absent from the reset are removed). Hosted mode requires a POSIX platform (macOS or Linux), as the owner lease does.
- **Verification.** Every path in `files` and `removed` must be a relative durable path (§6.1): no `..`, no absolute path, no symlink component, never a runtime path or `cache/`; otherwise the whole delta is rejected. Every file's bytes, inline or fetched, must match its `sha256`; on any mismatch the client discards the delta and re-syncs. An `href` must be a relative path on the same server; the client never sends its token or headers to another origin.
- **Tamper check.** `fingerprint` hashes each durable file's path, size, and mtime after every sync. Catch-up recomputes it (a stat walk, no reads); a mismatch (a checkout of an old branch that still tracks board files, a manual `chmod` and edit) triggers a reset sync. After sync, every board file matches the server byte for byte (AC-47).

### 9.5 Freshness, reads, and writes

- **Before any read** (every read command, and the read phases of write commands such as rendering), the client calls catch-up, unless a live follower is running: the `stream_live_until` field of `cache/follower.json` is in the future. Catch-up is one sync call with a 2-second connect and 5-second total timeout; with nothing new it returns a few hundred bytes. On failure it prints one line to stderr, `lattice: cannot reach <alias>; showing cache as of <synced_at>`, and continues (AC-8). `--json` stdout is unaffected.
- **Writes:** `HostedBoard.execute` posts the op. On success it syncs (so the next read sees the write, AC-6) and returns the `OpResult`. On connection failure after retries: `SERVER_UNREACHABLE`, exit 1, nothing written locally. Error envelopes and exit codes are the local ones (AC-5).
- **Actor:** in hosted mode `--actor` is optional; the server defaults it (§8.3). `--name` on a read command resolves the session from the cache read-only and never touches it; only the writer touches sessions (§3.7).
- **Origin:** the client fills `origin.reported` on every op.

### 9.6 Follower, watch, and doctor

- `lattice sync [--follow]`: one catch-up, or a foreground follower that holds the stream and syncs on each entry (at most one sync in flight, coalescing). Every byte received from the stream (entries and heartbeat comments) sets `stream_live_until = now + 2 × heartbeat_seconds` in `cache/follower.json`. Whenever the stream has delivered nothing for 3 seconds (including a stream that connected but is being buffered by a proxy), the follower also polls sync every 3 seconds, and it reconnects a failed stream with backoff capped at 60 seconds (AC-45). It exits 0 on SIGTERM and clears `stream_live_until`.
- `lattice dashboard` on a hosted checkout runs an embedded follower for its lifetime. Its writes go through `HostedBoard.execute`.
- `lattice watch` and `lattice wait` on a hosted checkout read the stream (or poll) instead of watching files, and print the same output.
- `lattice doctor` on a cache runs its read-only checks, then compares each board file's hash against a `reset`-style manifest from the server (`sync?since=0` with `manifest=1`, hashes only) and reports local modifications. `--fix` is `LOCAL_ONLY`.

---

## 10. Dashboard

- **Local (unchanged look):** `lattice dashboard` still serves the stdlib dashboard. Its read handlers call `dashboard/api.py`; its POST handlers call operations, so they gain the CLI's full rules and return the operations' error codes (a deliberate change to the dashboard's JSON API only: for example, a stale transition now returns the CLI's code instead of a generic 400). Security fix, deliberately included: POSTs require `Content-Type: application/json` and an `Origin` matching the served host, closing today's cross-origin POST exposure.
- **Base path:** the page derives a base path from its own URL; the `api()` and `apiPost()` helpers and every asset reference (today hard-coded `/static/*`, `static/index.html:7-8, 1625-1637`) use it, so the same assets work at `/` locally and at `/p/<slug>/` hosted.
- **Hosted:** the server serves each project's dashboard at `/p/<slug>/` from the same assets, answering `/p/<slug>/api/*` with `dashboard/api.py` on the authoritative board. POSTs become operations with the session's token; any actor in the request body is ignored and the token's default actor is used. `open-notes` and `open-plans` return `LOCAL_ONLY`. When hosted, the page subscribes to `/v1/projects/<slug>/stream` and refetches on entries, falling back to its 5-second poll if the stream fails (AC-24).
- **Login:** `GET /login` serves a form; `POST /login` with a valid token creates a session (32 random bytes; stored as a SHA-256 hash in `web_sessions.json` with `token_id`, `created_at`, `expires_at` 30 days later) and sets `lattice_session` (`HttpOnly; SameSite=Strict; Path=/`, plus `Secure` when the request is HTTPS). A session dies with its token (AC-13); expired sessions are pruned on every write to `web_sessions.json`. Cookie-authenticated POSTs require JSON content type and an `Origin` equal to the request's host origin or listed in `public_origins`. A session cookie authenticates only `/`, `/p/<slug>/...`, and the stream; it never authenticates `/v1/.../ops`, `sync`, or `files`.
- **Index:** `GET /` lists the projects the session's token may see, linking to each dashboard (AC-16). It uses the dashboard's stylesheet.

## 11. Moving a board

- `lattice server project import <slug> --from DIR [--replace]`: `DIR` contains `.lattice/`. Run strict doctor read-only; refuse on any error and print the findings (AC-17). Copy durable board files into `projects/<slug>/.lattice/`. Run the short-ID repair (§5) with the log floor. Create the journal with a new epoch and head 0, the audit repo, and an initial commit. Print the client attach command. The source is never modified. `--replace` re-imports over an existing project only while the owner flock is free (server stopped) and that project's journal head is 0 (no hosted write yet); the previous project directory is moved to `projects/.replaced/<slug>-<UTC timestamp>/`, never deleted.
- **Freeze.** Between import and attach, the source board must not be written. The guide says so, and attach enforces it:
- `lattice remote attach <alias> <slug> --migrate` in a checkout with a local board: fetch the server's manifest (`sync?since=0&manifest=1`) and compare it with the local board over every durable path except the derived ones that import rebuilds (task snapshots in `tasks/` and `archive/tasks/`, `ids.json`, `events/_lifecycle.jsonl`). That includes `archive/events/`, `archive/plans/`, and `archive/notes/`. Any path missing on either side or differing by hash fails with the list of differences and the instruction to re-import with `--replace` (AC-35). On a match: move `.lattice/` to `.lattice.pre-hosted-<UTC YYYYMMDD-HHMMSS>/`; add both paths to `.gitignore`; if board files are tracked, run `git rm -r --cached -q .lattice` (staged, not committed); write the binding; initial sync; print exactly what to commit. Nothing is deleted (AC-34).

## 12. MCP

MCP tools resolve their board with `resolve_board` (honoring `lattice_root`), call operations for writes, and read after catch-up. They therefore work hosted and apply the full rules.

## 13. Documentation deliverables

- `docs/hosted/guide.md`: when to use hosted and when not (local is the default), install, `server init`, `project create`/`import`, tokens (one per person per machine; seats get single-actor tokens), `serve` under launchd and systemd, reverse proxies (TLS termination, stream buffering off, read timeout above the 15-second heartbeat), client `remote add`/`attach`, thin clients by environment variables, the follower, backup, upgrade, and troubleshooting. Written so an agent can execute it end to end (AC-44).
- `docs/hosted/api.md`: every endpoint, body, and error code in §8.4, with curl examples.
- `docs/hosted/deploy/lattice-server.plist.example` and `lattice-server.service.example`, with placeholder paths and hosts.
- README: replace "coming soon: Lattice Remote" with a short "Lattice Hosted (optional)" section after the local quick start, pointing at the guide and saying when you would want it (AC-43).
- `skills/lattice/SKILL.md` and `templates/claude_md_block.py`: `lattice plan write` as the way to write plans; a short hosted section pointing at the guide. `docs/architecture/` gains `operations.md` and `hosted.md`.
- `Decisions.md` entries: the operation seam; origin on events; hosted boards leave git (for hosted checkouts only; local keeps its policy); tombstones; the v2 release branch.
- `docs/design-lattice-remote.md` already carries a status line pointing here (added at the architect stage).

---

## 14. Guardrails

Each guardrail has a pass/fail check in `EVALUATION.md` and an enforcement ticket in `BUILDPLAN.md`, including an audit of existing code.

| ID | Guardrail | Enforcement |
|---|---|---|
| G-1 | One writer per board. No client, cache, local CLI, or second server writes a server-owned board, and no code path writes a cache except the syncer. | §6 markers checked in every storage write primitive; write recorder coverage; audit of every writer in `src/` (the 43 commands, sessions, config, artifacts, prose, review state) |
| G-2 | No deletion of board data on a hosted board beyond the cases §7 permits (relocation; rollback of an uncommitted operation). | Recorder-based test; `doctor --fix` `LOCAL_ONLY`; refused-`complete` unlink removed; audit of every unlink and rmtree |
| G-3 | No deployment-specific hostnames, tokens, or proxy secrets in the repository. | Hygiene test in the default suite over tracked files: denylist from `LATTICE_HYGIENE_DENYLIST` (set privately by the operator; the test also passes when it is unset), plus built-in patterns for token shapes (`lat_tok_`), private keys, and Cloudflare Access header values |
| G-4 | The base install gains no runtime dependency; `import lattice.cli` imports no server library. | Test comparing `[project].dependencies` to a pinned list; import-graph test |
| G-5 | The server is independent of c11. | Subprocess test: `lattice server serve` runs with a fake `c11` executable first on `PATH` and `C11_*` variables set; the hosted parity corpus runs against it; the fake is never executed and no socket at `C11_SOCKET_PATH` is opened. `lattice.server` and `lattice.ops` import nothing from `lattice.integrations` or `lattice.cli` (import-graph test) |
| G-6 | Local mode is unchanged, except for these declared changes: the `origin` fields (AC-36) and the origin line in `show --events` (AC-38); the new commands; the dashboard's JSON error codes and its content-type and `Origin` checks (§10). | Golden parity corpus (AC-29) with the declared changes normalized; unknown top-level event keys ignored by replay; `lattice.ops` imports nothing from `lattice.cli` (so no `output_error` and no `SystemExit` can be reached from an operation); the server wraps `execute` in a `BaseException` handler that returns 500 and logs |
| G-7 | No token secret at rest in plaintext or in any log. | Tests over `tokens.json`, `web_sessions.json`, and captured logs |
| G-8 | No offline write queue. | Test: writes while unreachable leave the cache and filesystem unchanged |
| G-9 | The default test suite stays hermetic and fast. | Server tests in-process on `127.0.0.1:0`; slow torture tests behind the `torture` marker; suite time recorded per PR |
| G-10 | The server runs no hooks, spawns no agents, and executes no board-configured commands. | Test: a board with hooks configured, written through the server, runs no hook on the server |
| G-11 | New operations and event types need no server-specific code. | Test: an operation registered only in the test process (a plugin-style module) is executable through an in-process server with no server change |

---

## 15. Compatibility

- **Protocol** is the integer 1. A client refuses a server whose `/v1/info` protocol differs, before any write, and the server refuses a request whose `Lattice-Protocol` header differs (AC-48).
- **Version skew:** a client op the server lacks returns `UNKNOWN_OP` naming both versions (AC-48). The client never sends params the operation does not declare.
- **Event schema:** `schema_version` stays 1. `origin`, `task_tombstoned`, `plan_written`, and `notes_written` are additive. Older Lattice reads v2 boards: unknown top-level keys are ignored and the new types are unknown types (skipped with a warning), so their snapshots still materialize.
- **Local boards** keep their layout, their tracked-board policy, and their `.gitignore` scaffolding. Nothing in local mode mentions hosting unless a binding exists (AC-43).
- **Python** floor stays 3.12.

## 16. Criteria index

| Section | ACs |
|---|---|
| §3 Operations | AC-1, AC-5, AC-29, AC-46 |
| §4 Origin | AC-36, AC-37, AC-38 |
| §5 Short IDs | AC-2, AC-28 |
| §6 Ownership | AC-3 |
| §7 Tombstones | AC-27 |
| §8 Server | AC-4, AC-11 to AC-17, AC-22, AC-23, AC-25, AC-26, AC-30, AC-31, AC-46, AC-48 |
| §9 Client | AC-6 to AC-10, AC-18 to AC-21, AC-45, AC-47 |
| §10 Dashboard | AC-24, AC-38 |
| §11 Migration | AC-34, AC-35 |
| §13 Docs | AC-32, AC-43, AC-44 |
| §14 Guardrails | AC-3, AC-27, AC-29, AC-30, AC-33 |
| Scenarios | AC-40, AC-41, AC-42 (`EVALUATION.md` §4) |
| Follow-up | AC-39 |
