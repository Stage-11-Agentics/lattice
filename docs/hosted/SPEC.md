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

- `@operation("<group>.<verb>")` class decorator. Registers the class in a module-level registry. `lattice/ops/__init__.py` imports every submodule with `pkgutil.iter_modules`, so adding an operation edits no shared file. It then imports every module named by an entry point in the `lattice.operations` group, so a separately installed plugin package can ship operations. Client and server discover the same way; on a hosted board a plugin operation runs only when the plugin is installed on the server (the guide says so).
- `Params`: a frozen dataclass per operation. **Derivation rule:** an operation's params are exactly its CLI command's arguments and options, same names in snake_case, same types, same defaults, minus presentation options (`--json`, `--quiet`) and actor options (which travel in `Caller`). File-path options become content: `--file PATH` becomes the file's text, and `attach`'s payload becomes `payload` (§3.8). Task identifiers are passed as the caller gave them (ULID or short ID) and resolved by the operation under the lock. `parse_params(cls, json_obj)` rejects unknown keys, wrong types, and missing required keys with `VALIDATION_ERROR`; a missing key that has a declared default takes it. On a server, an unknown key is `UNSUPPORTED_PARAM` instead (§15).
- **Path-bearing inputs.** Some inputs name files. `execute` checks them after parsing and before actor resolution, in both modes, so no path is ever built from an unchecked input, and rejects a failure with `VALIDATION_ERROR`. The operation ID must match `^op_[0-9A-HJKMNP-TV-Z]{26}$` (`op_` plus a ULID). A resource name (every `resource.*` operation) and a session name (`caller.actor_name`, and the name `session.start` and `session.end` take) must each be one safe path component: 1 to 128 characters, no `/`, `\`, NUL, or other control character, and neither `.` nor `..`. An artifact payload's `filename` is never used as a path (§3.8). As a backstop, every storage write primitive refuses a path that does not resolve under the board's `.lattice/` directory (§6.2).
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
| `UNSUPPORTED_PARAM` | 400 | *New.* The server's operation lacks a parameter the client sent; the message names the parameter and both versions (§15) |
| `CLIENT_TOO_OLD` | 400 | *New.* The client's version is below the server's `min_client_version`; the message names both versions (§15) |
| `UNAUTHENTICATED` | 401 | *New.* Missing, invalid, or revoked credential |
| `FORBIDDEN` | 403 | *New.* Credential lacks the project |
| `ACTOR_NOT_PERMITTED` | 403 | *New.* Actor outside the token's permitted list |
| `NOT_FOUND`, `NOT_INITIALIZED`, `PLAN_NOT_FOUND`, `SESSION_NOT_FOUND` | 404 | Absent task, board, plan, or session (existing) |
| `UNKNOWN_OP` | 404 | *New.* Server lacks the operation; the message names client and server versions (AC-48) |
| `CONFLICT` | 409 | Existing uses unchanged, plus: declared expectation failed, `from` mismatch, operation ID reused (`details.reason: "OP_ID_REUSED"`) |
| `ALREADY_CLAIMED`, `RESOURCE_HELD`, `NOT_HELD`, `EXPIRED`, `FLAG_ALREADY_SET`, `FLAG_NOT_SET` | 409 | State conflicts (existing) |
| `STALE_VERSION` | 412 | *New.* A files-endpoint read whose `sha256` no longer matches (§8.8) |
| `PAYLOAD_TOO_LARGE` | 413 | *New.* Body over `limits.max_body_bytes`, or `task.event` data over `limits.max_event_data_bytes` |
| `INVALID_TRANSITION`, `PLAN_REQUIRED`, `COMPLETION_BLOCKED`, `REVIEW_CYCLE_LIMIT` | 422 | Workflow rules (existing) |
| `TASK_ERASED` | 422 | *New.* Write to a tombstoned task |
| `RATE_LIMITED` | 429 | *New.* A per-token limit was reached (§8.1); `Retry-After` gives the seconds to wait |
| `INTEGRITY_ERROR` | 500 | Authoritative log fails strict replay (existing) |
| `BOARD_BUSY` | 503 | *New.* Project lock not acquired within `limits.lock_timeout_seconds`; `Retry-After: 2` |
| `BOARD_UNAVAILABLE` | 503 | *New.* Project failed its startup integrity check or its transaction recovery (§8.6, §8.7), or is unloaded (§8.2) |
| `STORAGE_LOW` | 507 | *New.* Free disk under the server root is below `limits.min_free_disk_bytes`; writes are refused, reads still work (§8.11) |
| `BOARD_IS_HOSTED`, `BOARD_IS_CACHE`, `BINDING_CONFLICT`, `SERVER_UNREACHABLE`, `OUTCOME_UNKNOWN`, `PROXY_REJECTED`, `INSECURE_URL`, `REMOTE_NOT_CONFIGURED`, `TOKEN_ENV_UNSET`, `CACHE_INCOMPLETE`, `NOT_HOSTED`, `HOSTED_UNSUPPORTED_PLATFORM` | CLI only | *New.* §6, §8.6, §9 |

Storage exceptions map as follows wherever an operation runs: `AuthoritativeLogError` for "is archived", "is active", or "does not exist" (`storage/operations.py:670-676`) becomes `NOT_FOUND` with today's message; any other `AuthoritativeLogError` becomes `INTEGRITY_ERROR`; `BoardPathError` (a write whose path escapes the board, §6.2) becomes `VALIDATION_ERROR`; on the server, `LockTimeout` becomes `BOARD_BUSY` (local mode keeps today's behavior).

Codes that arise only on the client (`MISSING_SURFACE`, `TIMEOUT`, `REVIEW_IN_FLIGHT`, `REVIEW_FAILED`, `HEAD_SHA_UNKNOWN`, `DIFF_RESOLUTION_FAILED`, `EMPTY_DIFF`, `REBUILD_ERROR`) never cross the wire.

Every rejection about a task's state (`CONFLICT` from an expectation or `from` mismatch, `ALREADY_CLAIMED`, `FLAG_ALREADY_SET`, `FLAG_NOT_SET`, and the 422 rows) carries `details.snapshot`: the task's current compact snapshot (AC-1). Resource conflicts carry the resource's state instead, and `OP_ID_REUSED` carries neither. The exact envelope for a reused operation ID:

```json
{"ok": false, "error": {"code": "CONFLICT", "message": "operation id op_01J9Z... was already used with different arguments", "details": {"reason": "OP_ID_REUSED", "seq": 118}}}
```

- **No `SystemExit` below the CLI layer.** `lattice.ops` imports nothing from `lattice.cli`; rule helpers that live in the CLI today and exit on failure (`check_plan_gate`, `cli/helpers.py:514-592`) move to `lattice.ops` or `lattice.core` and raise `OpError`. The CLI catches `OpError` and renders today's envelope and exit code (G-6).

### 3.2 Execution

`lattice.ops.execute(board_dir, op_name, params, caller, *, run_hooks: bool) -> OpResult`. `LocalBoard` passes `run_hooks=True` (hooks run in-process after the lock is released, exactly as today). The server passes `run_hooks=False`; the board's config is still loaded for workflow and policy rules. `mutate_task` and `write_resource_event` gain the same `run_hooks` parameter, replacing today's "hooks run when `config` is truthy" coupling. Steps:

1. Refuse if the board is a client cache, or is server-owned and the caller is not the owning server (§6, G-1).
2. Look up the operation (`UNKNOWN_OP`), parse params, and check path-bearing inputs (§3.1).
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
| **new** `context write` | `board.context_write` (§3.9) |
| **new** `board write` | `board.file_write` (§3.9) |
| **new** `unerase` | `task.unerase` (§7) |
| **new** `erase` | `task.erase` (§7) |
| `set-project-code`, `set-subproject-code` | `board.set_project_code`, `board.set_subproject_code` |
| dashboard settings POST | `board.set_dashboard_config` |
| `resource create`, `acquire`, `release`, `heartbeat` | `resource.create`, `resource.acquire` (one non-blocking attempt; `RESOURCE_HELD` when held), `resource.release`, `resource.heartbeat`. `acquire --wait` is a client-side loop: it calls `resource.acquire` repeatedly with today's backoff and timeout, each attempt a separate operation with its own `op_id`, holding no lock between attempts (today's loop at `cli/resource_cmds.py:375` releases its lock between attempts the same way) |
| `session start`, `session end` | `session.start`, `session.end` |
| `code-review`, `plan-review` | Composite: run on the client and write only through the operations above (their direct `mutate_task` calls become op calls; their `lattice` subprocesses route through the binding like any CLI call) |

### 3.4 Client-local facts and effects

Some rules depend on facts only the caller's machine can observe. The client computes them and passes them as `attestations`. The operation validates them against the board's current state, records them in the event data it writes, and the policy evaluates them. Local mode computes and passes the same attestations, so there is one code path.

- `reachable_review_commits`: a list of `{sha, branch, exists, reachable}`, one entry per `Lattice-Reviewed-Commit` marker in the task's review artifacts and in any payload the same operation attaches, evaluated by today's `git cat-file -e` / `git merge-base --is-ancestor` logic (`core/config.py:671-720`) in the caller's worktree against the task's latest branch link. The operation rejects the attestation as stale (`COMPLETION_BLOCKED`, message naming the mismatch) unless every entry's `branch` equals the task's current latest branch link and the entries cover exactly the marker SHAs the operation finds. The client re-syncs, recomputes, and retries once on a stale attestation, as a new operation call with a new `op_id`. The `require_reachable_review_commit` policy passes when any entry has `exists` and `reachable`. On a server these entries are claims the authenticated token made, not facts the server verified: the operation records them with the attesting token in the event data, and the guide's trust section says so (§13).

These effects run on the client after a successful write, never on the server:

1. **Hooks.** Locally: exactly as today (task and resource hooks, in-process, after the lock is released). Hosted: the client runs the board's hooks (from the cache's `config.json`) for `OpResult.events`, with the same executor and timeout, **only if its remote entry sets `"run_board_hooks": true`** (default false), because a hosted board's hook commands are chosen by whoever administers the server. The hook environment never contains `LATTICE_REMOTE_*` variables, the token, or any proxy header variable. Resource operations return `resource_id` and `resource_name` in `OpResult` so the client can run resource hooks.
2. **c11 bridge side effects** of `status`, `needs-human`/flag, and `claim`, keyed on the caller's environment as today.
3. **Auto-review spawning** on transitions to `review` or `planned`, in the caller's worktree, followed by a `task.record_auto_review` operation. Whether a transition fires a review, and in which mode, is decided exactly as locally (`should_auto_fire`, `core/auto_review.py`) from the project's `config.json` in the caller's cache, which catch-up has just refreshed. Each hosted project therefore keeps its own review workflow: plan reviews only, code reviews only, both, or none (AC-49). The server admin sets it per project (§8.2). A machine can decline to run a hosted board's auto-reviews by setting `"run_auto_reviews": false` on its remote (§9.1; default `true`); the status output then says the review was skipped for that reason, and `lattice code-review <task>` still runs one by hand. The server itself never runs reviews in v2; server-run reviews are a possible later expansion. `agent:lattice-auto-review` is permitted for every token as a built-in allowance (§8.3); the authenticated origin still records the token. The review agent (the model process `core/agent_spawn.py` starts) runs without any `LATTICE_REMOTE_*` variable, the remote's token variable, or any proxy header variable; the `lattice code-review` or `plan-review` subprocess that starts it keeps them, because it writes through the binding.
4. **Output rendering.** A replayed result (§8.6) renders and triggers effects exactly like a fresh success, because the client never saw the first response. A committed write whose post-write sync fails is also a success: the client prints the one-line staleness notice (§9.5), renders the result, and runs the effects.

Machine-local runtime state stays on the client and is never synced (§6.1).

**Review status on hosted checkouts.** `review_state/` is machine-local, so on a hosted checkout `lattice review-status <task>` reads each gate from the board. It takes the task's latest `auto_review_spawned` event for that gate and checks for an artifact of that gate's role (`review` for code review, `plan-review` for plan review) attached after it:

- An artifact attached after the spawn: the review finished; report it as today.
- No such artifact, and the spawn's `origin.reported.host` is this machine with a local `review_state` record: today's local report.
- No such artifact, otherwise: `running on <host> since <spawned_at>` while less than `review_timeout_seconds` has passed since `spawned_at`, then `spawned on <host>, no artifact after <review_timeout_seconds> s; treat as failed`.

On a hosted checkout, `lattice code-review` and `lattice plan-review` refuse with `REVIEW_IN_FLIGHT` while a spawn of that gate newer than the gate's last artifact is younger than `review_timeout_seconds`. A new `--force` option overrides the refusal. The auto-fired child itself is never refused by its own spawn: a `code-review` or `plan-review` started with `--triggered-by <status event id>` whose spawn record names the same trigger proceeds, preserving today's parent-to-child handoff (`cli/review_cmds.py:153-192`) in either order of the child starting and the parent recording `auto_review_spawned`. Dashboard moves start no review, as locally today, and a thin client must stay alive until its review lands; the guide says both (§13).

### 3.5 Local-only maintenance commands

`init`, `demo init`, `rebuild`, `doctor --fix`, `backfill-ids`, and `migrate needs-human` operate directly on a data directory. On a hosted checkout they fail with `LOCAL_ONLY` and a message naming the server-side procedure: unload the project (`lattice server project unload <slug>`, §8.2) or stop the server, then run the command on the server host against `<server_root>/projects/<slug>` with `--offline-maintenance`, which is refused while any process holds the owner flock, takes the flock itself for its duration, and writes `hosted/maintenance.json` (`{at, command}`). The next project load (`project load`, or a server start) sees that record, rotates the epoch, and removes it (§8.7), so every cache resyncs. `init` gets its own message instead, because its user usually wants a board, not maintenance: "this checkout is bound to `<alias>/<project>`; its board lives on the server. For a separate local board, work in a checkout without `.lattice-remote.json`." `doctor` without `--fix` runs read-only on a cache (§9.6).

### 3.6 New event families need no server code

The server executes whatever operations its installed Lattice registers. It has no per-operation code, no per-event-type code, and relays any event an operation appends. Adding a family (LAT-275 judgments, LAT-276 decisions, LAT-277 spawn events) means adding an operation module and a reducer. Hosting it requires upgrading the server's Lattice install, nothing else (G-11).

### 3.7 Actors and sessions

`execute` moves today's `require_actor` logic into the writer and keeps its precedence exactly (`cli/helpers.py:206-262`). Order, with nothing written until step 4:

1. If `caller.actor_name` is set, read the session (`SESSION_NOT_FOUND` if absent) and build the structured actor with `_build_actor_dict`. Otherwise take `caller.actor` and validate it with `validate_actor` (`INVALID_ACTOR`).
2. Compute the actor's **permission identity**: a string actor is itself; a structured session actor is `agent:<base_name>`.
3. On a server, authorize the permission identity against the token (§8.3). Locally there is no token and this step is skipped.
4. Only now, for a session actor, touch the session (update its last-seen time) under the `sessions_index` lock. This replaces today's unlocked read-modify-write. On a server the touch is part of the operation's transaction, so a rejected or failed operation leaves the session untouched (§8.6).

`session.start` keeps today's semantics: each start allocates a new serial under the `sessions_index` lock. `session start` and `session end` take no actor today (`cli/session_cmds.py`), and neither do `set-project-code` and `set-subproject-code` (`cli/main.py:1213-1312`); all four keep that local interface, and steps 1 to 4 do not apply to them. On a server, the token authorizes them as its own default actor, recorded in `origin.authenticated`, independent of the session being created or ended. `sessions/` is board data (synced when hosted).

### 3.8 Artifacts

`task.attach` params carry `payload: {filename, content_b64, sha256}`. `filename` is metadata: it supplies the default title and the content-type guess, as the source file's name does today, and is never used as a path. The operation stores the payload at `artifacts/payload/<artifact_id><suffix>`, where `artifact_id` is validated as today and `suffix` is `PurePosixPath(filename).suffix`, which can hold no path separator (today's local name at `cli/artifact_cmds.py:253` is the same). The operation verifies the hash, writes the payload with `atomic_write` (replacing today's non-atomic copy at `cli/artifact_cmds.py:257`; metadata is already atomic), writes the metadata, and appends `artifact_attached`. `task.complete` validates everything before writing any file, so a refused completion leaves nothing to unlink (removing today's unlink at `cli/task_cmds.py:1828-1836`). Payloads larger than `limits.max_body_bytes` after base64 fail with `PAYLOAD_TOO_LARGE`.

### 3.9 Plans and notes

- `lattice plan write <task> (--file PATH | --stdin)` and `lattice notes write <task> (--file PATH | --stdin)`, with `--expect-sha256 HEX` (reject with `CONFLICT` if the current file hash differs) and the common options. Today `lattice plan <task> [--json]` is a read command (`cli/query_cmds.py:1325`); it becomes a group whose dispatcher treats a first argument that is not a subcommand name as the task of that legacy read, so `lattice plan LAT-5 --json` keeps working unchanged. `notes` is a new group.
- The operation writes the content with `atomic_write` to the task's plan (or notes) file at its current placement, then appends a `plan_written` (or `notes_written`) event with `data: {sha256, bytes}`. Both event types are no-ops for snapshot materialization and are registered in `BUILTIN_EVENT_TYPES`.
- Local users may still edit plan files directly. On a hosted cache, board files are mode 0400 and durable directories 0500 (§9.4), so a direct write, a rename-based save, or a new file under `plans/` or `notes/` all fail. An edit that gets through anyway (after a `chmod`, or as root) is moved aside at the next catch-up, never silently discarded (§9.4). The server-side plan gate reads the server's copy, so `PLAN_REQUIRED` in hosted mode appends: "write the plan with `lattice plan write <task> --file <path>`."
- The lattice skill and the CLAUDE.md template teach `lattice plan write` and `lattice notes write` as the methods that work in every mode. They change in the same ticket that adds the commands, so every agent run after it gets them. Installed copies do not update themselves, so `remote attach` prints the refresh commands (§9.2).
- `lattice board write <path> (--file PATH | --stdin) [--expect-sha256 HEX]` → `board.file_write` writes one file with `atomic_write` (its commit point; a transaction on a server). `<path>` is relative to `.lattice/` and must be either under `orchestration/` (any depth) or a loose file directly under `plans/` or `notes/` whose name is not `<task_id>.md` for a task of the board; anything else is `VALIDATION_ERROR`. It creates missing directories under `orchestration/`. It works in both modes, so an orchestrator keeps its run-state and review packs on the board and every machine reads them from its cache. v2 has no remove: overwrite a file instead. The orchestration skills (outside this repository) switch to it for hosted boards (BUILDPLAN §6).
- `lattice context write (--file PATH | --stdin)` → `board.context_write` replaces `context.md` with `atomic_write` (its commit point; on a server it runs inside a transaction like any operation). Any token may run it. Board configuration stays admin-only in v2, through `lattice server project config` (§8.2), with three exceptions that any token may make because they are ordinary operations today: `board.set_project_code` and `board.set_subproject_code` (the `project_code` and `subproject_code` keys) and `board.set_dashboard_config` (the `dashboard` keys today's settings POST accepts, `dashboard/server.py:1166`). Every other key (workflow, review modes and toggles, completion policies, hooks) is refused over the operation path with `FORBIDDEN` and changes only through `project config`.

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

- `op`, `op_id`: set by the writer. `op_id` is a client-generated ULID with prefix `op_`, one per operation call, reused only when that same call is retried (§8.6). A command that performs several operations (for example `status` followed by `task.record_auto_review`) generates one `op_id` for each. An HTTP caller may omit it; the server then mints one (§8.4).
- `reported`: `host` (`socket.gethostname()`), `os_user` (`getpass.getuser()`), and `client_version` (the Lattice version) are collected once per process and cached. `worktree` and `branch` are derived **per operation**, because one process (the MCP server, a long-lived dashboard, a review subprocess) can serve several checkouts and outlive a branch switch. The operation's starting directory is the directory board resolution began from: the cwd for a CLI command, the `lattice_root` argument for an MCP tool call. `worktree` is the nearest ancestor of that directory holding `.git` (the filesystem walk `_caller_git_worktree` already does, `cli/task_cmds.py:59-65`). `branch` is read from that worktree's `HEAD` (following a `.git` file's `gitdir:` for a linked worktree): `ref: refs/heads/<name>` gives `<name>`; a detached `HEAD` omits `branch`. Only when `HEAD` cannot be read this way does the client fall back to `git rev-parse --abbrev-ref HEAD`. Any field whose lookup fails is omitted, never an error. A write made from a dashboard (local or hosted) carries `"source": "browser"` and no `worktree` or `branch`. Local mode records `reported` too (AC-36).
- On a server, `reported` must be an object of strings drawn from these six keys; each value is at most 256 characters (1,024 for `worktree`) and holds no control character (U+0000 to U+001F, U+007F to U+009F), and `source`, when present, is `browser`. Anything else is rejected with `VALIDATION_ERROR` before the operation runs.
- `authenticated`: stamped only by a server, from the token (§8.3). The server discards any `authenticated` a client sends (AC-37).
- Resource events carry the same `origin`.
- `lattice show --events` prints, per event, `actor · user@machine · worktree (branch)`, using `authenticated` when present, else `reported`; a browser write shows `browser` in place of `worktree (branch)`. `--json` output includes `origin` verbatim. The dashboard's task event view shows the same line (AC-38).
- Other people's text reaches a hosted checkout's terminal. On a hosted checkout, plain (non-`--json`) output replaces every control character other than newline and tab with U+FFFD in any string read from the board. Local output is unchanged.

---

## 5. Short IDs

LAT-280 and LAT-269, generalized.

- **Floor.** Allocation computes `next = max(ids.next_seqs[prefix], 1 + max short-ID sequence observed for prefix in every task log, active and archived)`, then skips any ID present in the map. A short ID that appears in any task log is never issued to another task (AC-2).
- **Server.** Each project keeps `max_observed[prefix]` in memory, computed from the logs at project load and updated on every allocation, so a server create does not rescan. Allocation runs under the project lock.
- **Local.** The floor is computed per create by reading each task log's creation event and any short-ID-assignment events. It runs under the board-wide allocation locks, so it must stay cheap: it adds no more than 100 ms to a create on a 1,000-task board, checked by the perf suite (`EVALUATION.md` §1). The implementation may cache each log's contribution to the floor, keyed on that log's `(st_size, st_mtime_ns)`, and must re-read any log whose key changed. A directory-mtime cache is not allowed: appending a `task_short_id_assigned` event to an existing log changes no directory entry, so such a cache would miss it and reissue the ID.
- **Doctor.** `_validate_authoritative_short_ids` collects every problem instead of raising on the first. Doctor reports every unresolvable short ID, every short ID held by two tasks, and a `next_seqs` value at or below the maximum observed in the logs (AC-28).
- **Repair.** `rebuild --all` already rebuilds `ids.json` under the allocator lock with the log floor. Import (§11) runs the same repair.

---

## 6. Board ownership

### 6.1 Path classes

Every path under a `.lattice/` directory belongs to exactly one class. The class decides whether it syncs, whether writes to it are checked and recorded, and whether it may be deleted. A path that matches none of the first six rows is unmanaged; real boards hold such paths (`reviews/`, `exports/`, `logs/`, `runner.log`).

| Class | Paths | Synced | Marker-checked and recorded | Deletion on a hosted board |
|---|---|---|---|---|
| Durable board data | `tasks/`, `events/`, `archive/`, `plans/`, `notes/`, `artifacts/`, `resources/`, `sessions/`, `templates/` (review prompt overrides, `src/lattice/templates/__init__.py:8-21`), `config.json`, `ids.json`, `context.md`, `.gitignore` | yes | yes | only as §7 permits |
| Workspace | `orchestration/` (any depth): an orchestrator's run-state and working files | yes | yes, written only through `lattice board write` (§3.9) | only as §7 permits |
| Runtime | `locks/`, `review_state/`, `tmp-prompts/`, `.daemon/` | no | no | allowed |
| Temporary | `atomic_write` temp files (`.tmp.*` beside their target, `storage/fs.py:45`) | no | no | allowed |
| Server control | `hosted/` (owner lease, journal, receipts, undo logs, control requests, maintenance record) | no | server and offline maintenance only | never by a client |
| Cache control | `cache/` (`state.json`, the cache marker; `applying`; `follower.json`; `unreachable_until`; `acked.jsonl`; `server_info.json`; `rescued/`) | no | syncer, follower, and hosted client only | allowed |
| Unmanaged | every other path | no | no | allowed |

Unmanaged paths are never synced, never imported (§11), never committed to the audit history (§8.10), and stay writable in a cache. Every file under a durable directory is durable, including files that are not `<task_id>.md` under `plans/` and `notes/` (loose files such as review packs). On a cache, a loose file directly under `plans/` or `notes/` is written through `lattice board write`; nested or archived loose files that an import carried over stay read-only, and new editable working files belong in `orchestration/`. Workspace files are durable in every other respect: synced, imported, audited, and read-only on a cache.

### 6.2 Markers

Two markers make "one writer" structural (G-1, AC-3):

- **Server-owned board:** `<board>/.lattice/hosted/owner.json` (`{server_id, host, pid, started_at}`) plus an exclusive `fcntl.flock` on `<board>/.lattice/hosted/owner.lock`, held for the server's lifetime and released by the kernel on death. A second server fails to start on a held board. A durable-path write to a board with `hosted/owner.json` fails with `BOARD_IS_HOSTED` unless it runs inside the owning server process (an in-process flag set by the server).
- **Client cache:** `<checkout>/.lattice/cache/state.json`, or `cache/applying` while a first sync has not yet written `state.json` (§9.4). A durable-path write fails with `BOARD_IS_CACHE` ("this is a read-only mirror of `<alias>/<project>`; writes go through the server") unless it comes from the cache syncer. Runtime and temporary paths stay writable, so reads (which take lock files) work on a cache.
- The check lives in the storage write primitives (`atomic_write`, `jsonl_append`, the placement copy and unlink, and a new `ensure_dir` for creating directories under a board), so no command can bypass it. Every durable write in `src/` must use one of them (H-8 audits this, and a default-suite test keeps it true, §14 G-1). The owner flag and the syncer flag are `contextvars` values, never process globals.
- **Board confinement.** The same primitives refuse, with `BoardPathError`, any path whose resolved form (`Path.resolve()`) is not under the resolved `.lattice/` directory of the board being written. This is the backstop for the input checks of §3.1: a name that slipped past them still cannot write outside its board or reach another project's.
- `fcntl` is imported only inside the functions that take a hosted lock (owner lease, cache locks), never at module top level, so local Lattice keeps working on Windows. Hosted mode needs a POSIX platform (macOS or Linux); on another platform `remote attach`, `sync`, `server init`, `server serve`, and any command that resolves a hosted root fail with `HOSTED_UNSUPPORTED_PLATFORM`.
- **Offline maintenance:** a durable write to a server-owned board is also allowed when the command runs with `--offline-maintenance` and holds the owner flock itself (§3.5).
- Stale owner markers: `owner.json` whose flock is free is stale (a crashed server, a finished offline maintenance, or an unloaded project). The next server or `project load` takes it over and logs the takeover. `lattice server project unlock <slug>` removes a stale marker when no server holds the flock.

---

## 7. Tombstones and the no-delete rule

- `lattice erase <task> --reason TEXT` → `task.erase` → appends `task_tombstoned` `{reason}`. Snapshot gains `tombstoned: true`, `tombstoned_at`, `tombstone_reason` (present only when tombstoned). Nothing is removed from disk.
- Tombstoned tasks are excluded from `list`, `next`, stats, and dashboard boards by default. `list --include-tombstoned` shows them. `show` works and prints `ERASED: <reason>`. Any further write returns `TASK_ERASED`, except `lattice unerase <task> --reason TEXT` → `task.unerase`, which appends `task_untombstoned` `{reason}`; the tombstone fields leave the snapshot and the task returns to every view in the status it had. Both events stay in history, so any erase can be undone with one command.
- **No-delete rule on hosted boards (G-2, AC-27):** the only removals of durable board data permitted under a hosted board are (a) archive and unarchive relocation, which copies before it unlinks the source; (b) session-end relocation into `sessions/archive/`, also copy-first; and (c) rollback of an uncommitted operation from its undo log (§8.6), which restores pre-images and so may truncate or remove only what that same operation wrote. Rollbacks are logged. Runtime and temporary paths (§6.1) are not board data. `doctor --fix` is `LOCAL_ONLY`.
- **Doctor:** reports any task referenced by `_lifecycle.jsonl` or `ids.json` whose log file is absent (`missing_task_file`). A tombstoned task keeps its files, so a missing file is always a finding.
- **Enforcement:** in server tests, the write recorder (§8.5) fails the test on any unlink under a board that is not one of the permitted cases.

---

## 8. The server

### 8.1 Install and process

- Install: `uv tool install 'lattice-tracker[server]'` (or pip). The `server` extra adds `starlette`, `uvicorn`, `sse-starlette`, pinned to the ranges the `mcp` extra already resolves. The base install gains nothing (AC-30, G-4).
- Run: `lattice server serve [--root PATH] [--host H] [--port P]`. Foreground. uvicorn, one process, `--workers 1`. Without the extra, it exits 1 with an install hint. Other `lattice server` admin commands need no extra.
- Server root: `--root`, else `$LATTICE_SERVER_ROOT`, else `$XDG_DATA_HOME/lattice-server` (default `~/.local/share/lattice-server`).
- Projects load lazily: a project runs its load (§8.7) on its first request, and a background prewarm thread loads every project in slug order once the server starts listening. A request that arrives while its project loads waits in admission (§8.5). The server answers `/healthz` from its first second.

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
  "log_level": "info",
  "audit": {"enabled": true, "debounce_seconds": 5, "max_interval_seconds": 60, "push": null},
  "limits": {
    "max_body_bytes": 16777216,
    "inline_file_bytes": 1048576,
    "lock_timeout_seconds": 30,
    "max_inflight_per_token": 8,
    "token_ops_per_minute": 600,
    "token_body_bytes_per_minute": 268435456,
    "max_event_data_bytes": 65536,
    "max_stream_subscribers_per_project": 64,
    "stream_queue_entries": 1000,
    "replay_reset_entries": 1000,
    "min_free_disk_bytes": 1073741824
  },
  "stream": {"heartbeat_seconds": 2}
}
```

