# Lattice Hosted: Build Plan

The path from today's Lattice to v2 with hosted mode. Consumed by lattice-orchestrator-v2. Read `SPEC.md` for what to build and `EVALUATION.md` for how each piece is proven. Ticket IDs here are H-n; the orchestrator mints the Lattice tickets and records the mapping in its run-state.

---

## 1. Decided architecture

Settled with the operator in the intake interview (`sequence/run-state.md`):

| Decision | Choice | Why |
|---|---|---|
| Write seam | Named operations run by whichever process owns the board (`SPEC.md` §3) | The rules are Python callbacks that must run where the writer is; three disagreeing copies of the rules collapse into one |
| Server stack | Starlette + uvicorn + sse-starlette in an optional `server` extra; client is standard library | Same libraries the `mcp` extra already resolves; hardened HTTP and streaming where it matters; base install unchanged |
| Identity | Token issued to one person for one machine or seat, listing permitted actors (default one); origin on every event, authenticated fields stamped by the server | Unforgeable attribution without a token per agent session; machine, worktree, and user first-class |
| Plans and notes | Written through `lattice plan write` / `notes write` in both modes; hosted cache files read-only | A read-only mirror cannot carry direct file edits to the server |
| Cache | The checkout's gitignored `.lattice/`, shared by every worktree through the existing worktree jump; committed binding names an alias and a project | `cat .lattice/...` still works; no hostname in any repo |
| Freshness | Catch-up before every read unless a live follower is running; optional follower; automatic polling fallback | Correct without a daemon; live when wanted; works through any proxy |
| Server atomicity | Each operation is a transaction: undo log, receipt, one journal commit point | One mechanism covers every operation family, in-process failures, and crashes; retries return the original result |
| Merge target | The `v2` release branch; `main` only through the release gate | Heavy testing before any user sees it |
| Scope | Amendment 7 families (LAT-275/276/277) and the c11 ingester family are out; LAT-280, LAT-269, LAT-278, and LAT-279 (server-side) are in; LAT-209 is superseded | Keep the core change core; the out-of-scope families ride on v2 unchanged |

---

## 2. Brownfield reconcile

Audit of `main` at 5a9917a (2026-09-25). Paths under `src/lattice/`.

