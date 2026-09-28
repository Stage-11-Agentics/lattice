# Hosted mode

## Purpose

An optional server that owns many project boards, one writer per board, and
clients that write through it and read a local read-only cache. User guide:
`docs/hosted/guide.md`. Wire format: `docs/hosted/api.md`. Contract:
`docs/hosted/SPEC.md` §§6, 8, 9. Operations, which both sides run, are in
`operations.md`.

```
 checkout (full client)             box (thin client)            browser
 CLI · MCP · dashboard              CLI, no follower              dashboard
 cache: .lattice/ (read-only)       cache dies with the box       cookie session
     │ ops (POST) ▲ sync/stream         │ ops ▲ sync                 │
     ▼            │                     ▼     │                      ▼
 ┌──────────────────────────── lattice server ─────────────────────────────┐
 │ tokens → user, machine, actor patterns · per-project admission + lock   │
 │ lattice.ops.execute in a transaction · journal (epoch, seq) · receipts  │
 │ sync · SSE stream · hosted dashboard · audit git · owner lease          │
 └─────────────────────────────────────────────────────────────────────────┘
      <server_root>/projects/<slug>/.lattice/   standard boards, plain files
```

## Server (`src/lattice/server/`)

Only `app.py`, `serve.py`, and `testing.py` import Starlette and uvicorn (the
`server` extra); every admin command is standard library. Nothing imports
`lattice.cli` or `lattice.integrations`: the server runs no hooks, spawns no
agents, and never calls c11.

| Module | Role |
|---|---|
| `app.py` | Routes (`/healthz`, `/v1/...`), auth, request envelope, error mapping |
| `serve.py` | `lattice server serve`: uvicorn, one process; SIGTERM is a graceful exit 0 |
| `registry.py`, `project.py` | Projects: lazy load plus prewarm, owner lease, admission (`asyncio.Lock`) then work lock (`threading.Lock`), in-memory state |
| `transactions.py` | Every operation as a transaction: undo log, receipt, one journal line as the commit point, recovery on any failure |
| `journal.py`, `syncstate.py` | The journal (`seq`, epoch, line hashes), log lengths, the manifest; assembling sync deltas |
| `stream.py` | Server-Sent Events: subscribe, replay from the resume point, live entries, heartbeats, bounded queues |
| `tokens.py` | `lat_<token_id>_<secret>`, hashed at rest, `fnmatch` actor patterns, reload on `tokens.json` change |
| `limits.py` | Per-token in-flight, operation, and byte-rate limits, checked before admission |
| `control.py` | Control requests: admin commands that act on a project a running server owns |
| `admin.py` | `lattice server init/project/token`; edits under `admin.lock` |
| `recovery.py` | Startup recovery (§8.7), `project recover`, recoverable epoch rotation |
| `importer.py` | `lattice server project import`: doctor gate, path refusal, copy, short-ID repair, new epoch |
| `audit.py` | The per-project audit history: allowlist `.gitignore`, debounced commits, push, `git gc --auto` |
| `floors.py` | Per-project short-ID floors, computed at load |
| `dashboard.py` | `/p/<slug>/`: the dashboard page, assets, and `dashboard/api.py` reads (memoized per load, head, and query) and writes (operations as the browser actor, `Lattice-Op-Id` for retries) |
| `web.py`, `sessions.py` | Login, logout, the index, `Origin` and content-type checks, CSP; `web_sessions.json` (hashes only, 30 days, dies with the token) |
| `log.py` | JSON-lines log with token scrubbing |

### A write, end to end

1. `POST /v1/projects/<slug>/ops/<op>` with a bearer token. Body, in-flight,
   and rate limits answer at once.
2. The request waits for the project's admission lock on the event loop
   (`BOARD_BUSY` after `lock_timeout_seconds`), then takes a worker thread and
   the work lock. Waiting requests never hold threads.