`audit.push` is `null` or `{"remote": "<git remote name>", "branch": "<branch>"}` applied per project (a project's `.lattice/hosted/audit.json` may override it). `trusted_proxy: true` makes the server honor `X-Forwarded-Proto` and `X-Forwarded-For` for cookie security and logs. `public_origins` lists the browser origins (for example `https://lattice.example.internal`) accepted by the dashboard's `Origin` check when a proxy rewrites `Host`. `limits.lock_timeout_seconds` may not exceed 60.

**Limits.** No project is isolated from the host: all projects share one process, one disk, and one memory. These limits bound what one credential can take from the others:

- `max_body_bytes` is enforced while the body streams in: a `Content-Length` over the limit is refused before reading, and a body without one is refused as soon as it passes the limit (`PAYLOAD_TOO_LARGE`). No body is buffered past the limit.
- `max_inflight_per_token`: the authenticated requests (open streams excluded) a token may have in progress at once, across all projects. One more gets 429 `RATE_LIMITED` with `Retry-After: 1`.
- `token_ops_per_minute` and `token_body_bytes_per_minute`: each token has two buckets that refill continuously at these rates and hold at most one minute's worth. An operation request takes one unit from the first; every request's body takes its `Content-Length` (or, without one, the bytes as they arrive) from the second. A request that would overdraw either gets 429 `RATE_LIMITED`, with `Retry-After` set to the whole seconds until it would fit.
- `max_event_data_bytes`: on a server, `task.event` refuses `data` whose canonical JSON exceeds it (`PAYLOAD_TOO_LARGE`).
- `max_stream_subscribers_per_project`, `stream_queue_entries`, and `replay_reset_entries` bound the stream (§8.9).
- `min_free_disk_bytes`: the free-space floor for writes (§8.11).

The body, in-flight, and rate checks run before admission (§8.5), so a request they refuse never waits for a project lock.

### 8.2 Admin CLI

All under `lattice server`; all accept `--root`; all have `--json`.

| Command | Effect |
|---|---|
| `init [--root]` | Create the root, `server.json`, empty `tokens.json` (0600). Idempotent. |
| `serve` | Run the server (§8.1). |
| `project create <slug> [--code CODE] [--subproject-code C] [--review-mode M] [--plan-review-mode M] [--plan-approval A] [--auto-code-review/--no-auto-code-review] [--auto-plan-review/--no-auto-plan-review]` | Create `projects/<slug>/.lattice/` exactly as `lattice init` would with the same options, then the journal (epoch, seq 0) and audit repo. `--review-mode`, `--plan-review-mode`, and `--plan-approval` take `init`'s choices; the two toggles set `auto_code_review_on_transition` and `auto_plan_review_on_transition` (default on, as `init`). Slug: `^[a-z0-9][a-z0-9-]{0,62}$`. |
| `project import <slug> --from DIR [--code CODE]` | §11. The source's `config.json`, review settings included, is imported unchanged. |
| `project config <slug> --set KEY=VALUE...` | Change the project's review workflow (below). |
| `project list` | Slug, project code, head seq, task count, state (`loaded`, `loading`, `unloaded`, `unavailable`), owner. |
| `project unload <slug>`, `project load <slug>`, `project reload <slug>` | Release one project's lease so offline maintenance can run on it, take it back (running §8.7), or both in one step (for example to retry recovery after freeing disk). The other projects keep serving. |
| `project doctor <slug>` | Run `lattice doctor`'s read-only checks on the project under its work lock and print the findings, so they never race a transaction. |
| `project unlock <slug>` | Remove a stale owner marker (§6). |
| `project rotate-epoch <slug>` | Start a new journal epoch (below). |
| `project recover <slug> --rollback \| --keep` | Resolve undo logs left without a journal (§8.7 step 3): roll them back, or keep the files as they are and delete the logs. Requires the owner flock to be free. |
| `token create --user human:NAME --machine LABEL [--actor PATTERN]... [--project SLUG]... [--all-projects]` | Mint a token; print it once, with its actor patterns. With no `--actor`, the patterns are `[<user>, "agent:*"]` (a person and that person's agents); any `--actor` replaces that default entirely, so a seat token is minted with exactly one `--actor`. If no `--actor` pattern matches `--user`, print a warning: dashboard writes will not act as the user (§8.3). |
| `token grant <token_id> (--project SLUG \| --actor PATTERN)...`, `token ungrant <token_id> (--project SLUG \| --actor PATTERN)...` | Add or remove projects or actor patterns on an existing token, so adding a project needs no new secret. Effective on the next request. |
| `token list` | Id, user, machine, actors, projects, created, revoked. Never the secret. |
| `token revoke <token_id>` | Set `revoked_at`. Effective on the next request (AC-13). |

Admin commands edit files the running server reads, under `<server_root>/admin.lock` with `atomic_write`. The server reloads `tokens.json` whenever its `(st_mtime_ns, st_size, st_ino)` differs from the last load, checked per request, so two edits within one mtime tick are never missed. Admin commands never write a board while a server owns it: the actions below go through a control request, or, with the owner flock free, take the flock themselves.

**Control requests.** Every admin command that acts on a project a server owns (`project config`, `unload`, `load`, `reload`, `doctor`, `rotate-epoch`) writes `hosted/control/<ULID>.json` (`{"action", ...}`) into that project and waits up to 30 s for `<ULID>.done` (`{"ok", "result" | "error"}`), which it prints and deletes. The server checks `hosted/control/` of every project directory at each admission and every 2 seconds, including unloaded projects, and runs each request under that project's locks. Whether a server is running is decided by the server's own lease, not the project's: a running server holds an exclusive flock on `<server_root>/server.lock` for its lifetime. While it does, every control request goes through it, including `load` for a project it has unloaded (whose own owner flock is then free). When no server holds `server.lock`, `project config`, `rotate-epoch`, and `doctor` act directly, as described per action, and `unload`, `load`, and `reload` fail with a message that no server is running.

- **`project config <slug> --set KEY=VALUE...`** accepts only these keys, with `init`'s choices: `review_mode` and `plan_review_mode` (`inline`, `single`, `triple`), `plan_approval` (`auto`, `human`), `auto_code_review_on_transition` and `auto_plan_review_on_transition` (`true`, `false`). Any other key or value is refused before anything is written. The server applies it as a transaction (§8.6) that rewrites `config.json` with `atomic_write` and appends a journal line with `op: "server.set_config"`, `token_id: null`, and `paths: ["config.json"]`, so every cache receives the new config at its next sync. With the flock free, the command takes the flock, rewrites `config.json`, and writes `hosted/maintenance.json`, so the next load rotates the epoch and every cache resyncs.
- **`project unload <slug>`** waits for the project's in-flight operation, closes its streams, and releases its owner lease; until `load`, every route for it returns 503 `BOARD_UNAVAILABLE` ("unloaded"). **`load`** acquires the lease and runs §8.7, which also clears a quarantine (§8.6). **`reload`** is `unload` then `load`.
- **`project rotate-epoch <slug>`** starts a new journal epoch so every cache resyncs (run it after restoring a backup, before clients connect; §13). With the owner flock free it rotates directly, but refuses while any undo log exists: load the project once so recovery settles them, or run `project recover`. Through a control request, the server performs the rotation under the project's locks and broadcasts `reset` (§8.9).

Rotation is itself recoverable. In order, each step fsynced: (1) write `hosted/rotation.json` `{old_epoch, new_epoch}`; (2) rename `journal.jsonl` to `hosted/journal.<old epoch>.jsonl` (kept, never deleted); (3) write a new `journal_meta.json` with `new_epoch` and a `baseline` of the current log lengths; (4) create an empty `journal.jsonl`; (5) delete `rotation.json`. Each step is idempotent, so startup completes an interrupted rotation from the marker before anything else (§8.7). The new epoch starts at seq 1.

### 8.3 Tokens and identity

- Token string: `lat_<token_id>_<secret>`, where `token_id` is `tok_` + ULID and `secret` is 32 random bytes, base64url. `tokens.json` stores `{id, sha256_hex(secret), user, machine, actors: [patterns], projects: [slugs] | ["*"], created_at, revoked_at}`. Comparison uses `hmac.compare_digest`. Secrets never appear in logs or error messages (AC-14, G-7).
- A token is issued to one person (`user`, a `human:` actor) for one machine or seat (`machine`, a free label). `user` and `machine` are therefore authenticated and stamped into `origin.authenticated` (AC-37).
- `actors` are `fnmatch` patterns over actor base IDs (`agent:*`, `agent:owner-3`, `human:alice`). A request's actor must match one pattern, else `ACTOR_NOT_PERMITTED` (AC-12), whose message lists the token's patterns and the admin command that widens them: "actor `agent:x` is not permitted for token `tok_…`; it may act as: `human:alice`. An admin can widen it with `lattice server token grant tok_… --actor '<pattern>'`." A session actor is checked by its permission identity `agent:<base_name>` (§3.7), before the session is touched.
- Default actor: when a request carries no actor and the token has exactly one pattern with no wildcard, that is the actor (the user of a default person token; the single actor of a seat token). Otherwise `MISSING_ACTOR`.
- **Browser actor.** A write from a dashboard acts as the token's `user` when one of the token's patterns matches it, else as the token's default actor, else fails with `MISSING_ACTOR`. So a person token listing `human:alice` and `agent:*` writes from the browser as `human:alice`. The hosted dashboard applies this rule on the server (§10). The local dashboard on a bound checkout applies it on the client, from the identity `/v1/info` returns, and sends that actor explicitly.
- Built-in allowance: every token may also act as `agent:lattice-auto-review` (`core/auto_review.py:26`), so auto-review works under strict tokens (§3.4). Review subprocesses already write as the caller's own actor.
- Auth header: `Authorization: Bearer <token>`. Missing or invalid: 401. Valid but project not listed: 403 (AC-11).

### 8.4 HTTP API (protocol 1)

Every response carries `Lattice-Server-Version`, `Lattice-Min-Client-Version` (§15), and `Lattice-Protocol: 1`. Every `/v1` and `/p/{slug}/api/*` response also carries `Cache-Control: no-store`, so no proxy caches board data. JSON bodies use the CLI envelope: `{"ok": true, "data": ...}` or `{"ok": false, "error": {"code", "message", "details"?}}`.

| Method and path | Auth | Purpose |
|---|---|---|
| `GET /healthz` | none | `{"ok", "version", "protocol": 1, "disk_free_bytes", "projects": {"loaded", "loading", "unloaded", "unavailable"}}` (counts only, never slugs). `ok` is false, with status 503, when `disk_free_bytes` is below `limits.min_free_disk_bytes` |
| `GET /v1/info` | token | Server version, protocol, `min_client_version`, `stream_heartbeat_seconds`, the caller's identity (token id, user, machine, actors), visible projects, registered ops with their parameter names, registered event types, audit state |
| `GET /v1/projects` | token | Visible projects: slug, project code, head seq |
| `POST /v1/projects/{slug}/ops/{op}` | token | Execute an operation (§8.6) |
| `GET /v1/projects/{slug}/ops/{op_id}` | token | The outcome of one of the caller's own operations (§8.6): `{"state": "committed", "epoch", "seq", "result"?}` or `{"state": "not_found"}` |
| `GET /v1/projects/{slug}/sync?since=N&epoch=E&hash=H` | token | Cache delta (§8.8) |
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

`op_id`, `actor`, `actor_name`, `attestations`, and `expect` are optional. `params` omits every parameter whose value equals the operation's declared default (§15). Without `op_id`, the server mints one and does not deduplicate: a repeated request without `op_id` applies again. The Lattice client always sends one. Op response: `{"ok": true, "data": {"result": <OpResult as JSON>, "seq": <journal seq>, "op_id": <the op_id used>}}`.

Request headers: `Authorization`, `Content-Type: application/json` (required for POST), `Lattice-Protocol: 1` (optional; if present and different, `PROTOCOL_MISMATCH` before anything runs), `Lattice-Client-Version` (optional; the Lattice client always sends it; if it is below the server's `min_client_version`, an op request fails with `CLIENT_TOO_OLD` before anything runs).

### 8.5 Serialization and the write recorder

- **Limits, then admission, then work.** A request first passes the per-token limits (§8.1), which answer at once. Each project has an `asyncio.Lock` (admission) and a `threading.Lock` (work). A request then awaits the project's admission lock on the event loop, with a timeout of `limits.lock_timeout_seconds` (`BOARD_BUSY` on expiry). Only after admission does it take a worker thread (`anyio.to_thread.run_sync`) and the work lock. Waiting requests therefore never occupy worker threads, and a stalled project cannot starve another (AC-15). The audit committer thread takes only the work lock.
- Everything that reads or writes a board's durable files runs under the work lock. Requests (operations, sync assembly, file reads, hosted dashboard reads) pass admission first; the audit committer thread takes only the work lock. Because an operation holds the work lock for its whole transaction, audit staging never sees a partial transaction. Two kinds of request skip admission because they read only memory or files that never change: a sync whose `since` is the head (§8.8), and an op-status lookup (§8.6).
- A **write recorder** in `storage/fs.py` records every durable path (§6.1) written, appended, or unlinked by the storage primitives, and calls a registered callback *before every* durable mutation with the path and the mutation kind (`append`, `create`, `replace`, `unlink`). On a server, the transaction's callback decides which undo entries that requires (§8.6). It is a `contextvars` value created inside the worker thread by the op executor, and the executor returns its path set alongside the `OpResult`. It never crosses threads implicitly.

### 8.6 Transactions, journal, and receipts

Every operation on a server is a transaction, run under the project's work lock. Three server-control files carry it, all append-only JSONL with one fsync per line: the undo log `hosted/undo/<token_id>--<op_id>.jsonl`, the receipt file `hosted/receipts/<UTC YYYY-MM-DD>.jsonl`, and the journal. An operation's identity is the pair `(token_id, op_id)`, because two tokens may send the same `op_id`. A transaction the server starts itself (`project config`, §8.2) uses a server-minted `op_id`, `token_id: null` in its journal line, and `server` in place of the token ID in its undo log name. The server checks a client's `op_id` against the pattern of §3.1 before step 1, so a malformed one never names a file.

1. **Begin.** Record the byte lengths of the journal and of today's receipt file (in memory), then create the undo log, whose first line is `{"epoch": <current epoch>, "token_id", "op_id"}`.
2. **Undo entries.** The storage primitives append an entry to the undo log, and fsync it, *before* each change they guard. The log is a sequence, replayed in reverse on rollback:
   - before the first append to a path in this operation: `{"path", "kind": "length", "existed", "length"}`;
   - before creating, replacing, or unlinking a path, unless this operation already recorded a `content` entry for it: `{"path", "kind": "content", "existed", "content_b64"}` holding the path's bytes at that moment (so a log appended to and then unlinked by archive placement gets a `length` entry and then a `content` entry).

   A torn final undo line guards a change that was never made (the change waits for the fsync), so it is ignored.
3. **Work.** The operation runs, writing durable files through the normal storage code.
4. **Receipt.** Append `{"op_id", "token_id", "fp", "epoch", "seq", "result"}` (the full `OpResult` JSON) to the receipt file.
5. **Commit point.** Append the journal line and fsync. In-process, the operation counts as committed only when both the write and the fsync succeed. At startup, the only evidence is on disk: a complete journal line means committed (§8.7).
6. **Finish.** Update the in-memory state (the idempotency index, the op-status map, the line hash for this `seq` (§8.8), the length history of each log in `lengths`, and the manifest entries of each path in `paths`, hashed now), delete the undo log, then hand the entry to the stream broadcaster, all still under the locks, so streams see entries in `seq` order. Any failure here is handled by transaction recovery below, which never rolls back a committed operation.

**Failures: one recovery path.** On any failure after step 1 (an `OpError`, any other exception, or a failed write or fsync of a board or control file), the server runs **transaction recovery** for this operation before admitting another request to the project, then returns the error to the caller:

1. If the operation's journal line was written in full and its fsync succeeded, the operation is committed and is never rolled back: complete step 6 (index entry, undo deletion), and if publication failed, close the project's open streams so followers reconnect and replay from the journal (§8.9).
2. Otherwise it is uncommitted: truncate the journal and the receipt file back to the lengths recorded at step 1 and fsync them; roll back from the undo log by replaying its entries in reverse (a `content` entry restores the bytes, or removes the path if it did not exist; a `length` entry truncates the log to its length, or removes it if it did not exist; a torn final entry is ignored); delete the undo log.
3. If the journal fsync failed (durability unknown), or any step of recovery fails, mark the project `BOARD_UNAVAILABLE` (503 on every route for it) and log the reason. The quarantine lasts until the project's next load (`lattice server project reload <slug>`, §8.2, or a server restart), whose startup recovery decides from what is on disk (§8.7). Other projects keep serving.

A rejected operation therefore leaves nothing behind, including a session touch or a short-ID reservation. Most rejections happen before any write, so their recovery is only the deletion of an empty undo log.

**Durability errors propagate on the server.** Today `_fsync_directory` swallows `OSError` (`storage/fs.py`). Inside a server transaction, and for every server-control write, a failed file or directory fsync raises, so recovery can react. Local mode keeps today's behavior.

**Journal and metadata.**

- `<board>/.lattice/hosted/journal.jsonl`, one line per committed operation, including no-ops: `{"seq", "ts", "op", "op_id", "fp", "token_id", "task_id", "event_ids", "paths", "lengths"}`. `ts` has millisecond precision; `seq` starts at 1 per epoch and increases by 1. `paths` lists every durable path changed. `lengths` maps each append-only log the operation appended to its byte length afterward.
- `fp` is the request fingerprint: the first 32 hex characters of SHA-256 over the canonical JSON (sorted keys, separators `(",", ":")`) of `{"op", "params", "actor", "actor_name", "attestations", "expect_last_event_id"}`.
- `journal_meta.json`: `{"epoch": "ep_<ULID>", "created_at", "baseline": {<log path>: <byte length>}, "clean_shutdown": null | {"head_seq", "tree_fingerprint"}}`. `baseline` records every log's length when the epoch began; with `lengths` it gives the last known length of every log, which startup uses to detect foreign appends (§8.7).

**Replay (AC-46).**

- The idempotency index maps `(token_id, op_id) → (fp, epoch, seq, receipt location)` for the last 7 days of receipts. At load it is rebuilt from the receipt files: a receipt counts only if the journal of its `epoch` (the current `journal.jsonl` or a retained `journal.<epoch>.jsonl`) holds a line with the same `seq`, `token_id`, and `op_id`; any other receipt line is an orphan of an uncommitted operation and is removed. Receipt files older than 7 days are deleted (server control data, not board data); a retry of an operation older than that runs again. Epoch rotation does not affect deduplication.
- The server checks the index **after** admission (§8.5), so a retry that queued behind its own first attempt sees the committed result. A request that sent no `op_id` skips the check (§8.4).
- A known `(token_id, op_id)` with the same `fp` does not run again: the server returns the stored `OpResult` verbatim with `replayed: true`. A known `(token_id, op_id)` with a different `fp` returns the `OP_ID_REUSED` envelope (§3.1). The same `op_id` from another token is a different operation, so one token can never read or replay another's result.
- **Op status.** `GET /v1/projects/{slug}/ops/{op_id}` looks up `(caller's token_id, op_id)`. The server keeps an in-memory map from `(token_id, op_id)` to `seq` for the current epoch, built at load from the journal, and scans the retained `journal.<epoch>.jsonl` files (which never change) for older epochs. Found: `{"state": "committed", "epoch", "seq"}`, plus `result` while its receipt is retained. Otherwise `{"state": "not_found"}`: the operation never committed, or belongs to another token.
- **Client retries.** The client reuses the call's `op_id` and retries for up to `retry_seconds` (default 30, per remote, §9.1) on a connection error, a read timeout, HTTP 429, 502, or 504, and HTTP 503 unless the envelope's code is `BOARD_UNAVAILABLE`. It waits `Retry-After` when given, else backs off from 0.5 s, doubling to at most 5 s. The read timeout for operation requests is 90 seconds, above the maximum `lock_timeout_seconds` plus work time, so a slow admission is never mistaken for a lost request. When it gives up:
  - if no attempt's request was ever fully sent (every attempt failed to connect), the error is `SERVER_UNREACHABLE`: nothing was written;
  - otherwise the error is `OUTCOME_UNKNOWN`, exit 1: "the server may have applied this write (operation `op_…`); check with `lattice remote op-status op_…` before retrying". An agent that reruns the command without checking may apply it twice; the guide says so.

### 8.7 Startup, integrity, and crash recovery

At project load (its first request or the startup prewarm, §8.1; `project load` or `reload`, §8.2), under the project's locks, in this order:

1. **Lease.** Acquire the owner lease (§6.2). If `hosted/rotation.json` exists, finish that rotation's remaining steps.
2. **Journal tail.** Drop a truncated final line of `journal.jsonl` (its operation never committed). Drop torn final lines of receipt files and undo logs.
3. **Missing journal.** If `journal.jsonl` or `journal_meta.json` is missing or unparseable beyond its final line: with no undo logs present, rotate the epoch; with undo logs present, mark the project `BOARD_UNAVAILABLE` and log that `lattice server project recover <slug> --rollback | --keep` must decide (committed state cannot be known without the journal).
4. **Transactions.** For each undo log in `hosted/undo/`: if the journal of the epoch named on its first line (the current `journal.jsonl`, or `journal.<epoch>.jsonl` if that epoch was rotated away) holds a complete line with the same `token_id` and `op_id` (a `server` undo log matches `token_id: null`), the operation committed; delete the undo log. Otherwise roll it back (§8.6) and log `recovery_rollback`. Then rebuild the idempotency index (removing orphan receipts) and the op-status map (§8.6).
5. **Maintenance and restores.** If `hosted/maintenance.json` exists, rotate the epoch and remove it. Else if `clean_shutdown` is set and the durable tree's fingerprint (a hash over each durable file's path, size, and mtime) differs from it, rotate the epoch. Clear `clean_shutdown`.
6. **Foreign changes.** Any log longer than its last known length (§8.6), or any durable file changed outside the journal as detected by step 5's fingerprint when the epoch was not rotated, gets an `external` journal entry listing the paths, and a warning log.
7. **Discovery.** Run strict discovery. If it fails (for example a foreign writer left a truncated line), mark the project `BOARD_UNAVAILABLE` (503 on every route for it), log the failing path, and keep serving other projects. The admin unloads the project, repairs it with offline maintenance (`doctor --fix --offline-maintenance`), and loads it again (§8.2), with no server restart.
8. Compute the `max_observed` short-ID floors, and build the in-memory state the sync path uses: each current-epoch journal line's hash, each log's length history from `baseline` and `lengths`, and the manifest (§8.8).

At graceful shutdown the server writes `clean_shutdown` for each project after its final audit commit.

`config.json` and `context.md` changed by hand while the server runs are detected by mtime at each admission and journaled as `external` entries, so caches receive them.

Unknown-event-type warnings go through a module-level reporter in `core/tasks.py` whose default prints to stderr exactly as today; the server installs a reporter that logs once per (project, type) per process.

### 8.8 Sync

`GET /v1/projects/{slug}/sync?since=N&epoch=E&hash=H`:

- **Line hashes.** A journal line's hash is the first 32 hex characters of SHA-256 over the line's exact bytes, without the trailing newline. The server keeps every current-epoch line's hash in memory. Every sync response carries `head_hash`, the hash of the line at `head_seq` (absent at seq 0). The client stores it and sends it back as `hash`.
- If `epoch` matches, `N` ≤ head, and `hash` equals the hash of the server's line `N` (or `N` is 0): return `{"epoch", "head_seq", "head_hash", "reset": false, "files": {...}, "removed": [...]}` covering every path in journal entries `N+1..head`, coalesced to the current content of each path. When `N` is the head, the server answers from memory before admission (§8.5), with empty `files` and `removed`.
- If `epoch` is absent or differs, `N` > head, or `hash` differs from the server's line `N` (the server's history is not the one the client saw, for example after a restored backup): `reset: true` and every board file (AC-23). A reset inlines files until a cumulative 32 MiB of content and returns the rest as `href`.
- `files[path]` is `{"sha256", "size", "content_b64"}` when `size ≤ limits.inline_file_bytes`, else `{"sha256", "size", "href"}` where `href` is the files endpoint with `?sha256=<hash>`. The files endpoint returns 412 `STALE_VERSION` when the file no longer has that hash; the client then re-runs sync from its current `head_seq`.
- **Append deltas.** An append-only log (any path the journal records in `lengths`) grows only at its end, so a delta sends only the new bytes when it can. Let `L` be the log's length as of seq `N`: its latest `lengths` value at or before `N` in the current epoch, else its `baseline` length. When the log existed at `N` and `size - L ≤ limits.inline_file_bytes`, `files[path]` is `{"sha256", "size", "append_from": L, "content_b64": <bytes L..size>, "href"}`, where `sha256` and `size` describe the whole file and `href` fetches the whole file. A log that did not exist at `N`, and every reset, use the whole-file forms above.
- Only durable board paths (§6.1) are ever returned.
- Path traversal is rejected; every path must resolve under the board.
- **Manifest.** The server keeps an in-memory manifest of every durable path (`sha256`, `size`), built at load and updated from each committed transaction's `paths` (§8.6). Resets and the `manifest=1` form (hashes only, §9.6) take hashes and sizes from it and never rehash files under the locks.
- The response (head, then file bytes and hashes) is assembled under the project's locks, so it reflects exactly the state at `head_seq` and never a write in progress. The files endpoint also reads under the locks. Inline limits keep lock hold times short. A project assembles at most one reset at a time: a second reset request waits for the first to finish before it seeks admission.
- **Supported size.** v2 supports boards of up to 2,000 tasks (active and archived) and 200 MiB of durable data, with no single log over 8 MiB. Within that envelope, a follower of a board whose largest log is appended to continuously converges, because each delta carries only appended bytes. The torture suite tests at the envelope (`EVALUATION.md`, AC-7).

### 8.9 Stream

`GET /v1/projects/{slug}/stream` (SSE, `sse-starlette`):

- A new stream subscribes to the project's broadcaster first, then replays journal entries after its resume point, then delivers live entries, dropping any `seq` it has already sent. Entries arrive in `seq` order with no gap and no duplicate (AC-22).
- Resume point: `Last-Event-ID: <epoch>:<seq>:<line hash>` header or `?since=<seq>&epoch=<epoch>&hash=<line hash>`. If the hash differs from the server's line at that seq (§8.8), or more than `limits.replay_reset_entries` (1,000) entries separate it from head, the server sends `reset` instead of replaying.
- Each entry: `id: <epoch>:<seq>:<line hash>`, `event: journal`, `data:` the journal entry plus `"events"`: the full appended events, read back from the logs by `event_ids`.
- Epoch mismatch, history mismatch, or rotation: one `event: reset` with the new epoch, then live entries (AC-23).
- **Heartbeat:** `event: heartbeat` with `data: {"epoch", "head_seq"}` and no `id`, sent immediately on connect and then every `stream.heartbeat_seconds` (default 2). It tells a follower the head even when a proxy drops or holds entries (§9.6).
- The credential is rechecked at each heartbeat; a revoked token or expired session closes the stream (AC-13).
- **Bounds.** A project accepts at most `limits.max_stream_subscribers_per_project` open streams; one more gets 429 `RATE_LIMITED`. Publication never blocks: each subscriber has a queue of at most `limits.stream_queue_entries` entries, and a subscriber whose queue is full is disconnected, to resume later from its `Last-Event-ID`. A slow reader therefore never delays the project's writes.

### 8.10 Audit history

- With `audit.enabled` and `git` on `PATH`, each `projects/<slug>/` is a git repository (created at `project create`/`import`). The board's own logs are the audit record, so the history holds durable board data (§6.1) and nothing else. Its `.gitignore` is an allowlist: it ignores everything under `.lattice/` (`/.lattice/*`) and then re-includes exactly the durable paths of §6.1 (`!/.lattice/tasks/`, `!/.lattice/events/`, and so on through `!/.lattice/.gitignore`). `hosted/` (journal, receipts, undo logs, control requests), runtime, and unmanaged paths therefore never enter it.
- A per-project committer thread stages (`git add -A`) under the project lock, then commits outside it, `debounce_seconds` after the last write and at most `max_interval_seconds` after the first uncommitted one. Message: `audit: seq <a>-<b> (<n> ops)`. Author: `Lattice Hosted <lattice-hosted@localhost>`. After each commit, still outside the lock, it runs `git gc --auto`.
- If `push` is configured, push after each commit. Failures are logged and retried at the next commit; they never block writes (AC-26).
- If `git` is missing, audit is disabled with one startup warning, and `/v1/info` says so. Shutdown makes a final commit.
- **Restoring from the audit history** is an import, not a restore: check the commit out into a scratch directory and run `project import` from it (§11), which starts a new epoch. The history carries no journal, so it cannot resume the old one.

### 8.11 Logging, health, shutdown

- One JSON object per line to stdout, at `server.json` `log_level` (`debug`, `info`, `warning`; default `info`). Request lines: `{ts, level, event: "request", method, path, status, duration_ms, project, op, op_id, token_id, actor, seq, error_code}`, at `info`, except a sync that returns no change, which logs at `debug` (idle followers and every read's catch-up would otherwise dominate the log). Lifecycle lines for startup, project load, recovery, audit, lease, and config reload. Never token secrets, payload contents, or plan text; sizes only (AC-31, G-7). The service templates ship with log-rotation snippets (§13).
- `GET /healthz` is unauthenticated and touches no board. It reports free disk and project counts by state (§8.4).
- **Disk floor.** At admission, an operation request is refused with 507 `STORAGE_LOW` when free space on the server root's filesystem (`os.statvfs`, read at most once a second) is below `limits.min_free_disk_bytes`, before any fsync can fail. Reads, syncs, and streams keep working.
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
      "run_board_hooks": false,
      "run_auto_reviews": true,
      "allow_plaintext": false,
      "retry_seconds": 30
    }
  }
}
```

- `token` is a literal string or `{"env": "VAR"}`. Header values are always `{"env": "VAR"}`, never literals (AC-20). `run_board_hooks` (default `false`) opts this machine into running the hosted board's hooks (§3.4). `run_auto_reviews` (default `true`) lets this machine decline the hosted board's auto-reviews (§3.4). `allow_plaintext` (default `false`) permits an `http://` URL whose host is not loopback (§9.1 transport). `retry_seconds` (default 30) bounds the retries of one operation (§8.6).
- Environment overrides, for environments with no config file (thin clients): `LATTICE_REMOTE_<ALIAS>_URL`, `LATTICE_REMOTE_<ALIAS>_TOKEN`, `LATTICE_REMOTE_<ALIAS>_HEADERS` (a JSON object mapping header name to the name of the environment variable holding its value), `LATTICE_REMOTE_<ALIAS>_ALLOW_PLAINTEXT` (`1` to allow). `<ALIAS>` is uppercased with non-alphanumerics replaced by `_`. Environment wins over the file.
- `lattice remote add <alias> <url> [--token-env VAR | --token-stdin] [--header NAME=ENVVAR]... [--allow-plaintext]` writes the file. `lattice remote list` shows aliases and URLs, never tokens.
- **First-contact errors.** A binding whose alias has no remote, in the file or the environment, fails with `REMOTE_NOT_CONFIGURED`, which prints the line to run: `lattice remote add <alias> <url> --token-env <VAR>`, and "ask your server admin for the URL and a token". A token or header given as `{"env": "VAR"}` whose variable is unset or empty fails with `TOKEN_ENV_UNSET`, naming the variable.

