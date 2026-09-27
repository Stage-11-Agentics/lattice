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
| `floors.py` | Per-project short-ID floors, computed at load |
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
thread stages under the work lock and commits outside it, debounced, and may
push to a configured remote.

## Client (`src/lattice/remote/`)

Standard library only.

| Module | Role |
|---|---|
| `config.py` | `remotes.json` (0600) and `LATTICE_REMOTE_<ALIAS>_*` overrides; env-only header values |
| `http.py` | One transport policy: no redirects (`PROXY_REJECTED`), a response counts only if it carries `Lattice-Protocol`, plaintext only to loopback unless allowed |
| `binding.py` | `.lattice-remote.json`, `remote attach`, `.gitignore` and `info/exclude` |
| `cache.py` | Sync: fetch, verify (paths, hashes, append deltas), apply under `locks/cache_sync.lock` and the exclusive `cache_rw` lock; modes 0400/0500; the tamper fingerprint and `cache/rescued/`; `cache clear` |
| `client.py`, `session.py` | `HostedBoard.execute`: `op_id` per call, omitted defaults, retries, `OUTCOME_UNKNOWN`, post-write sync, `acked.jsonl`; catch-up before reads |
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
skips the network for 15 s. Readers hold the cache's `cache_rw` lock shared,
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