3. The idempotency index is checked: a known `(token_id, op_id)` with the same
   fingerprint returns the stored result (`replayed: true`); a different
   fingerprint is `CONFLICT` / `OP_ID_REUSED`.
4. The transaction begins; `lattice.ops.execute` runs with `run_hooks=False`
   and the transaction's `before_mutation` as the write recorder callback, so
   an undo entry is fsynced before every guarded change.
5. The receipt, then the journal line, are appended and fsynced. The journal
   line is the commit point.
6. In-memory state (index, op-status map, line hash, lengths, manifest) is
   updated, the undo log deleted, and the entry published to streams, all
   under the locks, so streams see `seq` order.

Any failure rolls an uncommitted operation back from its undo log; a failure
of recovery itself, or a journal fsync of unknown durability, quarantines the
project (`BOARD_UNAVAILABLE`) until its next load.

### Startup and load

Per project, under its locks: take the lease (finishing any interrupted epoch
rotation), drop torn tails, settle undo logs against the journal, rebuild the
idempotency index, rotate the epoch after offline maintenance or a restore,
journal foreign changes as `external`, run strict discovery (quarantine on
failure), then build the short-ID floors, line hashes, and manifest.

### Audit history

With `git` available, each `projects/<slug>/` is a git repository whose
`.gitignore` allowlists only durable board paths. A per-project committer
thread commits outside the work lock, debounced, and may push to a configured
remote. Staging runs in a worker process per project (`sys.executable -c`),
which keeps a stat cache and hashes changed files before the work lock is
taken, so the lock is held only to stage what changed since (LAT-340). The
worker is replaced if it dies and reaped before shutdown. `audit_commit` log
lines carry the cycle's timings; any work-lock hold of 1 s or more logs
`work_lock_slow`.

## Client (`src/lattice/remote/`)

Standard library only.

| Module | Role |
|---|---|
| `config.py` | `remotes.json` (0600) and `LATTICE_REMOTE_<ALIAS>_*` overrides; env-only header values |
| `http.py` | One transport policy: no redirects (`PROXY_REJECTED`), a response counts only if it carries `Lattice-Protocol`, plaintext only to loopback unless allowed |
| `binding.py` | Which checkouts are hosted: the binding, the cache marker, `BINDING_CONFLICT`, `remote attach` (`.gitignore` and `info/exclude`) |
| `cache.py` | Sync: fetch, verify (paths, hashes, append deltas), apply under `locks/cache_sync.lock` and the exclusive `cache_rw` lock; modes 0400/0500; the tamper fingerprint and `cache/rescued/`; `cache clear` |
| `client.py` | Operation requests: local param checks, omitted defaults, `op_id` per call, retries, `OUTCOME_UNKNOWN`, op status |
| `session.py` | The hosted read path of one CLI process: catch-up before reads, the offline and busy notices, version-skew lines |
| `cache_paths.py` | Every cache write goes through directory descriptors opened `O_NOFOLLOW`: a `.lattice`, `cache/`, or runtime directory that is a symlink or a file is `BINDING_CONFLICT` (`UNSAFE_CACHE_PATH`), never followed |
| `acked.py` | `cache/acked.jsonl` (each acknowledged write) and `lattice remote verify` |
| `follower.py`, `stream.py`, `sse.py`, `hosted_watch.py` | `lattice sync --follow`, the stream reader with polling fallback, hosted `watch` / `wait` |

### Routing

`find_root` keeps its order (`LATTICE_ROOT`, the linked-worktree jump to the
primary checkout, then walking up). A directory is a **hosted root** when it
has the machine-local cache marker (`.lattice/cache/state.json` or
`cache/applying`), which routes by itself on any branch, or the committed
binding with no local board beside it. A binding beside a local board, or a
marker naming another project, is `BINDING_CONFLICT`.

### Reads

