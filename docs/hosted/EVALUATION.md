# Lattice Hosted: Evaluation Contract

What "done" means for Lattice v2's hosted mode, and exactly how it is verified. Criteria IDs (AC-n) come from `sequence/USER_STORIES.md`; mechanics come from `SPEC.md`; guardrails (G-n) from `SPEC.md` §14; tickets (H-n) from `BUILDPLAN.md`.

Verifiability tags:

- `autonomous`: a build agent proves it alone, with a named command.
- `operator-assisted`: needs something only the operator can supply, named with its deadline.
- `external-oracle`: needs a real artifact outside the repository to judge against.
- `felt`: a human drives it; no test settles it. Scheduled as a checkpoint in §4.

---

## 1. Harness

| Name | Command | When |
|---|---|---|
| **Project gate** | `uv run ruff check src/ tests/ && uv run ruff format --check src/ tests/ && uv run pytest -q` | Every PR, at its final head |
| Parity | `uv run pytest tests/parity -q` | Inside the gate; named separately for evidence |
| Torture | `uv run pytest -m torture -q` | Every PR touching `ops/`, `storage/`, `server/`, or `remote/` once any torture test exists (an empty selection, pytest exit code 5, passes before then); the complete suite after H-15, and before CP3, CP4, and the release gate |
| Scenario rehearsal | `uv run pytest -m torture tests/torture/test_scenarios.py -q` | Before CP3, CP4, and the release gate (CP1 and CP2 rely on the tests their tickets already own) |
| Docs agent test | Procedure in §3, AC-44 | H-17 and terminal validation |

- The default suite excludes the `torture` marker (`addopts = "-m 'not torture'"`), runs in parallel after H-19 (`-n auto`), and must stay hermetic: servers bind `127.0.0.1:0` in-process, every board lives under `tmp_path`, nothing reads the operator's `~/.config` (tests set `XDG_CONFIG_HOME` and `XDG_DATA_HOME` to temp dirs).
- **Speed budget (G-9):** after H-19, the default suite runs in 30 seconds or less on the operator's laptop. No new default-suite test file may exceed 5 seconds. Each PR records the default-suite wall time in its description.
- CI must run on pull requests to `v2` (H-0 adds `v2` to `.github/workflows/ci.yml` triggers).

---

## 2. Guardrail checks (pass/fail)

| ID | Pass condition | Test | Ticket |
|---|---|---|---|
| G-1 | Every durable write in `src/` (§6.1 of SPEC) goes through a recorded, marker-checked primitive. Durable writes to a cache or a server-owned board from any non-owner path raise `BOARD_IS_CACHE` / `BOARD_IS_HOSTED` and change no durable file; runtime and temporary paths stay writable (a read on a cache succeeds). A second server on a held board fails to start. | `tests/test_storage/test_ownership.py`; `tests/test_server/test_lease.py`; the writer audit table in H-8's PR lists every durable writer with its primitive | H-8 |
| G-2 | Across the hosted parity corpus and the torture suite, the write recorder sees no removal of a durable path on a hosted board except the cases SPEC §7 permits (archive and session relocation; rollback of an uncommitted operation). `doctor --fix` on a cache or a hosted checkout returns `LOCAL_ONLY`. | `tests/test_server/test_no_delete.py`; recorder assertions in `tests/parity/test_hosted_parity.py` | H-7 (local tombstones), H-12 (hosted) |
| G-3 | No tracked file matches the hygiene patterns. | `tests/test_hygiene.py` | H-0 |
| G-4 | `[project].dependencies` equals the pinned list (`click`, `python-ulid`, `filelock`, `typing_extensions`); `python -c "import lattice.cli.main"` in an environment without the extra succeeds, and `sys.modules` holds no `starlette`, `uvicorn`, or `sse_starlette`. | `tests/test_packaging.py` | H-9 |
| G-5 | A `lattice server serve` subprocess with a fake `c11` first on `PATH` and `C11_*` set, driven through the hosted parity corpus, never executes the fake and opens no `C11_SOCKET_PATH` socket. `lattice.server` and `lattice.ops` import nothing from `lattice.integrations` or `lattice.cli`. | `tests/test_server/test_independence.py`; `tests/test_ops/test_import_graph.py` | H-1 (ops), H-12 (server run) |
| G-6 | Local parity is green with only the declared changes (SPEC §14) normalized; events carrying unknown top-level keys replay to the same snapshot; `lattice.ops` imports nothing from `lattice.cli`; an injected `SystemExit` inside an operation on the server returns 500 and the server keeps serving. | `tests/parity/test_local_parity.py`; `tests/test_core/test_unknown_keys.py`; `tests/test_ops/test_import_graph.py`; `tests/test_server/test_exit_containment.py` | H-1 to H-5, H-9 |
| G-7 | `tokens.json` and `web_sessions.json` contain no secret; captured server logs across the server suite contain no token secret or session cookie value. | `tests/test_server/test_secrets.py` | H-9, H-13 |
| G-8 | With the server stopped, a write exits 1 with `SERVER_UNREACHABLE`, and a recursive hash of the checkout (including the cache) is unchanged. | `tests/test_remote/test_offline.py` | H-11 |
| G-9 | Speed budget in §1 holds. | PR descriptions; H-19 records the baseline | H-19 |
| G-10 | A hosted board with `post_event`, `on.<type>`, and `transitions` hooks that touch a sentinel file: writes through the server create no sentinel on the server; a hosted client runs them only when its remote sets `run_board_hooks: true`; the hook environment contains no token, `LATTICE_REMOTE_*`, or proxy header variable. | `tests/test_server/test_no_hooks.py`; `tests/test_remote/test_client_hooks.py` | H-9, H-11 |
| G-11 | An operation module defined only inside a test (registered at runtime) executes through an in-process server with no server change (H-9), and its events appear in sync and the stream (H-10). | `tests/test_server/test_generic_ops.py` | H-9, H-10 |