| Capability | State | Evidence | Gap closed by |
|---|---|---|---|
| Single authoritative mutation function | DONE (storage), PARTIAL (rules) | `storage/operations.py:637-803`, 72 call sites; rules triplicated and drifting: `cli/task_cmds.py:777-886` vs `mcp/tools.py:546-574` vs `dashboard/server.py:879-903` | H-1 to H-5, H-13, H-21 |
| Rules raise typed errors | MISSING | `output_error` raises `SystemExit` inside the lock (`cli/helpers.py:92-98`, called from callbacks at `cli/task_cmds.py:792-840`, `cli/link_cmds.py:132`) | H-1 to H-5 |
| Precondition check before append | PARTIAL | Reducer `from` checks (`core/tasks.py:242-246, 278-282, 308-326`) run before append (`storage/operations.py:716-734`), but surface as bare `ValueError` (CLI `status` tracebacks; dashboard returns 400); no client expectation | H-1 |
| Multi-event atomic append | MISSING | One `jsonl_append` per event (`storage/operations.py:739-759`) | H-1, H-22 |
| Short-ID floor from the log | PARTIAL | `_reserve_or_reconcile_short_id` (`storage/operations.py:568-634`) skips map collisions only; floor exists only in `rebuild --all` (`cli/integrity_cmds.py:251-265`) | H-6 |
| Doctor short-ID completeness | PARTIAL | Stops at first failure (`cli/integrity_cmds.py:199-248`); counter compared with map, not logs (`:882-905`) | H-6 |
| Tombstones | PARTIAL | Logical removals are events (`core/tasks.py:343, 450, 487, 510, 556`); files unlinked by placement (`storage/operations.py:495-506`), refused `complete` (`cli/task_cmds.py:1833-1836`), unlocked `doctor --fix` truncation (`cli/integrity_cmds.py:76-103`) | H-4, H-7 |
| Writes outside `mutate_task` | PARTIAL | Artifact payload copied non-atomically (`cli/artifact_cmds.py:257`; metadata is atomic at `:286`), no operation-wide lock; plan scaffold/reset `write_text` (`storage/operations.py:828-858`, `cli/task_cmds.py:692-709`); sessions touch unlocked (`storage/sessions.py:220-229`); config unlocked in CLI (`cli/main.py:762, 1255, 1310`) | H-4, H-5, H-8 |
| Actor from identity | MISSING | Caller-supplied everywhere (`cli/helpers.py:206-262`; `mcp/tools.py:136`; dashboard default `"dashboard:web"` at `dashboard/server.py:838…1888`) | H-9 |
| Board ownership / read-only cache | MISSING | Reads take lock files that create and unlink (`storage/operations.py:297-301`); no markers | H-8, H-10 |
| Worktree resolution | DONE | `find_root` jumps from a linked worktree to the primary (`storage/fs.py:167-169, 181-216`) | reused by H-11 |
| HTTP write API | PARTIAL | Dashboard POSTs: single-threaded stdlib `HTTPServer`, loopback-only, no auth, no CSRF defense, subset of rules (`dashboard/server.py:222-260, 799-825, 2221`) | H-9, H-13 |
| Change stream with sequence | MISSING | 5 s client polling (`static/index.html:7288-7293`); file-offset `core/event_stream.py` without archive or global sequence | H-9, H-10 |
| Auth, multi-project HTTP, audit commit | MISSING | none in `src/` | H-9, H-13, H-16 |
| CLI registration | PARTIAL | Explicit import list at `cli/main.py:1625-1644` (a collision hotspot) | H-0 |
| Test suite speed | PARTIAL | 2,264 tests, 95 s serial, sys-time dominated by fsync; no xdist | H-19 |

Pre-existing defects the audit found that this build does **not** fix (filed as LAT-284 to LAT-290): hook `LATTICE_ROOT` points at `.lattice/` instead of the project root (`storage/hooks.py:170, 188`); `event_stream` mislabels lifecycle events and ignores `archive/events/` (`core/event_stream.py:38, 56, 87`); `write_review_state` fixed `.tmp` name race and unfsynced `failures.jsonl` (`core/review.py:158-160, 283-284`); `doctor --fix` truncates without a lock in local mode (`cli/integrity_cmds.py:76-103`); `/api/graph` revision misses same-second edits (`dashboard/server.py:586`); dead code (`storage/short_ids.py:93`, `cli/integrity_cmds.py:1138, 1188`, `storage/operations.py:861`); a foreign tool appended `process_started` events straight into a board log.

---

## 3. Tickets

Every ticket targets the `v2` branch. Each lists its criteria (the ticket is done when they pass per `EVALUATION.md`), dependencies (merged before it starts), files, and shared-file notes. Complexity: L/M/H. **Risk-reviewed** tickets get one independent plan review under the orchestrator's policy.

### H-0 Foundation · M
Create `v2` from `main`; add `v2` to the CI triggers (push and pull_request). Replace the explicit command import list at `cli/main.py:1625-1644` with discovery of every `*_cmds.py` / `*_cmd.py` module in `lattice.cli` (help output unchanged, since Click sorts). Add `tests/test_hygiene.py` (G-3), the `torture` marker, and `addopts = "-m 'not torture'"`.