**Transport.** One policy covers every request the client makes (info, ops, op status, sync, files, stream):

- **No redirects.** Any 3xx response fails with `PROXY_REJECTED`, naming the status and the `Location` host, and is never followed. A proxy's login redirect therefore never receives the bearer token or the proxy headers. The client also attaches the `Authorization` header and every remote header with `add_unredirected_header`, so no redirect handler could copy them.
- **Only a Lattice server's answer counts.** Every response must carry `Lattice-Protocol`. A JSON endpoint's response must also be `application/json` with a parseable envelope, and the stream's must be `text/event-stream`. Anything else (a proxy's HTML login page, an error page) fails with `PROXY_REJECTED`, naming the status and content type; it is never read as success or as "unreachable".
- **Plaintext.** An `http://` URL whose host is not loopback (`localhost`, `127.0.0.0/8`, `::1`) fails with `INSECURE_URL` unless the remote sets `allow_plaintext` (for example a server reached over an encrypted private network). `remote add` refuses such a URL the same way unless given `--allow-plaintext`.

### 9.2 Binding

- `<repo>/.lattice-remote.json`, committed: `{"remote": "<alias>", "project": "<slug>"}`. No hostname, no credential (AC-18, G-3).
- `lattice remote attach <alias> <project>`, from the primary checkout or any linked worktree of it:
  1. Verify the alias resolves (`REMOTE_NOT_CONFIGURED` otherwise) and the project exists and is visible to the token.
  2. Resolve the primary checkout with the same worktree jump as `find_root` (§9.3). Everything below happens there, because every worktree routes through it.
  3. Refuse with `BINDING_CONFLICT` if the primary checkout holds a local board (a `.lattice/` with a durable file and no cache marker, §9.3); the message names the guide's move steps (§11). Refuse the same way if it holds a cache of a different remote or project; the message names `lattice cache clear --forget`.
  4. Write the binding. Add `/.lattice/` to the checkout's `.gitignore` (idempotent) and to `$GIT_COMMON_DIR/info/exclude` (idempotent), which covers every branch and worktree of the clone, including branches whose `.gitignore` lacks the line.
  5. Run the initial sync.
  6. Print what to commit (the binding and `.gitignore`) and the refresh commands `lattice setup-claude --force` and `lattice setup-claude-skill --force`: CLAUDE.md blocks and skills installed before v2 tell agents to write plan files directly, which a cache refuses (§3.9).
