# Lattice Hosted: HTTP API (protocol 1)

The Lattice client uses this API for everything it does on a hosted board. You need it only for scripts and agents that cannot install Lattice. Setting up a server and a token is in the [guide](guide.md).

The examples use two variables: the server's URL and a token (`lattice server token create` prints it once).

```bash
export LATTICE_URL=http://127.0.0.1:8740
export LATTICE_TOKEN="$(cat "$HOME/lattice-trial/token")"
```

## Conventions

- **Envelope.** Every JSON body is the CLI's `--json` envelope: `{"ok": true, "data": ...}` or `{"ok": false, "error": {"code": "...", "message": "...", "details": {...}}}`. `details` is present only when the error has some.
- **Headers on every response:** `Lattice-Server-Version`, `Lattice-Min-Client-Version`, and `Lattice-Protocol: 1`. Every `/v1` and `/p/<slug>/api/*` response also carries `Cache-Control: no-store`.
- **Request headers:**
  - `Authorization: Bearer <token>` on every authenticated route.
  - `Content-Type: application/json` on every POST.
  - `Lattice-Protocol: 1` (optional). If sent and different, the request fails with `PROTOCOL_MISMATCH` before anything runs.
  - `Lattice-Client-Version: <version>` (optional; the Lattice client always sends it). If it is below the server's minimum, an operation fails with `CLIENT_TOO_OLD` before anything runs.
- **Task IDs** may be a ULID (`task_01...`) or a short ID (`DEMO-1`) wherever a task is named.
- **Redirects.** The server never redirects an API path. If you see a 3xx, a proxy is in the way (guide section 10).

## Routes

| Method and path | Auth | Purpose |
|---|---|---|
| `GET /healthz` | none | Liveness, free disk, project counts |
| `GET /v1/info` | token | Server version and protocol, your identity, your projects, registered operations and event types, audit state |
| `GET /v1/projects` | token | Projects your token can see |
| `POST /v1/projects/{slug}/ops/{op}` | token | Run an operation (a write) |
| `GET /v1/projects/{slug}/ops/{op_id}` | token | The outcome of one of your own operations |
| `GET /v1/projects/{slug}/sync?since=N&epoch=E&hash=H` | token | What changed since your last sync |
| `GET /v1/projects/{slug}/files/{path}` | token | One board file |
| `GET /v1/projects/{slug}/stream` | token or session | Server-Sent Events: every committed change, live |
| `GET /v1/projects/{slug}/tasks/{id}` | token | One task: snapshot, events, plan |
| `GET /v1/projects/{slug}/tasks?status=&assigned=&include_archived=` | token | Compact snapshots of the project's tasks |
| `GET /` | session | Dashboard index of your projects |
| `GET /login`, `POST /login` | none (`POST` authenticates with the token it submits) | Dashboard login |
| `POST /logout` | session | End the dashboard session |
| `GET /p/{slug}/`, `/p/{slug}/static/*`, `/p/{slug}/api/*` | session or token | The project's dashboard |

A dashboard session cookie authenticates only `/`, `/p/<slug>/...`, and the stream. It never authenticates operations, sync, or files.

## GET /healthz

No token. Touches no board.

```bash
curl -s "$LATTICE_URL/healthz"
```

```json
{"ok": true, "version": "2.0.0", "protocol": 1, "disk_free_bytes": 52613349376,
 "projects": {"loaded": 3, "loading": 0, "unloaded": 0, "unavailable": 0}}
```

Status 503 with `"ok": false` when free disk is below `limits.min_free_disk_bytes`. Project counts only; never slugs.

## GET /v1/info

```bash
curl -s -H "Authorization: Bearer $LATTICE_TOKEN" "$LATTICE_URL/v1/info"
```

`data` holds `version`, `protocol`, `min_client_version`, `stream_heartbeat_seconds`, `identity` (`token_id`, `user`, `machine`, `actors`, `default_actor`, `browser_actor`), `projects` (the slugs you can see), `ops` (every registered operation with its parameter names), `event_types`, and `audit` (whether the audit history is configured and active).