---

## 3. Acceptance criteria

| AC | Verification | Tag | Test or procedure | Ticket |
|---|---|---|---|---|
| AC-1 | 8 threads race `task.status` to `review` on one `in_progress` task through HTTP. With each sending `expect.last_event_id` of the version it read: exactly one 200, seven 409 `CONFLICT` with `details.snapshot`, one `status_changed` in the log, clean replay. Without `expect`: one mutation and seven idempotent successes (today's same-status behavior, `cli/task_cmds.py:781-784`), still one `status_changed`. | autonomous | `tests/test_server/test_races.py` | H-9 |
| AC-1 | 8 processes with 8 distinct actors race `next --claim` over 8 ready tasks: 8 distinct tasks claimed, none twice. | autonomous | `tests/torture/test_race_processes.py` | H-15 |
| AC-2 | Local: a regressed and a deleted `ids.json` never reissue a logged ID. Server: 8 clients × 50 concurrent creates yield 400 distinct IDs; restart with a regressed `ids.json` issues none of them again. | autonomous | `tests/test_storage/test_short_id_floor.py`; `tests/torture/test_create_storm.py` | H-6, H-15 |
| AC-3 | Primitive level: durable writes into a directory marked as a cache or as server-owned are refused (H-8). Running server: a second server, a local CLI, and offline maintenance while the server runs are all refused (H-9, H-22). | autonomous | `tests/test_storage/test_ownership.py`; `tests/test_server/test_lease.py` | H-8, H-9 |
| AC-4 | Deterministic, on the server, at every boundary: for each of `create`, `status`, `complete` with an artifact, `plan write`, `archive`, `unarchive`, a resource `acquire`, `session start`, and a config operation, inject a failure after each durable write and at each server-control step (undo append, undo fsync, a short receipt write, receipt fsync, a short journal write, journal fsync, a directory fsync, index insertion, undo delete, stream publish), including `archive` and `unarchive` failing right after the source log's unlink. After each: the operation is wholly present or wholly absent. Where SPEC §8.6 recovers in-process, the very next operation commits cleanly, a lost-response retry of it replays, and a connected follower misses no `seq`. Where it quarantines (journal fsync failure, failed recovery), every request to that project returns 503 without mutation until restart, and the restart recovers to a correct state. In both branches: no undo log remains after recovery, and doctor is clean. Also: a crash after each step of epoch rotation (the rotation completes at startup and a pre-rotation receipt still replays); commit, crash before undo deletion, offline `rotate-epoch` (refused while the undo log exists), restart, retry → applied exactly once; a torn journal, receipt, or undo line at startup; a missing journal with and without undo logs (`BOARD_UNAVAILABLE`, then `project recover`); a rejected operation after a session touch (the session is untouched). | autonomous | `tests/test_server/test_transactions.py`; `tests/test_server/test_recovery.py` | H-22 |
| AC-4 | 25 SIGKILL iterations of a mixed-operation writer loop against a server subprocess: every acknowledged `op_id` present in full, doctor clean. | autonomous | `tests/torture/test_crash.py` | H-15 |
| AC-5 | The golden corpus replayed against an in-process server through a bound checkout produces the same normalized stdout, exit codes, and board as the local goldens. | autonomous | `tests/parity/test_hosted_parity.py` | H-12 |
| AC-5 | MCP tools call operations: a status change through MCP enforces the plan gate, and MCP tools work on a bound checkout. | autonomous | `tests/test_mcp/test_ops_convergence.py` | H-21 |
| AC-5 | `resource acquire --wait` succeeds once another client releases the resource during the wait, locally (two processes) and hosted (two clients). | autonomous | `tests/test_cli/test_resource_wait.py`; `tests/test_remote/test_resource_wait.py` | H-5, H-12 |
| AC-5 | A completion whose attestation names another branch, omits a marker SHA, or lists a SHA the board lacks is rejected as stale; the client re-syncs and retries once; a fresh attestation passes. | autonomous | `tests/test_ops/test_attestations.py` | H-4 |
| AC-6 | Client writes then immediately runs `show --json`: the write is visible, with no follower running. A hosted `status` to `review` spawns auto-review on the client (fake agent) and records it. `next --name` on a hosted checkout reads without writing the cache. | autonomous | `tests/test_remote/test_read_your_writes.py` | H-11 |
| AC-7 | Client A writes while client B follows: B's cache reflects it in ≤ 2 s; with B's stream blocked by a test proxy, ≤ 5 s (H-10). With no follower: B's very next command reflects it, including a command issued within 100 ms of B's previous one (H-11). | autonomous | `tests/test_remote/test_freshness.py` | H-10, H-11 |
| AC-8 | See G-8, plus: reads succeed from cache and print exactly one stderr notice; `--json` stdout parses. | autonomous | `tests/test_remote/test_offline.py` | H-11 |
| AC-9 | After the hosted parity corpus, the cache and the server board hold exactly the same durable paths (SPEC §6.1) with the same bytes, and read commands give the same output on either (H-12). Deterministic interleavings at the cache and read-helper level (H-10): a reader paused between its active and archived enumerations, or between resolving a plan path and reading it, holds the shared lock, so a sync applying an unarchive waits and the reader succeeds; and in the reverse schedule a reader arriving during the apply waits and then sees the whole result. With one sync paused mid-fetch, a second sync cannot begin fetching until the first has applied (and the same across an epoch reset); the final cache matches the server. A concurrent archive/unarchive loop through the real CLI with client `show`/`list` reads never yields a read error or a task seen in both or neither placement (H-12). | autonomous | `tests/test_remote/test_concurrent_sync.py`; `tests/parity/test_hosted_parity.py` | H-10, H-12 |
| AC-10 | A real git repo with a binding and two linked worktrees (one created with a relative `gitdir:`), no `LATTICE_ROOT`: a write from worktree 1 is visible from worktree 2; exactly one cache exists (in the primary checkout). `LATTICE_ROOT` naming a hosted root before its first sync works. | autonomous | `tests/test_remote/test_worktrees.py` | H-11 |
| AC-11 | Bearer: no token → 401; bad token → 401; revoked → 401; token without project → 403; nothing appended in any case; `/healthz` answers without a credential (H-9). Login: `GET /login` answers without a credential; `POST /login` with a bad token → 401 and no session (H-13). | autonomous | `tests/test_server/test_auth.py` | H-9, H-13 |
| AC-12 | Strict token (one literal actor) with no actor sent → that actor; with another actor → 403; with `agent:lattice-auto-review` → accepted (built-in allowance). Wildcard token with `agent:x` → accepted; with `human:y` → 403. `--name` session actors are checked as `agent:<base_name>` before any session write. | autonomous | `tests/test_server/test_actors.py` | H-9 |
| AC-13 | Revoke via admin CLI while the server runs: the next bearer request gets 401 (H-9); the next session-cookie request gets 401 and an open stream on that session closes (H-13). | autonomous | `tests/test_server/test_revocation.py` | H-9, H-13 |
| AC-14 | See G-7, plus `token create` prints the secret once and `token list` never does. | autonomous | `tests/test_server/test_secrets.py` | H-9 |
| AC-15 | 50 requests to project A queue behind an injected 3-second operation; a project-B write still completes in under 1 s (waiting requests hold no worker threads). Project A marked unavailable (corrupt log): project B still serves reads and writes. | autonomous | `tests/test_server/test_isolation.py` | H-9, H-22 |
| AC-16 | `/v1/projects` and `GET /` list exactly the caller's projects; `/p/<slug>/` serves the dashboard. | autonomous | `tests/test_server/test_projects.py`; `tests/test_server/test_dashboard_hosted.py` | H-9, H-13 |
| AC-17 | `project create` yields a board that passes doctor. `project import` of a fixture board with a known corruption exits non-zero, prints the finding, and creates nothing. | autonomous | `tests/test_server/test_admin.py`; `tests/test_server/test_import.py` | H-9, H-14 |
| AC-18 | `remote attach` writes a binding with exactly the keys `remote` and `project`. | autonomous | `tests/test_remote/test_attach.py` | H-11 |
| AC-19 | A client with a temp `HOME`, no config file, environment-only remote settings, and no follower completes claim → plan write → status → comment → attach → complete. | autonomous | `tests/test_remote/test_thin_client.py` | H-12 (H-11 tests the environment-only mechanics with create, status, and comment) |
| AC-19, AC-21 | The same loop from a real disposable remote environment through the operator's real authenticating proxy. | external-oracle | Checkpoint CP3 (§4) | H-18 |
| AC-20 | A test reverse proxy that rejects requests missing two named headers: the client passes when the headers' env vars are set, fails with `UNAUTHENTICATED`-style proxy error when not; no header value appears in any file the client wrote. | autonomous | `tests/test_remote/test_proxy_headers.py` | H-11 |
| AC-21 | As AC-19, starting from `git clone` of a fixture repo that holds the binding. | autonomous | `tests/test_remote/test_thin_client.py` | H-12 |
| AC-22 | A follower disconnects mid-burst and resumes with `Last-Event-ID`: received seqs are exactly 1..N with no gap or duplicate, including entries committed while it was subscribing; 20 concurrent writers produce strictly increasing delivered seqs. | autonomous | `tests/test_server/test_stream.py` | H-10 |
| AC-23 | `lattice server project rotate-epoch` against a running server (control request) while a follower is connected and an operation is in flight: the operation completes, the follower receives `reset`, performs a full sync, and ends byte-identical. A restart after offline maintenance (the `maintenance.json` record) rotates the epoch on its own. | autonomous | `tests/test_server/test_stream.py`; `tests/test_server/test_recovery.py` | H-10 (rotation and reset), H-22 (automatic rotation) |
| AC-24 | API level: hosted dashboard GETs match the local dashboard's JSON for the same board; a dashboard POST that violates the plan gate is rejected with `PLAN_REQUIRED`; a POST with a foreign `Origin` is rejected unless listed in `public_origins`; a session cookie is refused on `/v1/.../ops`. | autonomous | `tests/test_server/test_dashboard_hosted.py` | H-13 |
| AC-24 | Live board updates in a browser while an agent writes. | felt | CP3 | H-13 |
| AC-25 | `tar` a project directory, extract under a new root, `serve`: doctor clean, and every durable file's hash equals the original's. | autonomous | `tests/test_server/test_backup_restore.py` | H-9 |
| AC-26 | 20 writes produce at least one audit commit whose tree equals the board; an audit staging that fires while a transaction is paused mid-way waits and never commits a partial transaction; push to a local bare repo succeeds; with the remote made unwritable, writes still succeed and a warning is logged. | autonomous | `tests/test_server/test_audit.py` | H-16 |
| AC-27 | `erase` appends `task_tombstoned`, removes nothing, hides the task from `list`/`next`, shows it with `--include-tombstoned`, rejects further writes with `TASK_ERASED`; doctor reports a manually removed task file. Plus G-2. | autonomous | `tests/test_ops/test_erase.py`; `tests/test_cli/test_doctor_missing_file.py` | H-7 |
| AC-28 | A fixture with two unresolvable IDs, two duplicates, and a low counter: doctor reports all five findings. | autonomous | `tests/test_cli/test_doctor_short_ids.py` | H-6 |
| AC-29 | Local parity green; full existing suite green. | autonomous | `tests/parity/test_local_parity.py`; project gate | H-0 to H-5 |
| AC-30 | See G-4. | autonomous | `tests/test_packaging.py` | H-9 |
| AC-31 | `serve` as a subprocess: health returns 200 unauthenticated; logs are JSON lines; SIGTERM during a slow op completes the op, then exits 0. | autonomous | `tests/torture/test_process_lifecycle.py` | H-22 |
| AC-32 | Docs present, every command in them exists (`--help` exits 0), no non-placeholder hostnames. | autonomous | `tests/test_docs_hosted.py` | H-17 |
| AC-32 | Operator reads the guide and templates. | felt | CP4 | H-17 |
| AC-33 | See G-3. | autonomous | `tests/test_hygiene.py` | H-0 |
| AC-34, AC-35 | Migrate a fixture repo whose board is git-tracked and has a review-template override: two commands; the old board sits in `.lattice.pre-hosted-*`; `git status` shows the staged removal and the binding; every durable file hash from the source (including `templates/`) exists on the server and the override is still loaded; doctor clean. A comment, and separately an archived-plan edit, made to the source between import and attach make attach refuse and list the difference. `import --replace` succeeds while the server is stopped and the journal head is 0, and refuses without changing either project when a running server owns the project or after a hosted write. | autonomous | `tests/test_remote/test_migrate.py` | H-14 |
| AC-36 | Every event written by the local and hosted parity corpora has `origin.op`, `origin.op_id`, and `origin.reported` with host, os_user, client_version, and (in git fixtures) worktree and branch. | autonomous | `tests/test_ops/test_origin.py` | H-1, H-9 |
| AC-37 | A client sending a forged `origin.authenticated` gets it replaced by the token's values. | autonomous | `tests/test_server/test_origin_auth.py` | H-9 |
| AC-38 | `show --events` prints `actor · user@machine · worktree (branch)`. | autonomous | `tests/test_cli/test_show_origin.py` | H-1 |
| AC-38 | Dashboard event view shows the same. | felt | CP3 | H-13 |
| AC-39 | `list --machine/--user/--worktree` filter; dashboard filters. | autonomous | follow-up ticket's tests | H-20 |
| AC-40 | Scenario W rehearsal: 5 worktrees, 5 scripted writers, 200 mixed writes; every write visible from every worktree within 2 s (followers) and no `.lattice` path in `git status` of any worktree. | autonomous | `tests/torture/test_scenarios.py::test_w` | H-15 |
| AC-40 | Operator runs real agents in five worktrees against a local server, then against the production host. | felt | CP2, CP3 | H-18 |
| AC-41 | Rehearsal: 3 local scripted clients and 3 clients behind a header-checking proxy, no follower on the proxied ones; integrity and freshness as W. | autonomous | `tests/torture/test_scenarios.py::test_b` | H-15 |
| AC-41 | Real: 3 agents on the operator's laptop, 3 in a real remote environment through the real proxy, one board. | external-oracle, operator-assisted | CP3 | H-18 |
| AC-42 | Rehearsal: 5 tokens (5 users, 3 machines' worth of reported hosts), 10 scripted tickets through the full lifecycle; every event's origin names the right user and machine. | autonomous | `tests/torture/test_scenarios.py::test_t` | H-15 |
| AC-42 | Real: five identities on their own machines (people the operator invites, or the operator driving five tokens across machines), two tickets each, own worktrees. | operator-assisted | CP4 | H-18 |
| AC-43 | Local parity implies no local output mentions hosting; the operator reads the README and guide for positioning. | autonomous + felt | parity; CP4 | H-17 |
| AC-44 | A fresh agent (a different model family from the doc's author, no repository context beyond `docs/hosted/guide.md` and `docs/hosted/api.md`, working in an empty temp directory with Lattice installed from the branch) sets up a server, a project, a token, binds a checkout, and completes a write. Transcript attached to the ticket. | autonomous | Procedure above | H-17 |
| AC-45 | Two test proxies: one refuses the stream, one accepts it and buffers every byte. In both, the follower polls and AC-7's 5 s bound holds, and `stream_live_until` never advances. | autonomous | `tests/test_remote/test_freshness.py` | H-10 |
| AC-46 | For `comment`, `session start`, a config operation, a resource `acquire`, and a no-op `status`: drop the response, perform an intervening write by another client, then retry with the same `op_id` → the stored result is returned verbatim with `replayed: true`, the operation applied once, and the CLI renders the same output and runs the same client effects. Also: commit, lost response, epoch rotation, restart, retry → replayed; an orphan receipt of an uncommitted operation → removed at startup and never replayed; a retry queued behind its own first attempt → applied once; a `status` that records auto-review uses two `op_id`s and both succeed; an `op_id` reused with different params → exactly the SPEC §3.1 `OP_ID_REUSED` envelope. | autonomous | `tests/test_remote/test_idempotency.py`; `tests/test_server/test_transactions.py` | H-22 |
| AC-47 | See AC-9; plus: a byte modified locally (after `chmod`) is detected by the fingerprint and reset at the next catch-up; a direct write, a rename-based save, and a new file in the cache all fail; a delta with a `..` path, an absolute path, a runtime path, a hash mismatch, or a cross-origin `href` is rejected whole. | autonomous | `tests/test_remote/test_cache_integrity.py` | H-10 |
| AC-48 | A server without a test-only op returns `UNKNOWN_OP` naming both versions; a `Lattice-Protocol: 2` request is rejected with `PROTOCOL_MISMATCH` before any write; a client facing protocol 2 in `/v1/info` refuses to write. | autonomous | `tests/test_server/test_versions.py` | H-9, H-11 |

---

## 4. Human-use checkpoints

The reference for every `felt` check is the local Lattice experience: the hosted one must feel the same, only shared.

| Checkpoint | Trigger | What the operator drives | Pass |
|---|---|---|---|
| **CP1: walking skeleton** | H-0, H-1, H-8, H-9, H-10, H-11 merged (operations so far: create, status, comment) | `lattice server serve` on the laptop; bind a scratch repo; open two worktrees; create, move, and comment from each; stop the server and try both a read and a write. | Changes show up in the other worktree without thinking about it; offline read and write errors read clearly. About 15 minutes. |
| **CP2: real work on a local server** | H-2 to H-5, H-12 merged | Run two or three real agents in separate worktrees on a scratch project bound to a laptop-local server, one real small ticket each, through the CLI (dashboards come at CP3). | Agents need no special instructions beyond `lattice plan write`; nothing about the board appears in git; the operator never has to reconcile anything. |
| **CP3: first production host** | Every ticket except H-17, H-18, H-19, H-20 merged, and H-18's deployment steps done (H-18 itself closes after CP4) | Scenario B: three laptop agents and three agents in a real remote environment through the real proxy on one trial board; the hosted dashboard for two projects in a browser; Scenario W on the production host. | AC-24, AC-38, AC-40, AC-41 as felt; the box agents finish a ticket loop (AC-19, AC-21 external-oracle). |
| **CP4: team** | CP3 passed and H-17 merged | Scenario T with five identities, and a read of the README and guide. | AC-42, AC-32, AC-43 as felt. |

**What the operator must supply, and when:**

- **Before CP3:** a production host with the Lattice server installed per the private deployment addendum; the reverse-proxy route and credentials for remote environments; one remote environment able to run agents; tokens per person-machine and per seat. The deployment ticket (H-18) prepares everything an agent can; the proxy route and remote-environment credentials are the operator's.
- **Before CP4:** five identities (invited people or operator-driven tokens) and their machines.
- **For the trial (§5):** the choice of two or three projects.

---

## 5. Release gate

`v2` merges to `main` only when all of these hold, and only with the operator's named approval:

1. Project gate green at the `v2` head, and torture green at the same head.
2. Parity (local and hosted) green.
3. Terminal validation report green (lattice-orchestrator-v2 Phase 2), or residuals named and accepted by the operator.
4. CP1 to CP4 passed.
5. **Trial, about one week:**
   - *Local:* the operator's own Lattice install (the editable checkout every agent on the laptop runs) switched to `v2`, across all local boards. One command rolls it back (`git switch` to the prior commit).
   - *Hosted:* two or three projects of the operator's choosing on the production host.
   - Throughout: `lattice doctor` clean daily on every trial board (hosted boards via `lattice server project list` plus doctor on the server host), no parity regression, no duplicate short ID, no lost write. The operator makes the call when it is working well.

`main` then goes to `prod` and PyPI as separate steps, each with the operator's named approval. Merging is not releasing: no ticket in this build publishes a package.