- `lattice remote status [--json]`: binding, URL, identity from `/v1/info`, cache epoch and seq, last sync time, follower state, and whether the cache is stale. It also lists every local and remote-tracking branch that still tracks files under `.lattice/` (`git for-each-ref` over `refs/heads` and `refs/remotes`, then `git ls-tree -r --name-only <ref> -- .lattice`), with the fix: merge the commit that untracked the board, or run `git rm -r --cached .lattice` on that branch. Checking such a branch out in the primary checkout makes git write board files into the read-only cache and stop partway, and merging it brings board files back into the merge.
- `lattice remote op-status <op_id> [--json]`: the outcome of one of this token's operations (§8.6), for a write that ended in `OUTCOME_UNKNOWN`.
- `lattice remote verify [--json]`: checks every acknowledged write this checkout recorded in `cache/acked.jsonl` (§9.5) against the server through op status. It prints each one the server does not hold and exits 1 if there is any. It keeps confirmed lines (marking them with the time last confirmed), so a later restore that loses an already verified write is still reported. Lines older than 90 days are dropped. It is the trial's daily lost-write check (`EVALUATION.md` §5).

### 9.3 Root discovery

`find_root` keeps its order (`LATTICE_ROOT`, then the linked-worktree jump to the primary worktree, then walk up). The worktree jump resolves a relative `gitdir:` against the directory of the `.git` file, not the process cwd (fixing `storage/fs.py:206-211`). Because the jump already resolves every linked worktree to the primary checkout, every worktree shares one binding and one cache with no setup (AC-10).