`ops` is how you discover an operation's parameters. They are the CLI command's arguments and options in snake_case (`lattice status TASK NEW_STATUS --reason R` takes `task`, `new_status`, `reason`), without `--json`, `--quiet`, and the actor options. A `--file PATH` option takes the file's **content**, not a path.

## GET /v1/projects

```bash
curl -s -H "Authorization: Bearer $LATTICE_TOKEN" "$LATTICE_URL/v1/projects"
```

```json
{"ok": true, "data": {"projects": [{"slug": "demo", "project_code": "DEMO", "head_seq": 7, "state": "loaded"}]}}
```

## POST /v1/projects/{slug}/ops/{op}

Runs one operation under the project's lock, as a transaction: it is wholly applied or not at all.

Request body (every key but `params` is optional):

```json
{
  "op_id": "op_01J9Z...",
  "params": {"task": "DEMO-1", "new_status": "in_progress"},
  "actor": "agent:ci-bot",
  "actor_name": null,
  "origin": {"reported": {"host": "ci-7", "os_user": "runner", "worktree": "/src/app", "branch": "main", "client_version": "2.0.0"}},
  "attestations": {},
  "expect": {"last_event_id": "ev_01..."}
}
```

- `op_id`: `op_` followed by a ULID, **fresh for every new request**. Retrying the *same* request with the same `op_id` is safe: the server applies it at most once and returns the original result with `replayed: true`. The same `op_id` with different arguments fails with `CONFLICT` (`details.reason: "OP_ID_REUSED"`). **A request without `op_id` is never deduplicated**: the server mints an ID, and a repeated request applies again.
- `params`: the operation's parameters. Omit any parameter to take its default.
- `actor`: who is acting. Must match one of the token's actor patterns (`ACTOR_NOT_PERMITTED` otherwise). Omitted, the server uses the token's default actor: its one pattern without a wildcard, if it has exactly one; otherwise the request fails with `MISSING_ACTOR`.
- `actor_name`: act as a registered session (`lattice session start`) instead of `actor`.
- `origin.reported`: where the request came from, shown in `lattice show`. Only the keys `host`, `os_user`, `worktree`, `branch`, `client_version`, and `source` (which may only be `browser`), each a string of at most 256 characters (1,024 for `worktree`) with no control characters. The server stamps `origin.authenticated` (token, user, machine) itself and discards any the request sends.
- `attestations`: facts only the caller's machine can check, such as a reviewed commit's reachability for a completion policy. The server records them as claims of your token.
- `expect.last_event_id`: refuse with `CONFLICT` unless the task's latest event is this one.

Mint an operation ID with Lattice's own dependency, or, without Lattice installed, with the standard library:

```bash
op_id="op_$(python3 -c 'import ulid; print(ulid.ULID())')"
op_id="op_$(python3 -c 'import os,time; A="0123456789ABCDEFGHJKMNPQRSTVWXYZ"; n=(int(time.time()*1000)<<80)|int.from_bytes(os.urandom(10),"big"); print("".join(A[(n>>(5*i))&31] for i in range(25,-1,-1)))')"
echo "$op_id"
```

Create a task, with the operation ID just minted (the next section looks it up):

```bash
curl -s -H "Authorization: Bearer $LATTICE_TOKEN" -H 'Content-Type: application/json' \
  -d "{\"op_id\": \"$op_id\", \"params\": {\"title\": \"Created over HTTP\"}, \"actor\": \"agent:curl\"}" \
  "$LATTICE_URL/v1/projects/demo/ops/task.create"
```

Response:

```json
{"ok": true, "data": {"op_id": "op_01J9Z...", "seq": 8, "result": {
  "task": {"id": "task_01...", "short_id": "DEMO-1", "status": "backlog", "...": "..."},
  "events": [{"id": "ev_01...", "type": "task_created", "origin": {"...": "..."}, "...": "..."}],
  "value": {"...": "the data object `lattice create --json` prints"},
  "idempotent": false,
  "replayed": false
}}}
```