Build the golden parity corpus **from the pre-refactor code**, covering only commands that exist today:
- `tests/parity/corpus.py`: named scenarios, each a list of in-process `CliRunner` invocations against a fresh `lattice init --project-code PAR` board in `tmp_path`, with auto-review disabled in config. Commands that support `--json` use it; `set-project-code` and `set-subproject-code` have no `--json` and are recorded with their plain output. The dashboard settings POST is recorded through the existing in-process dashboard test server (`tests/test_dashboard/conftest.py`) as a request/response pair. Together they exercise every board-writing command in `SPEC.md` §3.3 except `code-review` and `plan-review` (they spawn agents; their existing tests with `tests/fixtures/fake_agent.py` cover them unchanged), and every rejection code in `SPEC.md` §3.1 that the CLI emits today.
- `tests/parity/record.py` writes `tests/parity/golden/<scenario>.json`: per invocation `{args, exit_code, stdout, stderr}`, then the final board as `{path: content}` over durable paths (SPEC §6.1).
- Normalization, applied identically when recording and comparing: parse JSON and JSONL and re-dump with sorted keys; replace each distinct ULID-shaped token (task, event, artifact, session IDs) with `<ID-n>` in first-seen order, scanning invocations in order and then files in sorted path order; replace RFC 3339 timestamps with `<TS>`; replace the temp root with `<ROOT>`; drop the `origin` key from events and session files.
- `tests/parity/test_local_parity.py` replays and compares. New commands (`plan write`, `notes write`, `erase`) get v2-only goldens recorded by the tickets that add them.

Criteria: AC-29 (baseline), AC-33, G-3. Deps: none. Shared: `pyproject.toml`, `cli/main.py`.

### H-1 Operation framework and first slice · H · Risk-reviewed
`src/lattice/ops/` (`base.py`, discovery, `execute`), `src/lattice/boards.py` (`LocalBoard` only), the `OpError` table and storage-exception mapping, origin context and stamping, batched multi-event append, `CONFLICT` from `from` mismatches, `expect_last_event_id`, the `run_hooks` parameter on `mutate_task` and `write_resource_event`, and moving `check_plan_gate` out of `lattice.cli` (`lattice.ops` imports nothing from `lattice.cli`). Convert `create`, `status` (full rules, including auto-assign, plan-reset append, client-side auto-review and c11 effects), and `comment` to operations; the CLI renders `OpResult` identically. `show --events` origin line.
Criteria: AC-29 (parity stays green), AC-36 (local), AC-38 (CLI), G-5 (import graph), G-6, and `parse_params` tests for missing, unknown, and mistyped fields. Deps: H-0. Shared: `storage/operations.py`, `cli/task_cmds.py`, `core/events.py`.

### H-2 Operations: task lifecycle and fields · M
`update`, `edit-description`, `assign`, `needs-human`, `claim`, `unclaim`, `next --claim` (`board.next_claim`), `archive`, `unarchive`, `event`, `task.record_auto_review`.
Criteria: AC-29, G-6. Deps: H-1. Shared: `cli/task_cmds.py` (after H-1), `cli/flag_cmds.py`, `cli/claim_cmd.py`, `cli/archive_cmds.py`, `cli/query_cmds.py`.

### H-3 Operations: links, criteria, comment edits, reactions · M
`link`, `unlink`, `branch-link`, `branch-unlink`, `file-link`, `file-unlink`, `criterion add/edit/retire`, `comment-edit`, `comment-delete`, `react`, `unreact`.
Criteria: AC-29, G-6. Deps: H-1. Shared: `cli/link_cmds.py`, `cli/criterion_cmds.py`, `cli/file_cmds.py`; `comment-*` and `react` live in `cli/task_cmds.py`, so this ticket admits only when no other `task_cmds.py` ticket is active.

