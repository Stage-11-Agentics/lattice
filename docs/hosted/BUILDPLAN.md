# Lattice Hosted: Build Plan

The path from today's Lattice to v2 with hosted mode. Consumed by lattice-orchestrator-v2. Read `SPEC.md` for what to build and `EVALUATION.md` for how each piece is proven. Ticket IDs here are H-n; the orchestrator mints the Lattice tickets and records the mapping in its run-state.

---

## 1. Decided architecture

Settled with the operator in the intake interview and the contract read (`sequence/run-state.md`):

| Decision | Choice | Why |
|---|---|---|
| Write seam | Named operations run by whichever process owns the board (`SPEC.md` §3) | The rules are Python callbacks that must run where the writer is; three disagreeing copies of the rules collapse into one |
| Server stack | Starlette + uvicorn in an optional `server` extra, with the stream written directly on Starlette; client is standard library | Same libraries the `mcp` extra already resolves; hardened HTTP and streaming where it matters; base install unchanged |
| Identity | Token issued to one person for one machine or seat, listing permitted actors (by default the person and that person's agents; a seat token lists exactly one); origin on every event, authenticated fields stamped by the server | Unforgeable attribution without a token per agent session; machine, worktree, and user first-class |
| Plans and notes | Written through `lattice plan write` / `notes write` in both modes; hosted cache files read-only | A read-only mirror cannot carry direct file edits to the server |
| Cache | The checkout's gitignored, owner-only `.lattice/`, shared by every worktree through the existing worktree jump; a committed binding names an alias and a project and bootstraps a clone, and the cache's own marker routes it after that | `cat .lattice/...` still works; no hostname in any repo; branch switches cannot drop routing |
| Freshness | Catch-up before every read unless a live follower is running; optional follower; automatic polling fallback | Correct without a daemon; live when wanted; works through any proxy |
| Server atomicity | Each operation is a transaction: undo log, receipt, one journal commit point | One mechanism covers every operation family, in-process failures, and crashes; retries return the original result |
| Review workflows | Per project on hosted boards, as locally: plan reviews, code reviews, both, or none, set by the server admin | Operator ruling at the contract read |
| Moving a board | A doctor-gated import plus agent-followable guide steps; no migrate-and-verify tooling | Operator ruling at the contract read |
| Merge target | The `v2` release branch; `main` only through the release gate | Heavy testing before any user sees it |
| Scope | Amendment 7 families (LAT-275/276/277) and the c11 ingester family are out; LAT-280, LAT-269, LAT-278, and LAT-279 (server-side) are in; LAT-209 is superseded | Keep the core change core; the out-of-scope families ride on v2 unchanged |

---

## 2. Brownfield reconcile

Audit of `main` at 5a9917a (2026-09-25). Paths under `src/lattice/`.

| Capability | State | Evidence | Gap closed by |
|---|---|---|---|
| Single authoritative mutation function | DONE (storage), PARTIAL (rules) | `storage/operations.py:637-803`, 72 call sites; rules triplicated and drifting: `cli/task_cmds.py:777-886` vs `mcp/tools.py:546-574` vs `dashboard/server.py:879-903` | H-1 to H-5, H-13a, H-21 |
| Rules raise typed errors | MISSING | `output_error` raises `SystemExit` inside the lock (`cli/helpers.py:92-98`, called from callbacks at `cli/task_cmds.py:792-840`, `cli/link_cmds.py:132`) | H-1 to H-5 |
| Precondition check before append | PARTIAL | Reducer `from` checks (`core/tasks.py:242-246, 278-282, 308-326`) run before append (`storage/operations.py:716-734`), but surface as bare `ValueError` (CLI `status` tracebacks; dashboard returns 400); no client expectation | H-1 |
| Multi-event atomic append | MISSING | One `jsonl_append` per event (`storage/operations.py:739-759`) | H-1, H-22a |
| Short-ID floor from the log | PARTIAL | `_reserve_or_reconcile_short_id` (`storage/operations.py:568-634`) skips map collisions only; floor exists only in `rebuild --all` (`cli/integrity_cmds.py:251-265`) | H-6 |
| Doctor short-ID completeness | PARTIAL | Stops at first failure (`cli/integrity_cmds.py:199-248`); counter compared with map, not logs (`:882-905`) | H-6 |
| Tombstones | PARTIAL | Logical removals are events (`core/tasks.py:343, 450, 487, 510, 556`); files unlinked by placement (`storage/operations.py:495-506`), refused `complete` (`cli/task_cmds.py:1833-1836`), unlocked `doctor --fix` truncation (`cli/integrity_cmds.py:76-103`) | H-4, H-7 |
| Writes outside `mutate_task` | PARTIAL | Artifact payload copied non-atomically (`cli/artifact_cmds.py:257`; metadata is atomic at `:286`), no operation-wide lock; plan scaffold/reset `write_text` (`storage/operations.py:828-858`, `cli/task_cmds.py:692-709`); sessions touch unlocked (`storage/sessions.py:220-229`); config unlocked in CLI (`cli/main.py:762, 1255, 1310`) | H-4, H-5, H-8 |
| Actor from identity | MISSING | Caller-supplied everywhere (`cli/helpers.py:206-262`; `mcp/tools.py:136`; dashboard default `"dashboard:web"` at `dashboard/server.py:838…1888`) | H-9 |
| Board ownership / read-only cache | MISSING | Reads take lock files that create and unlink (`storage/operations.py:297-301`); no markers | H-8, H-10b |
| Worktree resolution | DONE | `find_root` jumps from a linked worktree to the primary (`storage/fs.py:167-169, 181-216`) | reused by H-11 |
| HTTP write API | PARTIAL | Dashboard POSTs: single-threaded stdlib `HTTPServer`, loopback-only, no auth, no CSRF defense, subset of rules (`dashboard/server.py:222-260, 799-825, 2221`) | H-9, H-13a, H-13b |
| Change stream with sequence | MISSING | 5 s client polling (`static/index.html:7288-7293`); file-offset `core/event_stream.py` without archive or global sequence | H-9, H-10a |
| Auth, multi-project HTTP, audit commit | MISSING | none in `src/` | H-9, H-13b, H-16 |
| CLI registration | PARTIAL | Explicit import list at `cli/main.py:1625-1644` (a collision hotspot) | H-0 |
| Test suite speed | PARTIAL | 2,264 tests, 95 s serial, sys-time dominated by fsync; no xdist | H-19 |

Pre-existing defects the audit found that this build does **not** fix (filed as LAT-284 to LAT-290): hook `LATTICE_ROOT` points at `.lattice/` instead of the project root (`storage/hooks.py:170, 188`); `event_stream` mislabels lifecycle events and ignores `archive/events/` (`core/event_stream.py:38, 56, 87`); `write_review_state` fixed `.tmp` name race and unfsynced `failures.jsonl` (`core/review.py:158-160, 283-284`); `doctor --fix` truncates without a lock in local mode (`cli/integrity_cmds.py:76-103`); `/api/graph` revision misses same-second edits (`dashboard/server.py:586`); dead code (`storage/short_ids.py:93`, `cli/integrity_cmds.py:1138, 1188`, `storage/operations.py:861`); a foreign tool appended `process_started` events straight into a board log.

---

## 3. Tickets

Every ticket targets the `v2` branch. Each lists its criteria (the ticket is done when they pass per `EVALUATION.md`), dependencies (merged before it starts), files, and shared-file notes. Complexity: L/M/H. **Risk-reviewed** tickets get one independent plan review under the orchestrator's policy.

### H-0 Foundation · M
The `v2` branch is cut from `main` (with this contract merged) at launch, before H-0 starts, so H-0's own PR can target it; H-0's PR has no CI run on `v2` yet, so the Merge Captain runs the project gate locally for it. H-0 adds `v2` to the CI triggers (push and pull_request). Add `tests/test_hygiene.py` (G-3, with the always-on built-in patterns and the optional salted-hash denylist of `SPEC.md` §14), the `torture` and `perf` markers, and `addopts = "-m 'not torture and not perf'"`.

Order inside the ticket matters: record the parity baseline first, from the unmodified code, and only then change CLI registration.

Build the golden parity corpus **from the pre-refactor code**, covering only commands that exist today:
- `tests/parity/corpus.py`: named scenarios, each a list of in-process `CliRunner` invocations against a fresh `lattice init --project-code PAR` board in `tmp_path`, with auto-review disabled in config. Each scenario is recorded twice, on two fresh boards: once with plain output and once with `--json` on every command that supports it, because people and CLAUDE.md-driven agents read the plain text. `set-project-code` and `set-subproject-code` have no `--json` and appear only in the plain run. The dashboard settings POST is recorded through the existing in-process dashboard test server (`tests/test_dashboard/conftest.py`) as a request/response pair. Together they exercise every board-writing command in `SPEC.md` §3.3 except `code-review` and `plan-review` (they spawn agents; their existing tests with `tests/fixtures/fake_agent.py` cover them unchanged), and every rejection code in `SPEC.md` §3.1 that the CLI emits today. They include `status --force --reason` invocations and `--name` session-actor invocations.
- A **sentinel-hook scenario**: a board whose `config.json` configures `post_event`, `on.<type>`, and `transitions` hooks that append their argv and a fixed set of environment variables to a sentinel file inside `tmp_path`. The golden records the sentinel file, so the set and order of hook firings is compared like any other output.
- `tests/parity/record.py` writes `tests/parity/golden/<scenario>.json`: per invocation `{args, exit_code, stdout, stderr}`, then the final board as `{path: content}` over durable paths (SPEC §6.1).
- Normalization, applied identically when recording and comparing: parse JSON and JSONL and re-dump with sorted keys; replace each distinct ULID-shaped token (task, event, artifact, session IDs) with `<ID-n>` in first-seen order, scanning invocations in order and then files in sorted path order; replace RFC 3339 timestamps with `<TS>`; replace the temp root with `<ROOT>`; drop the `origin` key from events and session files.
- `tests/parity/test_local_parity.py` replays and compares. New commands (`plan write`, `notes write`, `erase`) get v2-only goldens recorded by the tickets that add them.

Record the **local latency baseline** from the same pre-refactor code: `tests/perf/make_board.py` generates a 1,000-task board with long task logs and 300 archived tasks; `tests/perf/record_baseline.py` times `show`, `list`, `status`, and `create` on it as `lattice` subprocesses (median of 5 runs each) and writes `tests/perf/baseline.json`. It runs on the operator's laptop, the reference machine of G-9. `tests/perf/test_local_latency.py` (marker `perf`) compares later code against it: each command's median within 15% or 20 ms of the baseline, whichever is larger, and `create` within 100 ms (`SPEC.md` §5).

Then replace the explicit command import list at `cli/main.py:1625-1644` with discovery of every `*_cmds.py` / `*_cmd.py` module in `lattice.cli`, imported in sorted module-name order so import order is deterministic (help output unchanged, since Click sorts). `tests/test_cli/test_discovery.py` asserts that the discovered module set equals today's list, frozen in the test, plus the modules v2 tickets add; each ticket that adds a command module adds it to that list, which is kept sorted with one module per line so parallel additions merge cleanly. Discovery stays because it removes the `cli/main.py` merge hotspot for a parallel fleet; the test keeps it from widening silently.

Criteria: AC-29 (baseline), AC-33, G-3; the latency baseline recorded. Deps: none. Shared: `pyproject.toml`, `cli/main.py`.

### H-1 Operation framework and first slice · H · Risk-reviewed
`src/lattice/ops/` (`base.py`, discovery including the `lattice.operations` entry-point group, `execute`), `src/lattice/boards.py` (`LocalBoard` only), the `OpError` table and storage-exception mapping, the path-bearing input checks (`SPEC.md` §3.1), origin context and stamping with `worktree` and `branch` derived per operation from its starting directory (`SPEC.md` §4), batched multi-event append, `CONFLICT` from `from` mismatches, `expect_last_event_id`, the `run_hooks` parameter on `mutate_task` and `write_resource_event`, and moving `check_plan_gate` out of `lattice.cli` (`lattice.ops` imports nothing from `lattice.cli`). Convert `create`, `status` (full rules, including auto-assign, plan-reset append, client-side auto-review and c11 effects), and `comment` to operations; the CLI renders `OpResult` identically. `show --events` origin line. The PR carries the `run_hooks` call-site table: each of the 72 `mutate_task` call sites, whether it passed a config today, and the `run_hooks` value it now passes; the sentinel-hook parity scenario proves the firing set unchanged.
Criteria: AC-29 (parity stays green), AC-36 (local), AC-38 (CLI), G-5 (import graph), G-6, a test that an operation registered through a `lattice.operations` entry point is discovered and runs locally, and `parse_params` tests for missing, unknown, and mistyped fields and for malformed `op_id`s and unsafe resource and session names. Deps: H-0. Shared: `storage/operations.py`, `cli/task_cmds.py`, `core/events.py`.

### H-2 Operations: task lifecycle and fields · M
`update`, `edit-description`, `assign`, `needs-human`, `claim`, `unclaim`, `next --claim` (`board.next_claim`), `archive`, `unarchive`, `event`, `task.record_auto_review`.
Criteria: AC-29, G-6. Deps: H-1. Shared: `cli/task_cmds.py` (after H-1), `cli/flag_cmds.py`, `cli/claim_cmd.py`, `cli/archive_cmds.py`, `cli/query_cmds.py`.

### H-3 Operations: links, criteria, comment edits, reactions · M
`link`, `unlink`, `branch-link`, `branch-unlink`, `file-link`, `file-unlink`, `criterion add/edit/retire`, `comment-edit`, `comment-delete`, `react`, `unreact`.
Criteria: AC-29, G-6. Deps: H-1. Shared: `cli/link_cmds.py`, `cli/criterion_cmds.py`, `cli/file_cmds.py`; `comment-*` and `react` live in `cli/task_cmds.py`, so this ticket admits only when no other `task_cmds.py` ticket is active.

### H-4 Operations: artifacts, completion, plans and notes, reviews · H
`task.attach` (payload in params, atomic payload write, storage name from `artifact_id` and the filename's suffix only, `SPEC.md` §3.8), the `plan` group with its legacy-read dispatcher and the new `notes` group (`SPEC.md` §3.9; legacy `lattice plan <task>` plain and `--json` output tested unchanged), `task.complete` (validate before any write; remove the refused-completion unlink), attestations (`reachable_review_commits`, validated against the task's current state), new `lattice plan write` / `lattice notes write` / `lattice context write` / `lattice board write` (new module `cli/prose_cmds.py`; `board.context_write` and `board.file_write` with its path rules, SPEC §3.9 and §6.1), `plan_written` / `notes_written` event types, plan scaffold and reset inside operations, `code-review` / `plan-review` direct writes routed through operations. Update `skills/lattice/SKILL.md` and `templates/claude_md_block.py` to teach `lattice plan write` and `lattice notes write` instead of writing `.lattice/plans/<task_id>.md` directly (today at `templates/claude_md_block.py:267-272`), so the agents of CP2 and every later checkpoint get the method that works on a cache.
Criteria: AC-29, AC-27 (no refused-complete unlink), AC-5 (attestation rejection tests), G-6. Deps: H-2 (shares `cli/task_cmds.py`), H-8. Shared: `cli/task_cmds.py`, `cli/artifact_cmds.py`, `cli/review_cmds.py`, `cli/query_cmds.py` (the `plan` command), `core/events.py`, `core/tasks.py`, `skills/lattice/SKILL.md`, `templates/claude_md_block.py`.

### H-5 Operations: resources, sessions, board config · M
`resource.*` with `acquire --wait` as a client-side loop, `session.start/end` writing `origin` into session files, actor resolution and authorization before any session touch (`SPEC.md` §3.7), `board.set_project_code`, `board.set_subproject_code`, `board.set_dashboard_config` (the operation only; H-13a converts the dashboard's settings POST to call it), and the `LOCAL_ONLY` list of maintenance commands (`SPEC.md` §3.5).
Criteria: AC-29 (including `session start`, `session end`, `set-project-code`, and `set-subproject-code` with no actor arguments), AC-5 (local `acquire --wait` released by another process), G-6. Deps: H-1. Shared: `cli/helpers.py` (`require_actor`), `storage/sessions.py`, `cli/resource_cmds.py`, `cli/main.py`.

### H-6 Short-ID floor and doctor completeness (absorbs LAT-280, LAT-269) · M
`SPEC.md` §5, local and server-ready (`max_observed` hook for H-9). If the local floor caches anything, it keys each log's contribution on `(st_size, st_mtime_ns)`, never on a directory mtime.
Criteria: AC-2 (local part, including the appended-assignment regression test), AC-28; the perf check of `EVALUATION.md` §1 passes for `create`. Deps: H-1 (shares `storage/operations.py`). Shared: `storage/operations.py`, `cli/integrity_cmds.py`.

### H-7 Tombstones and the no-delete rule (absorbs LAT-278) · M
`lattice erase` and `lattice unerase` (`task_tombstoned`, `task_untombstoned`), the `core` visibility helper applied to `list`, `next`, stats, and `show`; `doctor` `missing_task_file`; the recorder-based no-delete test.
Criteria: AC-27 (tombstones, visibility, and the doctor check, locally); the hosted no-delete proof (G-2) is H-12's. Deps: H-1, H-8. Shared: `core/tasks.py`, `core/events.py`, `cli/query_cmds.py` (with H-2), `cli/integrity_cmds.py` (with H-4, H-6). The dashboard applies the visibility helper in H-13a; until then the local dashboard still shows erased tasks.

### H-8 Board ownership and the write recorder · M
`SPEC.md` §6: the path classes (including the default unmanaged class), the markers (`hosted/owner.json` with its flock; `cache/state.json` and `cache/applying`) checked in every durable-path primitive, board confinement in the same primitives (`BoardPathError`) with the new `ensure_dir` replacing raw `mkdir` of board directories (for example `storage/operations.py:941-942`), `fcntl` imported only inside hosted-only functions, `--offline-maintenance` for the local-only commands (with the `hosted/maintenance.json` record), `BOARD_IS_HOSTED` / `BOARD_IS_CACHE`, the write recorder with a callback invoked before every durable mutation with its kind (H-22a uses it to write undo entries) (§8.5, created per executor call), and the owner and syncer flags as `contextvars` values. The PR carries the audit table of every durable writer in `src/` and the primitive it uses.

Add the `fcntl`-blocked import test (G-6). The AST boundary test of G-1 lands in H-12, once H-2 to H-5 have converted every command, so no ticket has to maintain a list of modules still waiting for conversion.
Criteria: AC-3 (primitive level), G-1 (primitive level, including confinement), G-6 (the `fcntl` import test). Deps: H-1. Shared: `storage/fs.py`, `storage/operations.py`.

### H-9 Server core · H · Risk-reviewed (authentication)
`src/lattice/server/`: the `server` extra; the `lattice server` admin CLI (`init`, `serve`, `project create` with `init`'s review options, `project list/unlock/config`, `token create/list/revoke/grant/ungrant`, all under `admin.lock`); the control-request mechanism (`SPEC.md` §8.2) with its `set-config` action (`rotate-epoch` is H-10a's; `unload`, `load`, `reload`, and `doctor` are H-22's); `server.json` (including `public_origins`, `log_level`, every `limits` key, and the `lock_timeout_seconds` cap); tokens and actor permission with the built-in `agent:lattice-auto-review` allowance, the `ACTOR_NOT_PERMITTED` message, the `token create` warning, and `tokens.json` reload on `(st_mtime_ns, st_size, st_ino)`; the per-token limits, the body limit enforced while streaming, and the `task.event` data cap (`SPEC.md` §8.1); the op endpoint with `BaseException` containment, `Cache-Control: no-store`, `CLIENT_TOO_OLD` against `min_client_version`, and `UNSUPPORTED_PARAM`; the checks on `origin.reported` (`SPEC.md` §4); limits, then admission, then work locks (§8.5); journal (with `lengths` and `baseline`) and epoch; lazy project load with a startup prewarm; `/healthz` with disk and project counts, and the disk floor (`STORAGE_LOW`); `/v1/info` with op params, event types, `min_client_version`, and `stream_heartbeat_seconds`; `/v1/projects`; read endpoints; JSON logging with `log_level` (empty syncs at `debug`); `max_observed` floors; external `config.json` / `context.md` detection; unknown-type warnings to logging. Server writes in this ticket (operations and `set-config`) run under the locks and are journaled; H-22a wraps them all in transactions.
Criteria: AC-1 (`task.status` race), AC-3 (running-server part), AC-11 and AC-13 (bearer parts), AC-12, AC-14, AC-15 (admission and per-token limit parts; the `task.event` data cap is implemented here and unit-tested at the check itself, with its end-to-end case in H-12), AC-16 (API part), AC-17 (create), AC-25, AC-30, AC-31 (health and disk floor), AC-36 (hosted), AC-37, AC-48 (server side), AC-49 (server side), G-1 (hostile inputs over HTTP, apart from the attach payload, which H-12 proves end to end; the server-chosen payload name is unit-tested here), G-4, G-6 (exit containment), G-7, G-10, G-11 (server part). Deps: H-1, H-5 (session actors), H-6 (the `max_observed` floor), H-8. Shared: `pyproject.toml`.

### H-22a Server transactions · H · Risk-reviewed (cross-system consistency)
Proves the transaction protocol before sync, stream, and the client build on it. `SPEC.md` §8.6: undo logs (named by `(token_id, op_id)`, epoch-tagged) with `length` and `content` entries written through H-8's recorder callback; durability errors that propagate inside server transactions (`_fsync_directory` stays silent locally); receipts; the journal commit point; the finish step (index, op-status map, undo deletion, and a publication hook that H-10a connects to the broadcaster; H-10a also adds the sync path's in-memory state); transaction recovery before the next admission (the one recovery path for every in-process failure) and the quarantine; the idempotency index keyed by `(token_id, op_id)` and checked after admission, with `replayed: true`, `OP_ID_REUSED`, and server-minted `op_id`s; the op-status map and `GET .../ops/{op_id}`. Fault injection at every boundary for `task.create`, `task.status`, and `task.archive` (the multi-file case: appends, then a copy-then-unlink placement). The PR records the measured cost: server `create` latency, p50 and p95, with and without the transaction wrapper.
Criteria: AC-4 (H-22a part), AC-46 (server level). Deps: H-2 (`task.archive`), H-9. Shared: `storage/fs.py` (durability errors).

### H-10a Server sync and stream · H · Risk-reviewed (cross-system consistency)
Server `sync` (line hashes and the history check, append deltas from the per-log length history, the in-memory manifest, `since == head` answered before admission, one reset per project at a time, the reset inline cap, the `manifest=1` form), `files` (hash-pinned `href`, `STALE_VERSION`), and `stream` (broadcast under the locks through bounded, non-blocking subscriber queues, the subscriber cap, subscribe-then-replay, the replay reset threshold, heartbeat events carrying `head_seq`, credential recheck) endpoints; the finish-step updates of the sync path's in-memory state (`SPEC.md` §8.6 step 6); `project rotate-epoch`, offline (refused while undo logs exist) and through a control request, with `reset` broadcast. **Interface for H-10b and H-10c:** the wire format of `SPEC.md` §8.8 and §8.9, nothing else, plus a test helper that starts an in-process server on a fixture board.
The stream uses no SSE library (`SPEC.md` §8.9, framing and lifecycle): remove `sse-starlette` from the `server` and `dev` extras, from `server/serve.py`'s import check, and from `tests/test_packaging.py`.
Criteria: AC-15 (stream bounds), AC-22, AC-23 (rotation and reset broadcast), G-11 (sync and stream part). Deps: H-22a.

### H-10b Client cache · H · Risk-reviewed (cross-system consistency)
The client transport (`remote/http.py`: no redirects, credentials attached with `add_unredirected_header`, the `Lattice-Protocol` and envelope checks, `PROXY_REJECTED`; `SPEC.md` §9.1) and the cache syncer (`SPEC.md` §9.4): `cache/state.json` (with `head_hash` and `server_version`), `cache/applying` and interrupted-apply recovery (`CACHE_INCOMPLETE`), modes 0400, 0500, and 0700 set explicitly, runtime directories created at first sync and each reset, path and hash verification, append-delta application with the whole-file fallback, same-origin `href` only, the tamper fingerprint with rescue into `cache/rescued/`, reset handling, per-task lock keys; the cache reader-writer lock held shared by every hosted read from enumeration through its last file read; the whole-cycle sync lock; `lattice cache clear`; the cache `doctor` manifest check. **Interface for H-10c and H-11:** one function, `catch_up(hosted_root) -> SyncOutcome` (`applied`, `unchanged`, `unreachable`, `busy`, or `incomplete`, with `head_seq` and `synced_at`), and the shared read-lock context manager.
Registers the `envelope` marker in `pyproject.toml` and marks its supported-size tests with it (`EVALUATION.md` §1).
Criteria: AC-7 (the supported-size torture test), AC-9 (cache and read-helper interleavings; the CLI-level cases are H-12's), AC-20 (redirects on sync and files), AC-23 (history mismatch after a restore), AC-47. Deps: H-8, H-10a.

### H-10c Follower and stream consumers · M
`cache/follower.json` and the follower as a reusable component (`SPEC.md` §9.6: freshness from applied syncs against the announced head, cleared on any failure, the recorded `pid`, silence detection at 2 × `heartbeat_seconds`, the polling fallback, reconnect backoff, the same no-redirect transport); `lattice sync [--follow]`; hosted `watch` / `wait`. H-13a embeds the follower in `lattice dashboard`.
Criteria: AC-7 (follower part), AC-20 (redirects on the stream), AC-23 (the follower's full resync), AC-45. Deps: H-10b.

### H-11 Client binding and routing · H
`remotes.json` and environment overrides (including `run_board_hooks`, `allow_plaintext`, `retry_seconds`), `INSECURE_URL`, and the first-contact errors (`REMOTE_NOT_CONFIGURED`, `TOKEN_ENV_UNSET`); `remote add/list/attach/status/op-status`, including op-status's `in_flight` state (the server's pending set and the CLI's rendering, `SPEC.md` §8.6); `.lattice-remote.json` and `find_root` (routing by the machine-local marker, the binding as bootstrap, adoption of runtime leftovers, `BINDING_CONFLICT` pointing at the guide, hosted `LATTICE_ROOT`, the relative-`gitdir` fix); `attach` from any worktree (`.gitignore`, `$GIT_COMMON_DIR/info/exclude`, the printed refresh commands); `remote status` listing branches that still track `.lattice/`; `HostedBoard.execute` with per-call `op_id`s, omitted default params, the 90-second operation read timeout, the retry policy, `OUTCOME_UNKNOWN`, success when only the post-write sync fails, and error mapping; hook environment scrubbing and the review agent's environment scrubbing (`core/agent_spawn.py`); read-only `--name` resolution on reads; catch-up on read with the live-follower `pid` check, the offline negative cache (`cache/unreachable_until`), and the "busy" line; offline behavior; actor defaulting; client-side hooks, c11 effects, and auto-review on hosted writes, decided from the synced config; version-skew handling (`CLIENT_TOO_OLD`, the upgrade lines, warning suppression through `cache/server_info.json`); control-character replacement in plain output; `LOCAL_ONLY` on hosted checkouts (with `init`'s own message); `HOSTED_UNSUPPORTED_PLATFORM`; proxy headers.

A **mini rehearsal** in the default suite (`tests/test_remote/test_mini_rehearsal.py`), so CP1 is not the first two-writer race through the real client: one in-process server, a bound repo with two linked worktrees, two scripted writers alternating 50 writes, asserting that every write is visible from the other worktree on its next command and that doctor is clean on the server and on the cache. H-15 grows it into `test_w`.

Criteria: AC-6, AC-7 (next-command part and the killed-follower read), AC-8, AC-10, AC-18, AC-20, AC-38 (control characters in plain output), AC-40 (mini rehearsal), AC-48 (client side), AC-49, G-8, G-10 (client side); the environment-only thin-client mechanics with `create`, `status`, and `comment` (the full AC-19 / AC-21 loop is H-12's). Retries reuse the `op_id`; the kill-and-restart proof is H-22's. Deps: H-2 (`task.record_auto_review`), H-5, H-10b, H-10c (the killed-follower case). Shared: `storage/fs.py` (after H-8 and H-22a), `cli/helpers.py` (after H-5).

**CP1 (walking skeleton) triggers here:** H-0, H-1, H-8, H-9, H-22a, H-10a, H-10b, H-10c, H-11 merged, with their dependencies.

### H-12 Hosted parity gate · M
Run the golden corpus through an in-process server via a bound checkout; assert outputs, exit codes, and boards match the goldens; assert cache tree equals server tree after every scenario; recorder no-delete assertions. Add the G-1 boundary test (`tests/test_ops/test_write_boundary.py`, an AST scan of `src/lattice`, `SPEC.md` §14), whose allowlist names each module that writes only runtime paths or files outside any board, with a one-line reason. Fix any divergence in the owning operation (repair in place). Make the review commands truthful on a hosted checkout: `review-status` read from the board, and the `REVIEW_IN_FLIGHT` refusal with its `--force` override (`SPEC.md` §3.4).
Criteria: AC-5 (including hosted `acquire --wait` and cross-machine review status), AC-9, AC-19, AC-21, G-1 (end-to-end), G-2 (hosted), G-5 (server subprocess run), the auto-review handoff tests (SPEC §3.4), and the hosted `context write` round trip. Also: the `task.event` data-cap case of AC-15 and the hostile attach-payload case of G-1, over HTTP through the operations H-2 and H-4 supply; the `plan write --file` round trip of a file rescued from a tampered cache (AC-47), and a manual `lattice code-review` on a machine with `run_auto_reviews: false` (AC-49). Deps: H-2, H-3, H-4, H-5, H-7 (erase in the corpus), H-11. Shared: `cli/review_cmds.py` (after H-4).

**CP2 triggers here:** H-2 to H-5 and H-12 merged.

### H-22 Server recovery · H · Risk-reviewed (cross-system consistency)
The rest of `SPEC.md` §8.6 and §8.7, on top of H-22a's transactions: startup recovery in the specified order (lease and interrupted rotation, journal tail, missing journal, transactions, maintenance record and restore fingerprint, foreign changes, discovery quarantine, in-memory state); the idempotency index and op-status map rebuilt at load, with orphan receipts removed; receipt retention; recoverable epoch rotation (`rotation.json`); `lattice server project recover`; lease takeover; `clean_shutdown` at graceful SIGTERM; the `unload`, `load`, `reload`, and `doctor` control-request actions and their commands (`SPEC.md` §8.2); `cache/acked.jsonl` and `lattice remote verify` (`SPEC.md` §9.2, §9.5). It extends H-22a's fault injection to every remaining operation family.
Criteria: AC-4 (H-22 part; the SIGKILL loop is H-15's), AC-15 (unavailable part and reload), AC-23 (automatic rotation), AC-31 (process lifecycle), AC-46 (client retries and restarts), and the trial's daily checks (`remote verify`, `project doctor`; `EVALUATION.md` §5). Deps: H-4, H-5, H-10c, H-11 (AC-46's client retry test).

### H-13a Dashboard: local, through operations · H
Extract `dashboard/api.py`; local POSTs through operations (including the settings POST via `board.set_dashboard_config`) plus the JSON content-type and `Origin` checks; base-path-aware `api()`, `apiPost()`, and asset references; `esc()` escaping `'` (moved into a testable static file, `static/escape.js`, with node tests) and every handler moved off inline strings (`SPEC.md` §10); the tombstone visibility helper; origin line in the event view; `origin.reported.source: "browser"` on dashboard writes; on hosted checkouts, H-10c's follower embedded in `lattice dashboard`, and writes through `HostedBoard.execute` as the browser actor (`SPEC.md` §8.3). Sole owner of `dashboard/server.py` and `dashboard/static/index.html` until it lands.
Criteria: AC-24 (local and bound-checkout parts), AC-36 (browser writes), AC-38 (dashboard). Deps: H-2, H-3, H-5, H-7, H-10c, H-11. Shared: `dashboard/server.py`, `dashboard/static/index.html`.

### H-13b Dashboard: hosted per project · H
Hosted `/p/<slug>/` with `/api/*` on the authoritative board and read memoization per (project, head seq); the browser actor; body actors ignored under sessions; session cookies scoped away from `/v1` operations; `public_origins`; login with its `Origin` check, and `web_sessions.json`; the `GET /` index; stream-driven refresh with poll fallback; the CSP and `nosniff` headers; the dashboard part of the load test. Sole owner of `dashboard/server.py` and `dashboard/static/index.html` after H-13a.
Criteria: AC-11 (login), AC-13 (sessions), AC-16, AC-24 (hosted parts), AC-42 (load with dashboards), G-7 (sessions). Deps: H-13a. Shared: `dashboard/server.py`, `dashboard/static/index.html`.

### H-21 MCP converges on operations · M
MCP tools resolve boards with `resolve_board`, write through operations, read after catch-up; delete their private rule copies. Each call's `lattice_root` is its operation's starting directory, so origin names that call's worktree and branch (`SPEC.md` §4).
Criteria: MCP status changes enforce the plan gate; MCP tools work on a bound checkout (`tests/test_mcp/test_ops_convergence.py`); AC-36 (one MCP process, two checkouts). Deps: H-2, H-3, H-4, H-5, H-11. Shared: `mcp/tools.py` (sole owner in this build).

### H-14 Moving a board · M · Risk-reviewed (import safety)
`lattice server project import` (`SPEC.md` §11): refusal of an existing slug, the doctor gate, the symlink and special-file refusal, the unmanaged and non-canonical lists, the regular-file copy (`config.json` and `templates/` included), short-ID repair, new epoch, audit repo, and the printed move steps. Before CP3, a dry run: import copies of three of the operator's real boards (including the busiest and the oldest; the orchestrator supplies their paths out of band) into a throwaway server root, and record the doctor findings and the counts of both lists as a comment on this ticket's Lattice task, never in the repository or the PR, because the boards are private. Nothing is committed to those repositories, and the copies are deleted afterward.
Criteria: AC-17 (import), AC-34, AC-35. Deps: H-6, H-11.

### H-15 Torture suite and scenario rehearsals · M
`tests/torture/`: process races, create storm, crash loop, scenario rehearsals W, B (with a header-checking proxy stub), and T. The W and T rehearsals start from a repo whose board is tracked in git and move it by the guide's steps (`SPEC.md` §11), with a feature branch cut before the move and a second clone that pulls the move. The load test without dashboards (`tests/torture/test_load.py::test_readers_writers`).
Criteria: AC-1 (`next --claim` race), AC-2, AC-4 (SIGKILL loop), AC-40, AC-41, AC-42 (rehearsals and load). Deps: H-2, H-4, H-6, H-12, H-14 (the move), H-22.

### H-16 Audit history (absorbs LAT-279, server-side) · M
`SPEC.md` §8.10 (the allowlist `.gitignore`, `git gc --auto` after each commit), plus `lattice server project audit <slug> --push-remote NAME --branch B` writing `.lattice/hosted/audit.json`.
Criteria: AC-26. Deps: H-9, H-22 (the paused-transaction proof).

### H-17 Documentation, skill, and adoption · M
`SPEC.md` §13 in full: the `Philosophy.md` amendment, the guide, `api.md`, the service templates with log rotation, the README and its "Upgrading to v2" note, the hosted section of the skill and the CLAUDE.md template, and `docs/architecture/`. `Decisions.md` entries for this build (only this ticket writes `Decisions.md`). The docs-agent test (AC-44), whose agent also follows the guide's move-back steps once on its scratch board before the trial (`EVALUATION.md` §5).
Criteria: AC-32, AC-43, AC-44. Deps: H-11, H-13a, H-14, and H-13b and H-16 unless the operator has let them slip (the docs then leave those features out). Shared: `README.md`, `skills/lattice/SKILL.md` and `templates/claude_md_block.py` (after H-4), `Decisions.md`, `docs/architecture/` (sole owner).

### H-19 Parallel default suite · M
Add `pytest-xdist`, run the default suite with `-n auto`, fix tests that share state, record the baseline wall time. Target ≤ 40 s on the operator's laptop (`EVALUATION.md` §1).
Criteria: G-9. Deps: H-0. Shared: `pyproject.toml`, `tests/conftest.py`. Admit early: it speeds every later gate.

### H-20 Follow-up: filter by machine, user, worktree · L
`list --machine/--user/--worktree` and dashboard filters. The operator asked for this as a near-parallel follow-up.
Criteria: AC-39. Deps: H-1 (CLI part), H-13a (dashboard part).

### Deployment track (private)
H-18 (deploy to the first production host and stage the scenarios) and H-23 (spike: stream and polling through the production proxy) follow the operator's private deployment addendum, which the orchestrator receives out of band. Nothing from it enters this repository.

- **H-23 Spike: proxy behavior** · L · operator-assisted. Deploy a throwaway server behind the production proxy as soon as H-10c merges; prove sync, polling, and (if possible) the stream through it, and check that the proxy never redirects an API path. Outcome recorded: stream works, or off-network followers poll. Deps: H-10c.
- **H-18 Production deployment and scenarios** · M · operator-assisted. Install, supervise, token issuance, proxy route, trial board, then CP3 and CP4 staging. Every trial client, box images included, installs Lattice from the `v2` branch, never from PyPI, for the whole trial. Deps: every ticket except H-17, H-18, H-19, H-20, H-23 (H-17 before CP4).

---

## 4. Dependency graph and waves

```
H-0 ──► H-1 ──┬─► H-8 ──► H-9* ──► H-22a* ──► H-10a ──► H-10b ──┬─► H-10c ──► H-23
              │     │                                           └─► H-11* ──┬─► H-12 (needs H-2..H-5, H-7)
              │     ├─► H-4 (also needs H-2)                                ├─► H-13a (needs H-2, H-3, H-5, H-7, H-10c) ──► H-13b
              │     └─► H-7                                                 ├─► H-21 (needs H-2..H-5)
              ├─► H-2                                                       ├─► H-22 (needs H-4, H-5, H-10c) ──► H-16
              ├─► H-3                                                       └─► H-14 (needs H-6)
              ├─► H-5                                                            H-15 (needs H-2, H-4, H-6, H-12, H-14, H-22)
              └─► H-6                                                            H-17 (needs H-13a, H-14; H-13b, H-16 unless slipped)
H-0 ──► H-19                                                                     H-18 (needs all core)
                                                                                 H-20 (needs H-13a)

* H-9 also needs H-5 and H-6; H-22a also needs H-2; H-11 also needs H-2, H-5, and H-10c.
```

The critical path is H-0 → H-1 → H-8 → H-9 → H-22a → H-10a → H-10b → H-10c → H-11 → CP1. The transaction protocol (H-22a) is proven before anything is built on it. The operation conversions (H-2 to H-6) run beside the chain, so CP2 follows soon after CP1.

**Serialize these shared files** (one active ticket at a time): `cli/task_cmds.py` (H-1, H-2, H-3, H-4), `storage/operations.py` (H-1, H-6, H-8), `storage/fs.py` (H-8, H-22a, H-11), `pyproject.toml` (H-0, H-9, H-19), `core/events.py` and `core/tasks.py` (H-1, H-4, H-7), `cli/helpers.py` (H-5, H-11), `cli/query_cmds.py` (H-2, H-4, H-7), `cli/integrity_cmds.py` (H-6, H-7), `cli/review_cmds.py` (H-4, H-12), `skills/lattice/SKILL.md` and `templates/claude_md_block.py` (H-4, H-17). Sole owners: `dashboard/server.py` and `static/index.html` (H-13a, then H-13b), `mcp/tools.py` (H-21), docs, README, and `Decisions.md` (H-17). When a ticket moves CLI logic into an operation, that operation's module joins this list for every ticket that touches the same logic (H-2 and H-7 collided in `ops/board_next_claim.py`, which the list did not name).

---

## 5. Checkpoints and cut lines

| Checkpoint | After | Critical assumption it settles |
|---|---|---|
| Parity baseline | H-0 | The corpus captures today's behavior, plain and `--json`, and today's latency, before anything moves |
| Pattern proven | H-1 | The richest command (`status`) becomes an operation with byte-identical output |
| Transactions proven | H-22a | Every server write is wholly applied or wholly absent under fault injection, at an acceptable fsync cost, before sync, stream, and the client depend on it |
| **CP1** walking skeleton (human) | H-11 | Hosted feels like local across worktrees; offline errors are clear |
| **CP2** real work on a local server (human, CLI only) | H-12 | Agents work unmodified apart from `plan write`; the board never touches git |
| Proxy spike | H-23 | Whether followers can stream through the production proxy |
| Real boards import | H-14's dry run | The operator's real boards import cleanly, and their unmanaged paths are known |
| **CP3** first production host (human, external) | H-18's deployment steps | Boxes are first-class; the dashboard is live per project |
| **CP4** team (human) | CP3 + H-17 | Five identities share one board; docs let others adopt it |
| Release gate | `EVALUATION.md` §5 | About a week of trial across two or three projects |

**Core cut (required for CP3):** every agent-track ticket except H-17, H-19, H-20. **May slip to a later v2.x with operator approval:** H-13b (the hosted dashboard; the local dashboard on a cache, H-13a, still works), H-16 (the audit history), H-20.

---

## 6. Two tracks

- **Agent track (the fleet):** every H-n above except the operator-assisted parts of H-18 and H-23.
- **Operator track, in order:**
  1. Before H-23: the proxy route and credentials for one remote environment.
  2. Before H-14's dry run: the locations of three real boards to copy, given to the orchestrator out of band.
  3. Before CP3: the orchestration skills, which live outside this repository, switch to `lattice board write` for their run-state and review packs on hosted boards (SPEC §3.9).
  4. Before CP3: the production host ready per the private addendum; tokens decided per person-machine and per seat.
  5. Before CP4: five identities and their machines.
  6. At the release gate: the two or three trial projects, the trial call, and named approvals for `v2` → `main`, then `prod`, then PyPI.

## 7. Orchestration notes

- Branches cut from `v2`; PRs target `v2`. The Merge Captain merges `main` into `v2` whenever `main` moves, before the next landing.
- Project gate: `EVALUATION.md` §1. Green CI at the exact head stands in for a local run; the Merge Captain runs the per-PR torture lane locally on tickets touching `ops/`, `storage/`, `server/`, or `remote/`. Envelope tests run only in the background and never block a landing or an admission. The `perf` check runs on the operator's laptop at H-6 and before CP3, CP4, and the release gate.
- The orchestrator's own Lattice commands run on the operator's current install, not on `v2`. Builders validate from their worktrees with the worktree's own virtualenv (`uv pip install -e ".[dev,server]"`, then `.venv/bin/lattice`), never the global command.
- No ticket publishes a package or merges to `main`. Only H-18 deploys, apart from H-23's throwaway server behind the production proxy, which it removes when the spike ends.