`seq` is the change's position in the project's journal. `result.value` is exactly what the matching CLI command prints under `--json`.

Move it, and comment on it:

```bash
curl -s -H "Authorization: Bearer $LATTICE_TOKEN" -H 'Content-Type: application/json' \
  -d "{\"op_id\": \"op_$(python3 -c 'import ulid; print(ulid.ULID())')\", \"params\": {\"task\": \"DEMO-1\", \"new_status\": \"in_planning\"}, \"actor\": \"agent:curl\"}" \
  "$LATTICE_URL/v1/projects/demo/ops/task.status"
curl -s -H "Authorization: Bearer $LATTICE_TOKEN" -H 'Content-Type: application/json' \
  -d "{\"op_id\": \"op_$(python3 -c 'import ulid; print(ulid.ULID())')\", \"params\": {\"task\": \"DEMO-1\", \"text\": \"Picked up by a script.\"}, \"actor\": \"agent:curl\"}" \
  "$LATTICE_URL/v1/projects/demo/ops/task.comment"
```

Write a plan (the content travels in the body; `file` holds the text, not a path):

```bash
curl -s -H "Authorization: Bearer $LATTICE_TOKEN" -H 'Content-Type: application/json' \
  -d "{\"op_id\": \"op_$(python3 -c 'import ulid; print(ulid.ULID())')\", \"params\": {\"task\": \"DEMO-1\", \"file\": \"# Plan\\n\\nOne step.\\n\"}, \"actor\": \"agent:curl\"}" \
  "$LATTICE_URL/v1/projects/demo/ops/task.plan_write"
```

Operations by CLI command:

| Command | Operation |
|---|---|
| `create`, `update`, `edit-description` | `task.create`, `task.update`, `task.edit_description` |
| `status`, `assign`, `needs-human` | `task.status`, `task.assign`, `task.needs_human` |
| `comment`, `comment-edit`, `comment-delete`, `react`, `unreact` | `task.comment`, `task.comment_edit`, `task.comment_delete`, `task.react`, `task.unreact` |
| `complete` | `task.complete` |
| `link`, `unlink` | `task.link`, `task.unlink` |
| `branch-link`, `branch-unlink`, `file-link`, `file-unlink` | `task.branch_link`, `task.branch_unlink`, `task.file_link`, `task.file_unlink` |
| `criterion add`, `edit`, `retire` | `task.criterion_add`, `task.criterion_edit`, `task.criterion_retire` |
| `archive`, `unarchive` | `task.archive`, `task.unarchive` |
| `claim`, `unclaim`, `next --claim` | `task.claim`, `task.unclaim`, `board.next_claim` |
| `attach` | `task.attach` (`payload: {filename, content_b64, sha256}`) |
| `event` | `task.event` (custom `x_` types) |
| `plan write`, `notes write` | `task.plan_write`, `task.notes_write` |
| `context write`, `board write` | `board.context_write`, `board.file_write` |
| `erase`, `unerase` | `task.erase`, `task.unerase` |
| `set-project-code`, `set-subproject-code` | `board.set_project_code`, `board.set_subproject_code` |
| `resource create`, `acquire`, `release`, `heartbeat` | `resource.create`, `resource.acquire` (one attempt; `RESOURCE_HELD` when held), `resource.release`, `resource.heartbeat` |
| `session start`, `session end` | `session.start`, `session.end` |

`GET /v1/info` lists every operation the server has, including plugin operations installed on it.

## GET /v1/projects/{slug}/ops/{op_id}

The outcome of one of **your token's** operations: use it when a write's response never arrived.

```bash
curl -s -H "Authorization: Bearer $LATTICE_TOKEN" "$LATTICE_URL/v1/projects/demo/ops/$op_id"
```

```json
{"ok": true, "data": {"state": "committed", "epoch": "ep_01...", "seq": 8, "result": {"...": "..."}}}
{"ok": true, "data": {"state": "not_found"}}
```