A directory is a **hosted root** when either holds:

1. **The machine-local marker:** `.lattice/cache/state.json` or `.lattice/cache/applying` (§9.4). The marker names the remote and project, so it routes by itself, whatever branch the checkout is on. Switching the primary checkout to a branch without the committed binding therefore keeps every worktree routed to the server. Both files are ignored, never committed, and never touched by git.
2. **The committed binding** `.lattice-remote.json`, with no `.lattice/` or a `.lattice/` holding no durable file. The binding bootstraps a fresh clone, or a teammate's clone after they pull the move, where git has deleted the tracked board files and left ignored runtime leftovers (`locks/`, `.daemon/`): that `.lattice/` is adopted as an empty cache, and the first sync fills it. This includes `LATTICE_ROOT` naming the directory before its cache exists.

`BINDING_CONFLICT` covers the two remaining cases: a binding beside a `.lattice/` that holds durable files and no marker (a local board; the message points at the guide's move steps, §11), and a marker whose remote or project differs from the binding's (the message names `lattice cache clear --forget`). The cache is the hosted root's `.lattice/`.

### 9.4 Cache

- Layout identical to a local `.lattice/` (AC-9), plus `cache/state.json` (the marker): `{remote, project, epoch, head_seq, head_hash, server_version, synced_at, fingerprint}`, written only by syncs; and `cache/follower.json`: `{pid, stream_live_until}`, written only by the follower. The other `cache/` files are named where they are used: `applying` (below), `unreachable_until` (§9.5), `acked.jsonl` (§9.5), `server_info.json` (§15), and `rescued/` (below).
- **Private and read-only on disk.** The cache holds a private board, so only its owner can read it. The syncer writes board files atomically and then sets them to mode 0400 (`atomic_write` creates 0600, so the chmod is explicit). Durable directories are 0500. `.lattice/` itself, `cache/`, and the runtime directories are 0700: owner-only, and writable, so runtime state and unmanaged paths (§6.1) can be created. Modes are set explicitly, whatever the umask. The syncer adds the owner write bit to a durable directory only while it applies a delta, under its locks. A direct write, a rename-based save (`sed -i`, most editors), and a new file under a durable directory all fail. A top-level durable file (`config.json`, `ids.json`, `context.md`, `.gitignore`) is 0400 but sits in the writable `.lattice/`, so a rename-based save of it succeeds; the tamper check below catches that.
- **Runtime directories.** The first sync, and every reset, creates `locks/`, `review_state/`, `tmp-prompts/`, and `.daemon/` if they are missing, so an auto-review or `write_review_state` works on a fresh cache.
- **One sync at a time.** Every sync (a command's catch-up, a write's post-write sync, the follower's syncs) holds `locks/cache_sync.lock` exclusively for its whole cycle: read `state.json`, fetch, verify, apply. Two syncs can therefore never apply out of order or interleave paths.
- **Applying a delta** additionally takes the cache's reader-writer lock exclusively (`fcntl.flock(LOCK_EX)` on `locks/cache_rw.lock`), writes `cache/applying` (`{remote, project, started_at, pid, kind: "delta" | "reset", epoch, target_head_seq}`), writes files, unlinks `removed` paths, restores modes, updates `cache/state.json`, and removes `cache/applying` last. Every read on a hosted checkout (each read command after its catch-up, and each hosted-checkout dashboard request) holds the same lock shared (`LOCK_SH`) from its first directory enumeration through its last file read. A reader therefore never sees a sync half-applied, including an archive relocation between enumeration and a prose read (`storage/operations.py:337-368`, `cli/query_cmds.py:1337-1352`). A `reset` replaces the board files wholesale under the same exclusive lock (files absent from the reset are removed). The first sync writes `cache/applying` before any board file, so a checkout whose first sync died is still a cache (§9.3).
- **Interrupted apply.** A syncer killed mid-apply, or out of disk, leaves `cache/applying` behind with a mixed tree and possibly writable directories. The next sync, under `locks/cache_sync.lock`, finds it, restores every durable mode, and runs a reset sync. If the server is unreachable, every read fails with `CACHE_INCOMPLETE` ("the cache was interrupted mid-update and the server is unreachable; run `lattice sync` when it is back") instead of serving a partial tree.
- **Verification.** Every path in `files` and `removed` must be a relative durable path (§6.1): no `..`, no absolute path, no symlink component, never a runtime path, an unmanaged path, or `cache/`; otherwise the whole delta is rejected. Every file's bytes, inline or fetched, must match its `sha256`; on any mismatch the client discards the delta and re-syncs. For an append delta (§8.8) the client checks, before applying anything, that its local copy is exactly `append_from` bytes long; if not, it fetches the whole file from `href` instead. It applies an append delta by writing the local bytes plus the new bytes with `atomic_write` and checks the result against `sha256`. An `href` must be a relative path on the same server; the client never sends its token or headers to another origin, and never follows a redirect (§9.1).
- **Tamper check.** `fingerprint` hashes each durable file's path, size, and mtime after every sync. Catch-up recomputes it (a stat walk, no reads); a mismatch (a checkout of an old branch that still tracks board files, a manual `chmod` and edit, a rename-based save of a top-level file) triggers a reset sync. **A reset never discards a local edit.** Before applying it, the client compares each durable file with the reset's hashes and moves every file whose hash differs, or that the reset does not contain, to `cache/rescued/<UTC YYYYMMDD-HHMMSS>/<relative path>`. It then prints one line to stderr: `lattice: <n> locally edited board file(s) moved to <dir>; the cache is read-only. Write a plan with: lattice plan write <task> --file <path> (notes: lattice notes write)`. After sync, every board file matches the server byte for byte (AC-47).
- **Clearing a cache.** A 0500 directory makes `rm -rf`, `shutil.rmtree`, and `git clean -xdf` fail. `lattice cache clear` restores write modes and deletes the hosted root's `.lattice/`, except `cache/rescued/`, which it keeps and names on stderr, and except a fresh `cache/state.json` holding only `{remote, project}` (no epoch), so routing survives even on a branch without the committed binding; the next command sees no epoch and runs a reset sync. `lattice cache clear --forget` removes that routing marker too (still keeping `cache/rescued/`): use it when deliberately rebinding the checkout to another project, or when moving a board back to local. On a checkout that is not a hosted root it fails with `NOT_HOSTED` and deletes nothing, so it can never delete a local board. The guide's troubleshooting section says so (§13).

### 9.5 Freshness, reads, and writes

- **Before any read** (every read command, and the read phases of write commands such as rendering), the client calls catch-up, unless a live follower is running: the `stream_live_until` field of `cache/follower.json` is in the future and its `pid` is alive (`os.kill(pid, 0)` succeeds). Catch-up is one sync call with a 2-second connect and 5-second total timeout; with nothing new the server answers from memory with a few hundred bytes. That timeout bounds only the probe: once the server has started answering, a large delta or a reset (including the first sync) continues as a bulk transfer with a timeout of 60 seconds plus 2 seconds per MiB announced, printing progress to stderr every 5 seconds. `lattice sync` and `remote attach` always use the bulk policy. On failure it prints one line to stderr and continues (AC-8): `lattice: cannot reach <alias>; showing cache as of <synced_at>`, or, when the failure was `BOARD_BUSY` or `RATE_LIMITED`, `lattice: <alias> is busy; showing cache as of <synced_at>`. `--json` stdout is unaffected.
- **Offline reads skip the wait.** After a catch-up fails to connect or times out, the client writes `cache/unreachable_until` (now plus 15 seconds). Until then, catch-up makes no network attempt and every read goes straight to the cache, still printing the one-line notice. Any successful request (a write, `lattice sync`, a follower's sync) removes the file.
- **Writes:** `HostedBoard.execute` posts the op, with the retries of §8.6 (`OUTCOME_UNKNOWN` or `SERVER_UNREACHABLE` when they run out; nothing is written locally either way). `params` omits every parameter equal to its declared default. On success it appends `{op_id, project, epoch, seq, at}` to `cache/acked.jsonl` (for `lattice remote verify`, §9.2), syncs so the next read sees the write (AC-6), and returns the `OpResult`. If that sync fails, the write still succeeds (§3.4 item 4). Error envelopes and exit codes are the local ones (AC-5).
- **Actor:** in hosted mode `--actor` is optional; the server defaults it (§8.3). `--name` on a read command resolves the session from the cache read-only and never touches it; only the writer touches sessions (§3.7).
- **Origin:** the client fills `origin.reported` on every op (§4).

### 9.6 Follower, watch, and doctor

- `lattice sync [--follow]`: one catch-up, or a foreground follower that holds the stream and syncs on each entry (at most one sync in flight, coalescing). Freshness means applied syncs, not received bytes:
  - The follower tracks `announced`, the highest seq any entry or heartbeat has named (a heartbeat carries the server's `head_seq`, §8.9).
  - It sets `stream_live_until = now + 2 × heartbeat_seconds` in `cache/follower.json` only when its last sync succeeded and the cache's `head_seq` has reached `announced`. A heartbeat whose `head_seq` the cache already holds extends it without a sync. An entry or heartbeat ahead of the cache triggers a sync, and the extension waits for that sync to apply.
  - Any failed sync or apply clears `stream_live_until` at once, so every read on the machine falls back to its own catch-up.
  - `heartbeat_seconds` comes from `/v1/info` (`stream_heartbeat_seconds`), read when the follower starts.
  - When the stream has delivered nothing (no entry, no heartbeat) for 2 × `heartbeat_seconds`, which includes a stream a proxy refuses or buffers, the follower polls sync every `heartbeat_seconds` until the stream delivers again. It reconnects a failed stream with backoff capped at 60 seconds (AC-45).
  - It exits 0 on SIGTERM and clears `stream_live_until`. A follower killed any other way leaves a dead `pid`, which readers detect (§9.5).
- `lattice dashboard` on a hosted checkout runs an embedded follower for its lifetime. Its writes go through `HostedBoard.execute`, as the browser actor (§8.3).
- `lattice watch` and `lattice wait` on a hosted checkout read the stream (or poll) instead of watching files, and print the same output.
- `lattice doctor` on a cache runs its read-only checks, then compares each board file's hash against a `reset`-style manifest from the server (`sync?since=0` with `manifest=1`, hashes only, served from the server's in-memory manifest, §8.8) and reports local modifications. `--fix` is `LOCAL_ONLY`. To check the server's own copy without racing its transactions, the admin runs `lattice server project doctor <slug>` (§8.2).

---

## 10. Dashboard

- **Local (unchanged look):** `lattice dashboard` still serves the stdlib dashboard. Its read handlers call `dashboard/api.py`; its POST handlers call operations, so they gain the CLI's full rules and return the operations' error codes (a deliberate change: for example, a stale transition now returns the CLI's code instead of a generic 400, and a drag that breaks the plan gate or completion policy is now refused, as in the CLI; G-6 declares it). Security fix, deliberately included: POSTs require `Content-Type: application/json` and an `Origin` matching the served host, closing today's cross-origin POST exposure. On a local board a POST without an actor still defaults to `dashboard:web`; on a bound checkout the dashboard sends the browser actor (§8.3). Dashboard writes carry `origin.reported.source: "browser"` (§4). When a dashboard move is refused by a rule the CLI can override, the message names the CLI escape (`lattice status <task> <status> --force --reason "..."`); the dashboard gains no force control in v2.
- **Escaping.** Once a board is shared, the page renders other people's strings. `esc()` also escapes `'` (as `&#39;`), and no event handler is built by concatenating a string into an inline attribute: handlers read their arguments from `data-*` attributes through listeners the page attaches. Both apply to the local page too.
- **Base path:** the page derives a base path from its own URL; the `api()` and `apiPost()` helpers and every asset reference (today hard-coded `/static/*`, `static/index.html:7-8, 1625-1637`) use it, so the same assets work at `/` locally and at `/p/<slug>/` hosted.
- **Hosted:** the server serves each project's dashboard at `/p/<slug>/` from the same assets, answering `/p/<slug>/api/*` with `dashboard/api.py` on the authoritative board. Read results are memoized per (project, head seq, endpoint, query), so any number of viewers cost one computation per write. POSTs become operations with the session's token; any actor in the request body is ignored and the browser actor (§8.3) is used. `open-notes` and `open-plans` return `LOCAL_ONLY`. When hosted, the page subscribes to `/v1/projects/<slug>/stream` and refetches on entries, falling back to its 5-second poll if the stream fails (AC-24).
- **Hosted response headers.** Every page and API response under `/`, `/login`, and `/p/<slug>/` carries `X-Content-Type-Options: nosniff` and this `Content-Security-Policy`, where `<hashes>` is one `'sha256-…'` source per inline `<script>` block, computed when the server starts:

  ```
  default-src 'self'; script-src 'self' <hashes>; style-src 'self' 'unsafe-inline'; img-src 'self'; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'
  ```

  Under this policy a dashboard `background_image` pointing at another origin does not load on a hosted dashboard.
- **Login:** `GET /login` serves a form; `POST /login` with a valid token creates a session (32 random bytes; stored as a SHA-256 hash in `web_sessions.json` with `token_id`, `created_at`, `expires_at` 30 days later) and sets `lattice_session` (`HttpOnly; SameSite=Strict; Path=/`, plus `Secure` when the request is HTTPS). `POST /login` (a form post, so no JSON content type) requires the same `Origin` check as cookie-authenticated POSTs, so another site cannot log a browser in under its own token. A session dies with its token (AC-13); expired sessions are pruned on every write to `web_sessions.json`. Cookie-authenticated POSTs require JSON content type and an `Origin` equal to the request's host origin or listed in `public_origins`. A session cookie authenticates only `/`, `/p/<slug>/...`, and the stream; it never authenticates `/v1/.../ops`, `sync`, or `files`.
- **Index:** `GET /` lists the projects the session's token may see, linking to each dashboard (AC-16). It uses the dashboard's stylesheet.

## 11. Moving a board

Moving a board needs one tool, a doctor-gated import. The rest is a short procedure in the guide that a person or an agent follows.

`lattice server project import <slug> --from DIR [--code CODE]`, where `DIR` contains `.lattice/`:

1. Refuse if `projects/<slug>` already exists.
2. Run strict doctor read-only on the source; refuse on any error and print the findings (AC-17).
3. Walk the source's `.lattice/` without following symlinks. Refuse, naming the path, if any durable path (§6.1) is a symlink or anything other than a regular file or a directory. Nothing is created on a refusal.
4. Print two lists: every unmanaged path (§6.1), which import does not copy, and every non-canonical file under `plans/`, `notes/`, `archive/plans/`, and `archive/notes/` (anything other than `<task_id>.md` for a task of the board), which import copies because it is durable. Unmanaged paths stay only in the old board, which the move keeps (below).
5. Copy every durable regular file into `projects/<slug>/.lattice/`, `config.json` and `templates/` included, so the project keeps its review workflow and prompt overrides.
6. Run the short-ID repair (§5) with the log floor; like `rebuild --all`, it rewrites the derived files (task snapshots in `tasks/` and `archive/tasks/`, `ids.json`, `events/_lifecycle.jsonl`).
7. Create the journal with a new epoch and head 0, the audit repo, and an initial commit.
8. Print the guide's move steps with the attach command filled in.

The source is never modified. To redo an import, unload the project (§8.2), move `projects/<slug>/` aside, and import again.

**The move (in the guide, §13).** The guide gives these steps in this order, each with the exact commands, so an agent can follow them:

1. Stop every writer of the local board (agents, dashboards, MCP servers).
2. On the server host, run `project import` from a copy of the board, and read both lists it prints. Unmanaged paths do not move; they stay in the old board, which step 3 keeps.
3. In the checkout, move the old board aside: `mv .lattice .lattice.pre-hosted-<UTC YYYYMMDD-HHMMSS>`. Never delete it. Add `/.lattice.pre-hosted-*/` to `.gitignore`. If board files are tracked, run `git rm -r --cached -q .lattice` (staged, not committed).
4. Run `lattice remote attach <alias> <slug>` (§9.2).
5. Commit the binding, `.gitignore`, and the staged removal, and push, so teammates and other branches pick up the move. Check `lattice remote status` for branches that still track board files (§9.2).

**Moving back.** The guide also gives the reverse, again with no new tooling: stop writers; unload the project (or stop the server); in the checkout, run `lattice cache clear --forget`, remove `.lattice-remote.json`, and copy `projects/<slug>/.lattice/` from the server host into the checkout without its `hosted/` directory; remove the `/.lattice/` line from `.gitignore` and `$GIT_COMMON_DIR/info/exclude` if the board should be tracked again; run `lattice doctor`; commit. The board keeps the events v2 wrote, so it needs v2 to read correctly (§15).

## 12. MCP

MCP tools resolve their board with `resolve_board` (honoring `lattice_root`), call operations for writes, and read after catch-up. They therefore work hosted and apply the full rules, a change for local MCP users that G-6 declares. Each tool call's `lattice_root` is its operation's starting directory, so one MCP process writing to several checkouts records each call's own worktree and branch (§4).

## 13. Documentation deliverables

- `docs/hosted/guide.md`, written so an agent can execute it end to end (AC-44):
  - first, **before any server**: if your only problem is several worktrees on one machine (Scenario W), stop tracking the board in git. Linked worktrees already share the primary checkout's `.lattice/` (`storage/fs.py:165-169`); what breaks is each worktree carrying its own tracked copy. The recipe: `git rm -r --cached .lattice`, add `/.lattice/` to `.gitignore` (and to `$GIT_COMMON_DIR/info/exclude` for branches not yet updated), commit, and remove stale board copies from linked worktrees without touching the primary's. It is agent-followable, needs no new command, and the guide says what it costs (the board no longer travels with the repo to other machines, which is what a server is for);
  - when to use hosted and when not (local is the default); install; `server init`; `project create` and `import`; tokens (one per person per machine; seats get single-actor tokens; `token grant` to add projects or actors);
  - each project's review workflow: the `project create` options and `project config` (plan reviews only, code reviews only, both, or none);
  - `serve` under launchd and systemd, with log rotation; reverse proxies (TLS termination, stream buffering off, read timeout above the heartbeat, no redirect of API paths to a login page);
  - client `remote add` and `attach`, including refreshing installed CLAUDE.md blocks and skills; thin clients by environment variables; the follower;
  - moving a board to the server and back (§11), and fixing branches that still track board files;
  - auto-review on hosted boards: it runs on the machine that made the transition, so a thin client must stay alive until its review lands, dashboard moves start no review, and `review-status` reports reviews running on other machines;
  - backup and restore: stop the server (or unload the project) before copying the server root, or take an atomic filesystem snapshot of the whole root; a live `tar` or `rsync` of a running server is not a consistent backup. After restoring, run `project rotate-epoch` for every project before clients connect. Restoring from the audit history is an import (§8.10);
  - upgrade, including that plugin operations must be installed on the server;
  - trust: a token's `machine` label names the seat it was issued to, not where a copied token runs; revoking a token cannot retract board copies already in its caches; completion attestations (§3.4) are the client's claims, recorded with its token, not facts the server checked;
  - unknown write outcomes (`OUTCOME_UNKNOWN`: check `lattice remote op-status` before retrying) and the daily `lattice remote verify`;
  - troubleshooting, including that `rm -rf` of a checkout fails on its read-only cache and `lattice cache clear` is the fix, and every CLI-only error code of §3.1 with its remedy.
- `docs/hosted/api.md`: every endpoint, body, and error code in §8.4, with curl examples. Every example that sends an `op_id` mints a fresh one (`op_$(python -c 'import ulid; print(ulid.ULID())')`), never a literal, and the page says a request without `op_id` is never deduplicated.
- `docs/hosted/deploy/lattice-server.plist.example` and `lattice-server.service.example`, with placeholder paths and hosts, plus `newsyslog.conf.example` (macOS) and `logrotate.example` (Linux) for the server's log.
- README: bump the stated version to 2.0.0; replace "coming soon: Lattice Remote" with a short "Lattice Hosted (optional)" section after the local quick start, pointing at the guide and saying when you would want it (AC-43). Add a short "Upgrading to v2" note listing every change a local user sees: the declared changes of G-6.
- `skills/lattice/SKILL.md` and `templates/claude_md_block.py`: `lattice plan write` and `lattice notes write` as the way to write plans and notes (in the ticket that adds the commands, H-4); a short hosted section pointing at the guide (H-17). `docs/architecture/` gains `operations.md` (including the `lattice.operations` entry-point group) and `hosted.md`.
- `Decisions.md` entries: the operation seam; origin on events; hosted boards leave git (for hosted checkouts only; local keeps its policy); tombstones; the v2 release branch.
- `docs/design-lattice-remote.md` already carries a status line pointing here (added at the architect stage).

---

## 14. Guardrails

Each guardrail has a pass/fail check in `EVALUATION.md` and an enforcement ticket in `BUILDPLAN.md`, including an audit of existing code.

| ID | Guardrail | Enforcement |
|---|---|---|
| G-1 | One writer per board. No client, cache, local CLI, or second server writes a server-owned board, no code path writes a cache except the syncer, and no operation writes outside its own board. | §6 markers checked in every storage write primitive; board confinement in the same primitives and the path-bearing input checks (§3.1, §6.2); write recorder coverage; audit of every writer in `src/` (the 43 commands, sessions, config, artifacts, prose, review state); a default-suite AST test that keeps the boundary for future code: no module outside `lattice.ops` and `lattice.storage` calls a storage write primitive or `mutate_task`, and nothing inside them writes a file except through `storage/fs.py`, apart from an explicit allowlist in the test of modules that write only runtime paths or files outside any board |
| G-2 | No deletion of board data on a hosted board beyond the cases §7 permits (relocation; rollback of an uncommitted operation). | Recorder-based test; `doctor --fix` `LOCAL_ONLY`; refused-`complete` unlink removed; audit of every unlink and rmtree |
| G-3 | No deployment-specific hostnames, tokens, or proxy secrets in the repository. | Hygiene test in the default suite over tracked files, meaningful in CI and in every worktree: always-on built-in patterns for token shapes (`lat_tok_`), private keys, Cloudflare Access header values, `*.ts.net` names (except the placeholder label `example`), `*.cloudflareaccess.com` names, and IPv4 addresses in the tailnet range `100.64.0.0/10`; plus, when committed, `tests/hygiene_denylist.sha256` (a salt line, then one SHA-256 of salt plus a denylisted string per line), checked by hashing every lowercased `[a-z0-9.-]+` token of every tracked text file; plus the private `LATTICE_HYGIENE_DENYLIST` when set |
| G-4 | The base install gains no runtime dependency; `import lattice.cli` imports no server library. | Test comparing `[project].dependencies` to a pinned list; import-graph test |
| G-5 | The server is independent of c11. | Subprocess test: `lattice server serve` runs with a fake `c11` executable first on `PATH` and `C11_*` variables set; the hosted parity corpus runs against it; the fake is never executed and no socket at `C11_SOCKET_PATH` is opened. `lattice.server` and `lattice.ops` import nothing from `lattice.integrations` or `lattice.cli` (import-graph test) |
| G-6 | Local mode is unchanged, except for these declared changes: the `origin` fields (AC-36) and the origin line in `show --events` (AC-38); the new commands and options; the dashboard's JSON error codes and its content-type and `Origin` checks (§10); the rule convergence, by which MCP status changes and local dashboard POSTs now apply the CLI's rules (plan gate, review-cycle limit, completion policy), so a move the CLI refuses is refused there too (§10, §12); resource and session names that are not one safe path component are refused (§3.1). The README's "Upgrading to v2" note lists the same changes (§13). | Golden parity corpus (AC-29), recorded both plain and `--json`, including a sentinel-hook scenario, with the declared changes normalized; unknown top-level event keys ignored by replay; `lattice.ops` imports nothing from `lattice.cli` (so no `output_error` and no `SystemExit` can be reached from an operation); the server wraps `execute` in a `BaseException` handler that returns 500 and logs; a test that imports `lattice.cli.main`, `lattice.boards`, `lattice.storage.fs`, and `lattice.ops` with `fcntl` blocked, so local Lattice keeps importing on Windows |
| G-7 | No token secret at rest in plaintext or in any log. | Tests over `tokens.json`, `web_sessions.json`, and captured logs |
| G-8 | No offline write queue. | Test: writes while unreachable leave the cache and filesystem unchanged |
| G-9 | The default test suite stays hermetic and fast. | Server tests in-process on `127.0.0.1:0`; slow torture tests behind the `torture` marker and timing checks behind the `perf` marker, both outside the default suite; suite time recorded per PR |
| G-10 | The server runs no hooks, spawns no agents, and executes no board-configured commands. | Test: a board with hooks configured, written through the server, runs no hook on the server |
| G-11 | New operations and event types need no server-specific code. | Test: an operation registered only in the test process (a plugin-style module), and one registered through a `lattice.operations` entry point, are executable through an in-process server with no server change |

---

## 15. Compatibility

- **Version.** Lattice v2 ships as version `2.0.0` (the same string under PEP 440). `pyproject.toml` and the README move to it at release; until then the branch keeps its development version.
- **Protocol** is the integer 1. A client refuses a server whose `/v1/info` protocol differs, before any write, and the server refuses a request whose `Lattice-Protocol` header differs (AC-48). Only a change to the wire format itself bumps it.
- **Version skew within protocol 1.** Client and server may run different Lattice versions:
  - A client op the server lacks returns `UNKNOWN_OP` naming both versions (AC-48).
  - The client omits every parameter equal to its declared default, so a newer client that adds an option works against an older server until the option is actually used. A non-default parameter the server's operation lacks returns `UNSUPPORTED_PARAM`, naming the parameter and both versions.
  - The server's Lattice code declares `min_client_version`. A release bumps it when it adds an event type that changes snapshot materialization, or changes the default or meaning of an existing operation parameter. The server refuses ops from an older client with `CLIENT_TOO_OLD` before anything runs (§8.4); every response carries it as `Lattice-Min-Client-Version`, and a client below it prints one line per command on reads: `lattice: this client (<v>) is older than the server's minimum (<v>); upgrade Lattice`.
  - A client older than the server (by `Lattice-Server-Version`) that is still at or above the minimum prints one line per command, `lattice: server runs Lattice <v>, this client <v>; upgrade to read every event type`, and suppresses the per-event warnings for event types the server registers. It learns them from `/v1/info`, cached in `cache/server_info.json` and refreshed whenever `Lattice-Server-Version` differs from `state.json`'s `server_version`.
- **Event schema:** `schema_version` stays 1. `origin`, `task_tombstoned`, `plan_written`, and `notes_written` are additive, and replay by older Lattice does not fail on them: unknown top-level keys are ignored and the new types are unknown types (skipped with a warning), so snapshots still materialize. Snapshots are not always correct under v1, though. v1 skips `task_tombstoned`, so erased tasks reappear, and a v1 client in a bound checkout does not route to the server. Boards v2 has written are therefore not guaranteed to work under v1 (operator ruling). Rolling back is code-only (`EVALUATION.md` §5), and a hosted board returns to local through the guide (§11).
- **Local boards** keep their layout, their tracked-board policy, and their `.gitignore` scaffolding. Nothing in local mode mentions hosting unless a binding exists (AC-43).
- **Python** floor stays 3.12.

## 16. Criteria index

| Section | ACs |
|---|---|
| §3 Operations | AC-1, AC-5, AC-29, AC-46, AC-49 |
| §4 Origin | AC-36, AC-37, AC-38 |
| §5 Short IDs | AC-2, AC-28 |
| §6 Ownership | AC-3 |
| §7 Tombstones | AC-27 |
| §8 Server | AC-4, AC-11 to AC-17, AC-22, AC-23, AC-25, AC-26, AC-30, AC-31, AC-46, AC-48, AC-49 |
| §9 Client | AC-6 to AC-10, AC-18 to AC-21, AC-45, AC-47 |
| §10 Dashboard | AC-24, AC-38 |
| §11 Migration | AC-34, AC-35 |
| §13 Docs | AC-32, AC-43, AC-44 |
| §14 Guardrails | AC-3, AC-27, AC-29, AC-30, AC-33 |
| §15 Compatibility | AC-48 |
| Scenarios | AC-40, AC-41, AC-42 (`EVALUATION.md` §4) |
| Follow-up | AC-39 |