### H-4 Operations: artifacts, completion, plans and notes, reviews · H
`task.attach` (payload in params, atomic payload write), the `plan` group with its legacy-read dispatcher and the new `notes` group (`SPEC.md` §3.9; legacy `lattice plan <task>` plain and `--json` output tested unchanged), `task.complete` (validate before any write; remove the refused-completion unlink), attestations (`reachable_review_commits`, validated against the task's current state), new `lattice plan write` / `lattice notes write` (new module `cli/prose_cmds.py`), `plan_written` / `notes_written` event types, plan scaffold and reset inside operations, `code-review` / `plan-review` direct writes routed through operations.
Criteria: AC-29, AC-27 (no refused-complete unlink), AC-5 (attestation rejection tests), G-6. Deps: H-2 (shares `cli/task_cmds.py`), H-8. Shared: `cli/task_cmds.py`, `cli/artifact_cmds.py`, `cli/review_cmds.py`, `cli/query_cmds.py` (the `plan` command), `core/events.py`, `core/tasks.py`.

### H-5 Operations: resources, sessions, board config · M
`resource.*` with `acquire --wait` as a client-side loop, `session.start/end` writing `origin` into session files, actor resolution and authorization before any session touch (`SPEC.md` §3.7), `board.set_project_code`, `board.set_subproject_code`, `board.set_dashboard_config` (the operation only; H-13 converts the dashboard's settings POST to call it), and the `LOCAL_ONLY` list of maintenance commands (`SPEC.md` §3.5).
Criteria: AC-29, AC-5 (local `acquire --wait` released by another process), G-6. Deps: H-1. Shared: `cli/helpers.py` (`require_actor`), `storage/sessions.py`, `cli/resource_cmds.py`, `cli/main.py`.

### H-6 Short-ID floor and doctor completeness (absorbs LAT-280, LAT-269) · M
`SPEC.md` §5, local and server-ready (`max_observed` hook for H-9).
Criteria: AC-2 (local part), AC-28. Deps: H-1 (shares `storage/operations.py`). Shared: `storage/operations.py`, `cli/integrity_cmds.py`.

### H-7 Tombstones and the no-delete rule (absorbs LAT-278) · M
`lattice erase`, `task_tombstoned`, `core` visibility helper applied to `list`, `next`, stats, and `show`; `doctor` `missing_task_file`; the recorder-based no-delete test.
Criteria: AC-27 (tombstones, visibility, and the doctor check, locally); the hosted no-delete proof (G-2) is H-12's. Deps: H-1, H-8. Shared: `core/tasks.py`, `core/events.py`, `cli/query_cmds.py` (with H-2), `cli/integrity_cmds.py` (with H-4, H-6). The dashboard applies the visibility helper in H-13; until then the local dashboard still shows erased tasks.

### H-8 Board ownership and the write recorder · M
`SPEC.md` §6: the path classes, the markers (`hosted/owner.json` with its flock; `cache/state.json`) checked in every durable-path primitive, `--offline-maintenance` for the local-only commands (with the `hosted/maintenance.json` record), `BOARD_IS_HOSTED` / `BOARD_IS_CACHE`, the write recorder with a callback invoked before every durable mutation with its kind (H-22 uses it to write undo entries) (§8.5, created per executor call), and the owner and syncer flags as `contextvars` values. The PR carries the audit table of every durable writer in `src/` and the primitive it uses.
Criteria: AC-3 (primitive level), G-1 (primitive level). Deps: H-1. Shared: `storage/fs.py`, `storage/operations.py`.

### H-9 Server core · H · Risk-reviewed (authentication)
`src/lattice/server/`: the `server` extra, `lattice server` admin CLI (`init`, `serve`, `project create/list/unlock`, `token create/list/revoke`, all under `admin.lock`), `server.json` (including `public_origins` and the `lock_timeout_seconds` cap), tokens and actor permission with the built-in `agent:lattice-auto-review` allowance, the op endpoint with `BaseException` containment, admission then work locks (§8.5), journal (with `lengths` and `baseline`) and epoch, `/healthz`, `/v1/info`, `/v1/projects`, read endpoints, JSON logging, `max_observed` floors, external `config.json` / `context.md` detection, unknown-type warnings to logging.
Criteria: AC-1 (`task.status` race), AC-3 (running-server part), AC-11 and AC-13 (bearer parts), AC-12, AC-14, AC-15 (admission part), AC-16 (API part), AC-17 (create), AC-25, AC-30, AC-36 (hosted), AC-37, AC-48 (server side), G-4, G-6 (exit containment), G-7, G-10, G-11 (server part). Deps: H-1, H-5 (session actors), H-6 (the `max_observed` floor), H-8. Shared: `pyproject.toml`.

### H-22 Server transactions and recovery · H · Risk-reviewed (cross-system consistency)
`SPEC.md` §8.6 and §8.7: undo logs (epoch-tagged) with `length` and `content` entries written through H-8's recorder callback, durability errors that propagate inside server transactions (`_fsync_directory` stays silent locally), receipts, the journal commit point, transaction recovery before the next admission (the one recovery path for every in-process failure), recoverable epoch rotation (`rotation.json`), the idempotency index checked after admission, `replayed: true`, `OP_ID_REUSED`; startup recovery in the specified order (journal tail, missing journal, transactions, maintenance record and restore fingerprint, foreign changes, discovery quarantine), `lattice server project recover`, lease takeover, `clean_shutdown` at graceful SIGTERM, receipt retention.
Criteria: AC-4 (deterministic boundary tests; the SIGKILL loop is H-15's), AC-15 (unavailable part), AC-23 (automatic rotation), AC-31, AC-46. Deps: H-4, H-5, H-9, H-10, H-11 (AC-46's client retry test).

### H-10 Sync and stream · H · Risk-reviewed (cross-system consistency)
Server `sync` (assembled under the locks, reset inline cap), `files` (hash-pinned `href`, `STALE_VERSION`), and `stream` (broadcast under the locks, subscribe-then-replay, replay cap, credential recheck) endpoints; `project rotate-epoch`, offline (refused while undo logs exist) and through the control-request path, with `reset` broadcast; client cache syncer (`cache/state.json`, 0444 files and 0555 directories, per-task lock keys, path and hash verification, same-origin `href` only, tamper fingerprint, reset handling), the cache reader-writer lock held shared by every hosted read from enumeration through its last file read, the whole-cycle sync lock, `cache/follower.json` (`SPEC.md` §9.4), `lattice sync [--follow]` with polling fallback as a reusable follower (H-13 embeds it in `lattice dashboard`), hosted `watch` / `wait`, cache `doctor` manifest check.
Criteria: AC-7 (follower part), AC-9 (cache and read-helper interleavings; the CLI-level cases are H-12's), AC-22, AC-23 (rotation and reset), AC-45, AC-47, G-11 (sync and stream part). Deps: H-9.

### H-11 Client binding and routing · H
`remotes.json` and environment overrides (including `run_board_hooks`), `remote add/list/attach/status`, `.lattice-remote.json` and `find_root` (hosted `LATTICE_ROOT`, the relative-`gitdir` fix), `HostedBoard.execute` with per-call `op_id`s, the 90-second operation read timeout, retries, and error mapping, hook environment scrubbing, read-only `--name` resolution on reads, catch-up on read, offline behavior, actor defaulting, client-side hooks / c11 effects / auto-review on hosted writes, `LOCAL_ONLY` on hosted checkouts, proxy headers.
Criteria: AC-6, AC-7 (next-command part), AC-8, AC-10, AC-18, AC-20, AC-48 (client side), G-8, G-10 (client side); the environment-only thin-client mechanics with `create`, `status`, and `comment` (the full AC-19 / AC-21 loop is H-12's). Retries reuse the `op_id`; the deduplication proof is H-22's. Deps: H-2 (`task.record_auto_review`), H-5, H-10. Shared: `storage/fs.py` (after H-8), `cli/helpers.py` (after H-5).

**CP1 (walking skeleton) triggers here:** H-0, H-1, H-8, H-9, H-10, H-11 merged.

### H-12 Hosted parity gate · M
Run the golden corpus through an in-process server via a bound checkout; assert outputs, exit codes, and boards match the goldens; assert cache tree equals server tree after every scenario; recorder no-delete assertions. Fix any divergence in the owning operation (repair in place).
Criteria: AC-5 (including hosted `acquire --wait`), AC-9, AC-19, AC-21, G-1 (end-to-end), G-2 (hosted), G-5 (server subprocess run). Deps: H-2, H-3, H-4, H-5, H-11.

**CP2 triggers here:** H-2 to H-5 and H-12 merged.

### H-13 Dashboard: operations, hosted per project · H
Extract `dashboard/api.py`; embed H-10's follower in `lattice dashboard` on hosted checkouts; local POSTs through operations (including the settings POST via `board.set_dashboard_config`) plus the JSON content-type and `Origin` checks; base-path-aware `api()`, `apiPost()`, and asset references; body actors ignored under sessions; session cookies scoped away from `/v1` operations; `public_origins`; the tombstone visibility helper; origin line in the event view; hosted `/p/<slug>/` with `/api/*`, login and `web_sessions.json`, `GET /` index, stream-driven refresh with poll fallback.
Criteria: AC-11 (login), AC-13 (sessions), AC-16, AC-24, AC-38 (dashboard), G-7 (sessions). Deps: H-2, H-3, H-5, H-7, H-10, H-11. Shared: `dashboard/server.py`, `dashboard/static/index.html` (sole owner in this build).

### H-21 MCP converges on operations · M
MCP tools resolve boards with `resolve_board`, write through operations, read after catch-up; delete their private rule copies.
Criteria: MCP status changes enforce the plan gate; MCP tools work on a bound checkout (`tests/test_mcp/test_ops_convergence.py`). Deps: H-2, H-3, H-4, H-5, H-11. Shared: `mcp/tools.py` (sole owner in this build).

### H-14 Moving a board · M · Risk-reviewed (destructive migration)
`lattice server project import [--replace]` and `lattice remote attach --migrate` with the manifest comparison over every durable path, including `templates/` and archived prose (`SPEC.md` §11).
Criteria: AC-17 (import), AC-34, AC-35. Deps: H-6, H-11.

### H-15 Torture suite and scenario rehearsals · M
`tests/torture/`: process races, create storm, crash loop, scenario rehearsals W, B (with a header-checking proxy stub), and T.
Criteria: AC-1 (`next --claim` race), AC-2, AC-4 (SIGKILL loop), AC-40, AC-41, AC-42 (rehearsals). Deps: H-2, H-4, H-6, H-12, H-22.

### H-16 Audit history (absorbs LAT-279, server-side) · M
`SPEC.md` §8.10, plus `lattice server project audit <slug> --push-remote NAME --branch B` writing `.lattice/hosted/audit.json`.
Criteria: AC-26. Deps: H-9, H-22 (the paused-transaction proof).

### H-17 Documentation, skill, and adoption · M
`SPEC.md` §13 in full; `Decisions.md` entries for this build (only this ticket writes `Decisions.md`); the docs-agent test (AC-44).
Criteria: AC-32, AC-43, AC-44. Deps: H-11, H-13, H-14, H-16. Shared: `README.md`, `skills/lattice/SKILL.md`, `templates/claude_md_block.py`, `Decisions.md`, `docs/architecture/` (sole owner).

### H-19 Parallel default suite · M
Add `pytest-xdist`, run the default suite with `-n auto`, fix tests that share state, record the baseline wall time. Target ≤ 30 s on the operator's laptop.
Criteria: G-9. Deps: H-0. Shared: `pyproject.toml`, `tests/conftest.py`. Admit early: it speeds every later gate.

### H-20 Follow-up: filter by machine, user, worktree · L
`list --machine/--user/--worktree` and dashboard filters. The operator asked for this as a near-parallel follow-up.
Criteria: AC-39. Deps: H-1 (CLI part), H-13 (dashboard part).

### Deployment track (private)
H-18 (deploy to the first production host and stage the scenarios) and H-23 (spike: stream and polling through the production proxy) follow the operator's private deployment addendum, which the orchestrator receives out of band. Nothing from it enters this repository.

- **H-23 Spike: proxy behavior** · L · operator-assisted. Deploy a throwaway server behind the production proxy as soon as H-10 merges; prove sync, polling, and (if possible) the stream through it. Outcome recorded: stream works, or off-network followers poll. Deps: H-10.
- **H-18 Production deployment and scenarios** · M · operator-assisted. Install, supervise, token issuance, proxy route, trial board, then CP3 and CP4 staging. Deps: every ticket except H-17, H-18, H-19, H-20, H-23 (H-17 before CP4).

---

## 4. Dependency graph and waves

```
H-0 ──► H-1 ──┬─► H-8 ──► H-9* ─┬─► H-10 ──► H-11* ─┬─► H-12 (needs H-2..H-5)
              │     │           ├─► H-16 (after H-22)├─► H-13 (needs H-2, H-3, H-5, H-7)
              │     │           └─► H-23 (after H-10)├─► H-21 (needs H-2..H-5)
              │     ├─► H-4 (also needs H-2)        ├─► H-22 (needs H-4, H-5, H-10)
              │     └─► H-7                         ├─► H-14 (needs H-6)
              ├─► H-2                               └─► H-15 (needs H-2, H-4, H-6, H-12, H-22)
              ├─► H-3                                    H-17 (needs H-13, H-14, H-16)
              ├─► H-5                                    H-18 (needs all core)
              └─► H-6                                    H-20 (needs H-13)
H-0 ──► H-19

* H-9 also needs H-5 and H-6; H-11 also needs H-2 and H-5.
```

The critical path is H-0 → H-1 → H-8 → H-9 → H-10 → H-11 → CP1. The operation conversions (H-2 to H-6) run beside it, so CP2 follows soon after CP1.

**Serialize these shared files** (one active ticket at a time): `cli/task_cmds.py` (H-1, H-2, H-3, H-4), `storage/operations.py` (H-1, H-6, H-8), `storage/fs.py` (H-8, H-11), `pyproject.toml` (H-0, H-9, H-19), `core/events.py` and `core/tasks.py` (H-1, H-4, H-7), `cli/helpers.py` (H-5, H-11), `cli/query_cmds.py` (H-2, H-4, H-7), `cli/integrity_cmds.py` (H-6, H-7). Sole owners: `dashboard/server.py` and `static/index.html` (H-13), `mcp/tools.py` (H-21), docs, skill, template, README, and `Decisions.md` (H-17).

---

## 5. Checkpoints and cut lines

| Checkpoint | After | Critical assumption it settles |
|---|---|---|
| Parity baseline | H-0 | The corpus captures today's behavior before anything moves |
| Pattern proven | H-1 | The richest command (`status`) becomes an operation with byte-identical output |
| **CP1** walking skeleton (human) | H-11 | Hosted feels like local across worktrees; offline errors are clear |
| **CP2** real work on a local server (human, CLI only) | H-12 | Agents work unmodified apart from `plan write`; the board never touches git |
| Proxy spike | H-23 | Whether followers can stream through the production proxy |
| **CP3** first production host (human, external) | H-18 | Boxes are first-class; the dashboard is live per project |
| **CP4** team (human) | CP3 + H-17 | Five identities share one board; docs let others adopt it |
| Release gate | `EVALUATION.md` §5 | About a week of trial across two or three projects |

**Core cut (required for CP3):** every agent-track ticket except H-17, H-19, H-20. **May slip to a later v2.x with operator approval:** the hosted-dashboard half of H-13 (the local dashboard on a cache still works), H-16 (the audit history), H-20.

---

## 6. Two tracks

- **Agent track (the fleet):** every H-n above except the operator-assisted parts of H-18 and H-23.
- **Operator track, in order:**
  1. Before H-23: the proxy route and credentials for one remote environment.
  2. Before CP3: the production host ready per the private addendum; tokens decided per person-machine and per seat.
  3. Before CP4: five identities and their machines.
  4. At the release gate: the two or three trial projects, the trial call, and named approvals for `v2` → `main`, then `prod`, then PyPI.

## 7. Orchestration notes

- Branches cut from `v2`; PRs target `v2`. The Merge Captain merges `main` into `v2` whenever `main` moves, before the next landing.
- Project gate: `EVALUATION.md` §1. Torture also runs on tickets touching `ops/`, `storage/`, `server/`, or `remote/`.
- The orchestrator's own Lattice commands run on the operator's current install, not on `v2`. Builders validate from their worktrees with the worktree's own virtualenv (`uv pip install -e ".[dev,server]"`, then `.venv/bin/lattice`), never the global command.
- No ticket publishes a package, merges to `main`, or deploys outside H-18.