Before every read the client catches up (one sync call, 2 s connect and 5 s
probe timeouts), unless a live follower is running (`cache/follower.json`
fresh and its PID alive). Offline, it prints one line and reads the cache, and
skips the network for 15 s. A write retries for `retry_seconds` (15 s) with
progress lines on stderr; if the offline window is already open, a write whose
first connection fails gives up at once, so an outage costs one wait, not one
per write. Readers hold the cache's `cache_rw` lock shared,
so they never see a half-applied sync.

## Board ownership (`src/lattice/storage/ownership.py`, `storage/fs.py`)

| Marker | Meaning | Refusal |
|---|---|---|
| `hosted/owner.json` + `flock` on `hosted/owner.lock` | A server owns the board | `BOARD_IS_HOSTED` for any other writer (offline maintenance takes the lock itself) |
| `cache/state.json` or `cache/applying` | A client cache | `BOARD_IS_CACHE` for anything but the syncer |

The checks live in the storage write primitives (`atomic_write`,
`jsonl_append`, placement copy and unlink, `ensure_dir`), with the owner and
syncer flags held in `contextvars`. The same primitives refuse any path that
resolves outside the board (`BoardPathError`). Path classes (durable,
workspace, runtime, temporary, server control, cache control, unmanaged)
decide what syncs, what is recorded, and what may be deleted: SPEC §6.1.

## Guardrail tests

`tests/test_storage/test_ownership.py`, `tests/test_server/`,
`tests/test_remote/`, `tests/test_hygiene.py` (no deployment specifics in the
repository), `tests/test_packaging.py` (the base install gains no dependency),
and the torture suite (`-m torture`).

## Proving the hosted docs

The guide is checked by running it, not by reading it.

- `tests/test_docs_hosted.py` (AC-32, default suite): the docs and service
  templates exist; every `lattice ...` command path the guide, API page,
  README, skills, and CLAUDE.md block name resolves and its `--help` exits 0
  (command names only: options and arguments are not checked); no real host
  in the guide, API page, templates, README, both skills, the CLAUDE.md block,
  or `docs/architecture/` (placeholders, loopback, RFC 5737 addresses, and a
  few public project links only); and the guide's move-back steps 4 and 5,
  run as written, commit the board only when `TRACK_BOARD=yes`. Everything
  else in the guide is proven by the runner below.
- `scripts/run_hosted_guide.py`: runs every `bash` block of the guide in order,
  as one shell session, under a scratch `HOME` with the repository's
  `lattice` first on `PATH` (the one-shell assumption the guide states for
  people; agent runners are told to re-export). A block preceded by `<!-- guide: skip: <reason> -->`
  is reported as skipped with that reason; other languages are shown files.
  `uv run python scripts/run_hosted_guide.py --keep-going` for the guide;
  add `--guide docs/hosted/api.md --setup scripts/hosted_api_setup.sh` for the
  API page. Exit 0 when every runnable block passed.
- `scripts/docs_agent_test.sh [OUT_DIR]` (AC-44): installs Lattice from the
  checkout into a scratch venv, gives a fresh agent only `guide.md` and
  `api.md` in an empty directory, and checks what it did: a server, project,
  token, bound checkout and write; the section 1 worktree recipe on a fixture
  with two linked worktrees; and the move back to local with a clean doctor.
  The agent writes `RESULT.env` (checkout, slug, server root, URL); the script
  then checks every part itself (`/healthz`, the project on disk, a status
  change stamped with the token in the moved-back board, no binding, no
  cache marker or `hosted/`, `lattice doctor`), so a wrong report cannot pass.
  The agent is a parameter: `AGENT_CMD` (default `claude`, given the prompt
  with `-p`), `AGENT_ARGS`, `AGENT_TIMEOUT` (default 2700 s). Run it with an
  agent from a different model family than the one that wrote the guide; the
  transcript and results land in `OUT_DIR`. Needs network for the agent and
  `uv`; it is not part of the default suite.