`committed` means the operation was applied (`result` is included while its receipt is kept, 7 days). `not_found` means it never committed, or belongs to another token. Retrying a committed operation with the same `op_id` and arguments returns its original result; with a new `op_id`, it applies again.

## GET /v1/projects/{slug}/sync

The cache protocol. Send the position you last saw; get every file that changed since.

```bash
curl -s -H "Authorization: Bearer $LATTICE_TOKEN" "$LATTICE_URL/v1/projects/demo/sync?since=0"
```

Query: `since` (the `head_seq` you last applied, 0 for none), `epoch` (the epoch it belongs to), `hash` (the `head_hash` you received with it), and `manifest=1` for hashes only.

Response `data`: `epoch`, `head_seq`, `head_hash`, `reset`, `files`, `removed`.

- With a matching `epoch` and `hash`, `reset` is `false` and `files` holds every path changed in entries `since+1..head_seq`, at its current content. When `since` is the head, `files` and `removed` are empty.
- Without an `epoch`, with a different one, or with a `hash` that does not match the server's history (for example after a restored backup), `reset` is `true` and `files` holds every board file. Replace your copy wholesale.
- `files[path]` is `{"sha256", "size", "content_b64"}` when the file is at most `limits.inline_file_bytes`, else `{"sha256", "size", "href"}`. An append-only log that grew may come as `{"sha256", "size", "append_from": L, "content_b64": <bytes from L>, "href"}`: append the bytes if your copy is exactly `L` bytes long, else fetch `href`. Check every file against `sha256`.
- Only board data is ever returned: never runtime, server control, or unmanaged paths.

## GET /v1/projects/{slug}/files/{path}

One board file, for an `href` from sync:

```bash
curl -s -H "Authorization: Bearer $LATTICE_TOKEN" "$LATTICE_URL/v1/projects/demo/files/config.json"
```

With `?sha256=<hash>`, the server answers 412 `STALE_VERSION` if the file has changed since; sync again.

## GET /v1/projects/{slug}/stream

Server-Sent Events (`text/event-stream`). Each committed change arrives once, in order.

```bash
timeout 5 curl -s -N -H "Authorization: Bearer $LATTICE_TOKEN" "$LATTICE_URL/v1/projects/demo/stream" || true
```

- `event: journal`, with `id: <epoch>:<seq>:<line hash>` and `data:` the journal entry (`seq`, `ts`, `op`, `op_id`, `task_id`, `event_ids`, `paths`, ...) plus `events`, the full events it appended.
- `event: heartbeat`, `data: {"epoch", "head_seq"}`, at connect and every `stream_heartbeat_seconds` (2 by default).
- `event: reset`, `data: {"epoch"}`: your position is not in the server's history (a new epoch, a restore, or more than 1,000 entries behind). Resync with `sync?since=0`.

Resume with the `Last-Event-ID` header set to the last `id` you received, or with `?since=<seq>&epoch=<epoch>&hash=<line hash>`. Without either, the stream starts at the current head: a heartbeat, then live entries. A project accepts at most `limits.max_stream_subscribers_per_project` streams (429 beyond it), and a subscriber too slow to keep up is disconnected, to resume from its `Last-Event-ID`. A revoked token's stream closes at its next heartbeat.

## GET /v1/projects/{slug}/tasks

```bash
curl -s -H "Authorization: Bearer $LATTICE_TOKEN" "$LATTICE_URL/v1/projects/demo/tasks?status=in_planning"
curl -s -H "Authorization: Bearer $LATTICE_TOKEN" "$LATTICE_URL/v1/projects/demo/tasks/DEMO-1"
```

The list returns `{"tasks": [...]}`, compact snapshots, filtered by `status`, `assigned`, and `include_archived=true`. One task returns `{"snapshot", "events", "plan"}`.

## Dashboard routes

`GET /login` serves a form; `POST /login` with a valid token (a form post from the same origin) sets the `lattice_session` cookie (`HttpOnly; SameSite=Strict`, `Secure` over HTTPS) for 30 days. `GET /` lists your projects; `/p/<slug>/` is each project's dashboard, whose `/p/<slug>/api/*` routes are the local dashboard's API served from the authoritative board. Cookie-authenticated POSTs need `Content-Type: application/json` and an `Origin` matching the host or `public_origins`. A dashboard write acts as the token's user when the token permits it (the "browser actor"); any actor in the request body is ignored. `POST /logout` ends the session. A session dies with its token.

## Error codes

The CLI prints the same codes; the HTTP status is the server's.

| Code | HTTP | Meaning |
|---|---|---|
| `VALIDATION_ERROR`, `MISSING_ARGS`, `INVALID_ID`, `INVALID_ROLE`, `INVALID_ACTOR` | 400 | Bad input |
| `MISSING_ACTOR` | 400 | No actor, and the token has no default actor |
| `LOCAL_ONLY` | 400 | A maintenance action that runs only on the server host |
| `PROTOCOL_MISMATCH` | 400 | `Lattice-Protocol` differs from the server's |
| `UNSUPPORTED_PARAM` | 400 | The server's operation lacks a parameter you sent; the message names it and both versions |
| `CLIENT_TOO_OLD` | 400 | `Lattice-Client-Version` is below the server's minimum |
| `UNAUTHENTICATED` | 401 | Missing, invalid, or revoked credential |
| `FORBIDDEN` | 403 | The token cannot reach this project, or the change is admin-only configuration |
| `ACTOR_NOT_PERMITTED` | 403 | The actor is outside the token's patterns; the message lists them and the command that widens them |
| `NOT_FOUND`, `NOT_INITIALIZED`, `PLAN_NOT_FOUND`, `SESSION_NOT_FOUND` | 404 | No such task, board, plan, or session |
| `UNKNOWN_OP` | 404 | The server has no such operation; the message names both versions |
| `CONFLICT` | 409 | An expectation or a `from` status did not hold, or an `op_id` was reused (`details.reason: "OP_ID_REUSED"`) |
| `ALREADY_CLAIMED`, `RESOURCE_HELD`, `NOT_HELD`, `EXPIRED`, `FLAG_ALREADY_SET`, `FLAG_NOT_SET` | 409 | State conflicts |
| `STALE_VERSION` | 412 | A file read with `?sha256=` whose content has changed |
| `PAYLOAD_TOO_LARGE` | 413 | Body over `limits.max_body_bytes`, or custom event data over `limits.max_event_data_bytes` |
| `INVALID_TRANSITION`, `PLAN_REQUIRED`, `COMPLETION_BLOCKED`, `REVIEW_CYCLE_LIMIT` | 422 | Workflow rules |
| `TASK_ERASED` | 422 | A write to an erased task |
| `RATE_LIMITED` | 429 | A per-token limit; wait `Retry-After` seconds |
| `INTEGRITY_ERROR` | 500 | A task log failed strict replay |
| `BOARD_BUSY` | 503 | The project lock was not acquired in time; `Retry-After: 2` |
| `BOARD_UNAVAILABLE` | 503 | The project failed its integrity check or recovery, or is unloaded |
| `STORAGE_LOW` | 507 | The server's disk is below its floor; writes refused, reads work |

Every rejection about a task's state (`CONFLICT` from an expectation or `from` mismatch, `ALREADY_CLAIMED`, `FLAG_ALREADY_SET`, `FLAG_NOT_SET`, and the 422 codes) carries the task's current compact snapshot in `details.snapshot`. A reused operation ID:

```json
{"ok": false, "error": {"code": "CONFLICT", "message": "operation id op_01J9Z... was already used with different arguments", "details": {"reason": "OP_ID_REUSED", "seq": 118}}}
```

**Retrying.** Retry with the same `op_id` on a connection error, a timeout, 429, 502, 504, and 503 other than `BOARD_UNAVAILABLE`, honoring `Retry-After`, else backing off from 0.5 s up to 5 s. Use a read timeout of at least 90 seconds for operations: a request can wait up to 60 seconds for the project lock. If you give up after a request may have reached the server, check `GET .../ops/<op_id>` before sending the write again.
