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
  - `Lattice-Client-Version: <version>` (optional; the Lattice client always sends it). An operation below the server's minimum fails with `CLIENT_TOO_OLD` before execution. Sync and stream also check it before returning data when the project holds any synced issue file (even an ID map with no entries); that conditional read gate is separate from the server-wide operation refusal.
- **Task IDs.** The `/v1` routes and operation parameters take a ULID (`task_01...`) or a short ID (`DEMO-1`) wherever a task is named. The dashboard routes (`/p/<slug>/api/tasks/<id>...`) take the full ULID only; a short ID there answers 400 `INVALID_ID`.
- **User-Agent.** The server does not check it, but a proxy's bot protection may. The Lattice client sends `lattice/<version>`; a script behind such a proxy sends a User-Agent the proxy admits.
- **Redirects.** The server never redirects an API path. If you see a 3xx, a proxy is in the way (guide section 10).

## Routes

| Method and path | Auth | Purpose |
|---|---|---|
| `GET /healthz` | none | Liveness, free disk, project counts |
| `GET /v1/info` | token | Server version and protocol, your identity, your projects, registered operations and event types, audit state |
| `GET /v1/projects` | token | Projects your token can see |
| `POST /v1/projects/{slug}/ops/{op}` | token | Run an operation (a write) |
| `GET /v1/projects/{slug}/ops/{op_id}` | token | The outcome of one of your own operations: `committed`, `in_flight`, or `not_found` |
| `PUT /v1/projects/{slug}/issues/media/staging/{sha256}` | token with project write permission | Upload raw issue-media bytes to private staging; does not mutate board state |
| `GET /v1/projects/{slug}/issues/media/availability?issue=<ULID>` | token with project read permission | Presence metadata for issue media and frames; repeat `issue` for a batch |
| `GET /v1/projects/{slug}/issues/media/{issue_id}/{media_id}` | token with project read permission | Read one media object; supports byte ranges |
| `GET /v1/projects/{slug}/issues/media/{issue_id}/{media_id}/frames/{frame_name}` | token with project read permission | Read one client-derived frame; supports byte ranges |
| `GET /v1/projects/{slug}/sync?since=N&epoch=E&hash=H` | token | What changed since your last sync |
| `GET /v1/projects/{slug}/files/{path}` | token | One board file |
| `GET /v1/projects/{slug}/stream` | token or session | Server-Sent Events: every committed change, live |
| `GET /v1/projects/{slug}/tasks/{id}` | token | One task: snapshot, events, plan |
| `GET /v1/projects/{slug}/tasks?status=&assigned=&include_archived=` | token | Compact snapshots of the project's tasks |
| `GET /` | session | Dashboard index of your projects |
| `GET /login`, `POST /login` | none (`POST` authenticates with the token it submits) | Dashboard login |
| `POST /logout` | session | End the dashboard session |
| `GET /p/{slug}/`, `/p/{slug}/static/*`, `/p/{slug}/api/*` | session or token | The project's dashboard |
| `PUT /p/{slug}/issues/media/staging/{sha256}` | session | Same-origin raw upload for a hosted issue file; returns prepared metadata backed by private staging |
| `GET /p/{slug}/issues/media/{issue_id}/{media_id}` and `GET /p/{slug}/issues/media/{issue_id}/{media_id}/frames/{frame_name}` | session | Same-origin dashboard media reads; use the same range-serving helper as `/v1` |
| `GET /web/dashboard.css`, `GET /web/logout.js` | none | The login and index pages' stylesheet and logout script |

A dashboard session cookie authenticates only `/`, `/logout`, `/p/<slug>/...`, and the stream. It never authenticates operations, sync, or files.

### Filing-only tokens

`lattice server token create --only issue.file` mints a bearer token for exactly one `--project SLUG`; it cannot be combined with `--all-projects`. It also requires `--source NAME`. The server stores that trimmed source on the token. The request's `params.source` must match it or the server returns 403 `TOKEN_RESTRICTED`. A filing token may call only the exact `POST /v1/projects/{slug}/ops/issue.file` route and the exact `PUT /v1/projects/{slug}/issues/media/staging/{sha256}` route for that project. Every other method or route is denied with 403 `TOKEN_RESTRICTED`, including `GET /v1/projects/{slug}/ops/{op_id}` (op-status), all other operation names, `/v1/info`, project and issue reads, sync, stream, files, task and media reads, dashboard routes, session routes, public routes when the restricted bearer credential is supplied, and unknown routes. Trailing slashes, duplicate slashes, and encoded path separators do not widen the allowlist. It cannot create a dashboard session at `/login`, and a session backed by a filing-only token is denied. A filing-only `issue.file` request cannot use `actor_name`; its `actor` must still match the token's actor rules. Unauthenticated `GET /healthz` remains public.

The restriction is stored with the token record. A filing token's `sha256` value is prefixed `only-v1:<hex-digest>` so pre-LAT-389 readers cannot authenticate it. New readers treat a `only-v1:` record without a valid filing restriction, or with an unknown restriction value, as unusable, never as an unrestricted token. Unrestricted records keep their existing hash and behavior. Granting a filing token additional projects or actors cannot widen its filing restriction; revoke and mint a replacement to change its scope.

Mint-time limit overrides are `--ops-per-minute N`, `--bytes-per-minute N`, and `--max-staged-bytes N` (bytes for the last two flags). Unset operation and body-byte limits on unrestricted tokens use the server's current per-token limits; an unrestricted token with no staged-byte override keeps the existing project-quota behavior. For filing-only tokens, omitted limits default to 30 operations/minute, `max(64 MiB, the live max_issue_media_file_bytes)` of request bodies/minute, and 512 MiB of unreferenced staged objects owned by that token. This byte-rate default tracks the server's current per-file media cap so one legal file fits in the token bucket. An explicit `--bytes-per-minute` override for any token must be at least the live `max_issue_media_file_bytes` when minted. If a later server config change makes one body larger than that token's effective byte capacity, the server returns non-retryable 413 `PAYLOAD_TOO_LARGE` with `details: {limit_bytes, scope: "token"}` before charging the byte bucket. Per-token operation or body rate refusals return 429 `RATE_LIMITED` with `Retry-After`. If a staged upload would put that token over its staged-byte cap, the server returns 413 `MEDIA_QUOTA_EXCEEDED` with `details.scope: "token"` and does not store the object. Re-uploading a hash that token already owns does not count it twice.

### `issue.file` source references and filing receipts

`issue.file` accepts `source_ref` alongside `source`. A `source_ref` requires a nonempty `source`. When the pair is supplied, the server trims surrounding whitespace, preserves case, rejects blank or control-character values, and limits `source` to 128 characters and `source_ref` to 256 characters. A longer `source_ref` returns HTTP 400 `VALIDATION_ERROR`; it is never truncated. Callers with unusually long RFC 5322 email Message-IDs should hash the Message-ID and use that digest as `source_ref`.

The deduplication key is `(project, source, source_ref)` and does not include the filing token: it can match an issue originally filed by any token with that pair. The first filing commits one `issue_filed` event. A later request with the same normalized pair returns that original issue without changing its title, reporter, media, links, or closure state and without committing another issue event. Concurrent filings with the same pair resolve to one issue. A distinct `op_id` still gets the ordinary successful operation transaction, journal sequence, and receipt; a dedupe response has `events: []` and `idempotent: true`.

`actor` is the authorized service actor. Optional `on_behalf_of` is a separate reporter label, not an actor or permission identity. For `issue.file` it is trimmed, nonblank free-form printable text of at most 256 characters with no control characters. A new issue filed by an authenticated filing-only token is marked `external: true`; clients cannot set that marker. On a dedupe hit, `external` reflects the original issue's marker, so it can be `false` when another token filed that issue. Full-token callers keep the full issue view and normal event result, including `external`, `on_behalf_of`, and `source_ref` when present.

Filing-only callers receive a receipt as `data.result.value` on the first filing, a source-ref dedupe hit, and a same-`op_id` replay. The receipt has exactly these keys; `source_ref` is `null` if omitted, and `deduplicated` is true only for a source-ref hit:

```json
{
  "id": "<issue-id>",
  "short_id": "<short-id>",
  "filed_at": "<UTC timestamp>",
  "source": "reporter-links",
  "source_ref": "ISS-7K2MQ",
  "external": true,
  "deduplicated": false
}
```

For filing-only callers, `data.result.events` is `[]` and the envelope fields `task`, `resource_id`, and `resource_name` are always `null` on all three paths. The response and durable operation receipt never include the title, description, evidence, media, closure, task links, or `filed_origin`. On a dedupe hit, the receipt has the same `id`, `short_id`, `filed_at`, `source`, and `source_ref` as the first filing, with `deduplicated: true`; a same-`op_id` replay returns the original receipt with the normal `replayed: true` indicator. The full issue view is available only to full tokens. This receipt is the stable filing contract for LAT-390, LAT-370, RP-V1-94, and LAT-392; filing-only callers cannot read issues with list or show routes.

If a filing response is lost, `GET /v1/projects/{slug}/ops/{op_id}` is deliberately unavailable to this token. Retry `issue.file` with the same bound source and `source_ref`; use the same `op_id` for an ordinary receipt replay while it is retained, or a fresh `op_id` to receive the source-ref dedupe receipt. This pair is the safe recovery key and prevents a second issue event even after the operation receipt expires. A dedupe hit does not attach, consume, or release media the token staged for the retry. That media remains owned by the token and counts against its staged-byte quota until the normal staging expiry.

## GET /healthz

No token. Touches no board.

```bash
curl -s "$LATTICE_URL/healthz"
```

```json
{"ok": true, "version": "2.0.0", "protocol": 1, "disk_free_bytes": 52613349376,
 "projects": {"loaded": 3, "loading": 0, "unloaded": 0, "unavailable": 0}}
```

`version` reads `2.0.0` from the release on; until the release sets it (SPEC §15), the package reports its pre-release number (`0.2.x`), as `client_version` below does. Status 503 with `"ok": false` when free disk is below `limits.min_free_disk_bytes`. Project counts only; never slugs.

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

- `op_id`: `op_` followed by a ULID, **fresh for every new request**. Retrying the *same* request with the same `op_id` within the receipt retention window (7 days) is safe: the server applies it at most once and returns the original result with `replayed: true`. After 7 days the server no longer remembers the `op_id`, and the same request **runs again**; before retrying anything older, check `GET .../ops/<op_id>` (below) and do not resend if it is `committed`. The same `op_id` with different arguments fails with `CONFLICT` (`details.reason: "OP_ID_REUSED"`). **A request without `op_id` is never deduplicated**: the server mints an ID, and a repeated request applies again.
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
| `issue file` | `issue.file`: takes `title` or legacy `text`, never both; optional `description` |
| `issue link`, `issue unlink` | `issue.link`, `issue.unlink` |
| `issue dismiss`, `issue duplicate`, `issue reopen`, `issue promote` | `issue.dismiss`, `issue.duplicate`, `issue.reopen`, `issue.promote` |
| `issue edit`, `issue comment` | `issue.edit`, `issue.comment` |
| `issue attach`, `issue detach` | `issue.attach`, `issue.detach` |

`GET /v1/info` lists every operation the server has, including plugin operations installed on it. The current hosted issue operation set is `issue.file`, `issue.link`, `issue.unlink`, `issue.dismiss`, `issue.duplicate`, `issue.reopen`, `issue.promote`, `issue.attach`, `issue.detach`, `issue.edit`, and `issue.comment`. Issue reads use synced metadata; media bytes use the private staging and read routes below.

## Issue media upload, availability, and reads

Issue media is separate from the issue metadata carried by sync. Upload each stored original and each client-derived frame as its own raw request. The staging route requires a bearer token with write permission for the project and a `Content-Length`; the URL digest must be exactly 64 lowercase hexadecimal characters. The request body is the file bytes, not JSON or base64:

```bash
curl -s -X PUT \
  -H "Authorization: Bearer $LATTICE_TOKEN" \
  -H 'Content-Type: application/octet-stream' \
  -H 'Content-Length: 21832' \
  --data-binary @shot.jpg \
  "$LATTICE_URL/v1/projects/demo/issues/media/staging/<64-lowercase-hex-sha256>"
```

The response uses the normal JSON envelope and describes the verified object (`sha256`, `size_bytes`, detected `content_type`, and `staged: true`). The server reserves project quota from the declared length, then streams the body and verifies its actual length, SHA-256, and supported photo/video type. Upload staging is under the project's private server runtime directory, outside the board. It creates no operation receipt, board sequence, audit commit, or stream event. Retrying the same hash and size is idempotent; a hash/size conflict fails, and a verified re-upload replaces a staged copy that has been damaged. Each stage records an owner set. A verified re-upload adds the authenticated token to that set without removing existing owners. A filing-only token can consume a stage only when its token ID is in the owner set; a stage owned only by another token answers `NOT_FOUND` to it. Unrestricted tokens retain the existing cross-token staging behavior. The route refuses with `ISSUES_DISABLED` when the project's issue log is off, before reserving anything. The server waits at most 30 seconds for each chunk of the body; a stalled upload ends with `408 UPLOAD_TIMEOUT` and releases its reservation. When the server refuses an upload before reading its body (over quota, too large, rate limited, the same hash already uploading), it first reads and discards the rest of the body, up to twice the per-file limit and stopping after 2 seconds without data, so the client receives the refusal instead of a broken connection. Failed uploads release their reservation, and abandoned stages expire.

The raw upload limit is `limits.max_issue_media_file_bytes` (100 MiB per stored object). Filing-token uploads are additionally bounded by the token's effective byte capacity; one body above it is refused with 413 `PAYLOAD_TOO_LARGE` and `details.scope: "token"`, without waiting for a rate-limit retry. Hosted issue mutations remain separate JSON operations: `issue.file` and `issue.attach` pass `payload: {filename, sha256, size, staged: true}` and use the same shape under each `frames[].payload`; the server accepts neither media bytes nor `content_b64` in hosted operation params. Local operations keep the `{filename, content_b64, sha256}` form. The server verifies staged bytes again when the named operation consumes them. These operations commit the issue event and snapshot in the ordinary write transaction, then finalize media paths after commit. `issue.detach` commits its removal event before unlinking or quarantining the bytes. The server trusts client-supplied video metadata and source hashes, while verifying the uploaded stored object's own hash and size. Raw staging avoids the 16 MiB JSON body cap, which leaves roughly 12 MiB for base64 file content.

`GET /v1/projects/{slug}/issues/media/availability?issue=<ULID>` requires a bearer token with read permission. Repeat `issue` to check several issues. The response contains only present media/frame identifiers, hashes, and sizes, never bytes; availability is not part of sync or its reset manifest. A hosted client combines this result with verified local-cache files to report `local`, `remote`, or `missing` availability (`unreachable` when the server cannot be asked and nothing is cached).

`GET /v1/projects/{slug}/issues/media/{issue_id}/{media_id}` reads a stored original. `GET /v1/projects/{slug}/issues/media/{issue_id}/{media_id}/frames/{frame_name}` reads one client-derived JPEG frame. Both require a bearer token with project read permission. The server refuses traversal and symlinks, validates every recorded SHA-256 during issue-event replay and before using it in a header or path, and serves only regular files. A valid `Range` request returns `206` with `Accept-Ranges`, `Content-Range`, and the recorded content type; each range is capped at 1 MiB, and a request without `Range` returns the whole object. Responses stream from disk in 1 MiB chunks; the server never holds a whole object in memory. An unsatisfiable range returns `416`. The hosted dashboard has a same-origin session-protected `GET /p/{slug}/issues/media/...` route using the same serving rules; a dashboard cookie does not authenticate `/v1` operations.

The server limit is 250 MiB per issue, including frame sidecars, and 10 GiB per project by default; `max_issue_media_project_bytes` is configurable in `server.json`. Per-file and per-issue limit failures use `PAYLOAD_TOO_LARGE`. Project quota exhaustion uses `MEDIA_QUOTA_EXCEEDED` (HTTP 413). A filing token's staged-byte cap counts unreferenced staged hashes whose owner set contains that token; its refusal is also `MEDIA_QUOTA_EXCEEDED` (HTTP 413), with `details.scope: "token"`. These caps are separate from `limits.max_body_bytes` for JSON requests.

## GET /v1/projects/{slug}/ops/{op_id}

The outcome of one of **your token's** operations: use it when a write's response never arrived.

```bash
curl -s -H "Authorization: Bearer $LATTICE_TOKEN" "$LATTICE_URL/v1/projects/demo/ops/$op_id"
```

```json
{"ok": true, "data": {"state": "committed", "epoch": "ep_01...", "seq": 8, "result": {"...": "..."}}}
{"ok": true, "data": {"state": "in_flight"}}
{"ok": true, "data": {"state": "not_found"}}
```

`committed` means the operation was applied (`result` is included while its receipt is kept, 7 days). `in_flight` means the server has accepted the request and has not finished it: it may still commit, so ask again rather than resending. `not_found` means it never committed (or was rolled back), or belongs to another token. Retrying a committed operation with the same `op_id` and arguments returns its original result while its receipt is kept (7 days); after that, or with a new `op_id`, it applies again. So check op status before retrying any request older than a few days.

## GET /v1/projects/{slug}/sync

The cache protocol. Send the position you last saw; get every file that changed since.

```bash
curl -s -H "Authorization: Bearer $LATTICE_TOKEN" "$LATTICE_URL/v1/projects/demo/sync?since=0"
```

Query: `since` (the `head_seq` you last applied, 0 for none), `epoch` (the epoch it belongs to), `hash` (the `head_hash` you received with it), and `manifest=1` for hashes only. Before returning any delta or reset manifest, the server checks `Lattice-Client-Version`: if the project holds any synced issue file (even an ID map with no entries) and the client is below `0.2.2`, the response is `CLIENT_TOO_OLD` and contains no issue-bearing data. A project with no synced issue file keeps its existing sync behavior.

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

Server-Sent Events (`text/event-stream`). Each committed change arrives once, in order. Before sending the initial heartbeat, replay, or any live entry, the server checks `Lattice-Client-Version`: if the project holds any synced issue file (even an ID map with no entries) and the client is below `0.2.2`, it returns `CLIENT_TOO_OLD` without stream events. A project with no synced issue file keeps its existing stream behavior.

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

These serve the hosted dashboard (guide section 11, Dashboards). A script can use them with a bearer token, like any other route.

**Login and sessions.**

- `GET /login` serves a form. `POST /login` is a form post (`application/x-www-form-urlencoded`, at most 4 KiB) with `token` and optional `next` (a `/p/<slug>/` path). It checks the `Origin` first (403 `FORBIDDEN` from another site), then the token (401 with the form again if it is not valid), then sets the `lattice_session` cookie (`HttpOnly; SameSite=Strict; Path=/`, plus `Secure` over HTTPS) and redirects (303) to `next`, else `/`. The session lasts 30 days; the server stores only its hash, in `web_sessions.json`.
- `POST /logout` with `Content-Type: application/json` and a body of `{}` ends the session and clears the cookie: `{"ok": true, "data": {"logged_out": true}}`.
- `GET /` lists the projects the session's token may see. Without a session it redirects (303) to `/login`.
- A session dies with its token. A request carrying a cookie that names no live session gets a clearing `Set-Cookie`.
- The cookie authenticates only `/`, `/logout`, `/p/<slug>/...`, and the stream. When an `Authorization` header is present it is used alone, never falling back to the cookie.

**A project's dashboard.**

- `GET /p/<slug>/` serves the page (without a session: 303 to `/login?next=/p/<slug>/`); `GET /p/<slug>` redirects (308) to it. `/p/<slug>/static/*` serves its assets; nothing is loaded from another site.
- `GET /p/<slug>/api/<path>` answers the local dashboard's read API on the server's board: `config`, `tasks`, `stats`, `activity`, `archived`, `graph`, `structure`, `tasks/<id>`, and the rest. `api/graph` carries an `ETag` (its revision); send it back in `If-None-Match` to get 304 when the graph has not changed. The other read routes answer 200 with no `ETag` every time. `api/git` reports `{"available": false, "reason": "hosted"}`.
- `GET /p/<slug>/api/issues` and `GET /p/<slug>/api/issues/<issue_id>` read the hosted issue log from the authoritative board, with the same project lock and head-keyed read memo. A bound-checkout dashboard reads issue metadata from its synced mirror under the cache read lock and omits media URLs; neither dashboard serves media from a checkout-local path.
- `PUT /p/<slug>/issues/media/staging/<sha256>?filename=<name>` is the hosted dashboard's same-origin upload path. It requires the session cookie, a matching `Origin`, `Content-Type: application/octet-stream`, and `Content-Length`; an `Authorization` header is refused rather than falling back to the cookie. The body is one raw file. The server verifies the input hash, calls the shared dashboard media preparation, stages the prepared original and video frames in this project's private `HostedIssueMedia` store, and returns a media item containing staged metadata. `POST /p/<slug>/api/issues` then sends only that staged metadata in its JSON body.
- Hosted issue writes use the token's browser actor (§8.3), even when the body names another actor. The local single-user dashboard continues to honor an explicit actor and uses its configured human actor or `dashboard:web` default when none is sent.
- `GET /p/<slug>/api/tasks` takes the origin filters `machine`, `user`, and `worktree`, as `lattice list --machine/--user/--worktree` (a task matches when one of its events carries every filter given; tasks written before v2 match nothing). On a hosted board, machine and user are the token's. An empty value is no filter. `worktree` must be an absolute path and is normalized lexically (repeated and trailing slashes, `.` and `..`); the server never resolves it against a filesystem, so a symlinked path matches nothing. Refusals, 400 `VALIDATION_ERROR`: a relative `worktree` ("worktree filter must be an absolute path"), and a value longer than 256 characters (`machine`, `user`) or 1024 (`worktree`).

```bash
curl -s -H "Authorization: Bearer $LATTICE_TOKEN" "$LATTICE_URL/p/demo/api/tasks?machine=laptop&user=human:alice"
```

- `POST /p/<slug>/api/<path>` runs the matching operation as the token's browser actor: the token's user when the token permits it, else its single default actor, else 400 `MISSING_ACTOR`. Any actor in the body is ignored. It needs `Content-Type: application/json` (415 otherwise) and, with a session cookie, an `Origin` equal to the server's own or listed in `public_origins` (403 otherwise, checked before anything else). Send a `Lattice-Op-Id: op_<ULID>` header per logical write and reuse it on retry, so a retry applies once; without it the server mints one and cannot deduplicate. `POST .../api/tasks/<id>/open-notes` and `open-plans` answer 400 `LOCAL_ONLY`: write plans and notes with `plan.write` and `notes.write`.
- Responses outside `/v1` carry `X-Content-Type-Options: nosniff` and a `Content-Security-Policy` that allows only this server's own scripts, styles, and connections; `img-src` also allows `blob:` and `data:` for image previews, while `media-src` allows `blob:` for local video previews.

**Live refresh.** The page follows `GET /v1/projects/<slug>/stream` with its session cookie and refetches on each entry; with the stream down, it polls every 5 seconds.

## Version compatibility

The hosted issue path sets both the pre-release package version and `min_client_version` to `0.2.2`. The minimum applies to writes server-wide: every operation request from a client below the floor is rejected with `CLIENT_TOO_OLD` before execution, even on a project without issues. Sync and stream have a separate conditional data gate: when a project holds any synced issue file (even an ID map with no entries), a client below `0.2.2` is refused before any delta, reset manifest, heartbeat, or stream event is returned. Projects with no synced issue file keep their existing read behavior. The conditional read gate protects the newly synced issue path; it does not replace or narrow the server-wide write refusal.

## Error codes

The CLI prints the same codes; the HTTP status is the server's.

| Code | HTTP | Meaning |
|---|---|---|
| `VALIDATION_ERROR`, `MISSING_ARGS`, `INVALID_ID`, `INVALID_ROLE`, `INVALID_ACTOR` | 400 | Bad input |
| `MISSING_ACTOR` | 400 | No actor, and the token has no default actor |
| `LOCAL_ONLY` | 400 | A maintenance action that runs only on the server host |
| `PROTOCOL_MISMATCH` | 400 | `Lattice-Protocol` differs from the server's |
| `MEDIA_STAGE_UNAVAILABLE` | 400 | A staged-media payload (`staged: true`) reached an operation that is not a hosted issue operation, such as a local board |
| `HOSTED_MEDIA_INLINE_UNSUPPORTED` | 400 | A hosted operation carried inline `content_b64`; stage the bytes through the raw upload route |
| `UNSUPPORTED_PARAM` | 400 | The server's operation lacks a parameter you sent; the message names it and both versions |
| `CLIENT_TOO_OLD` | 400 | `Lattice-Client-Version` is below the server's minimum |
| `UNAUTHENTICATED` | 401 | Missing, invalid, or revoked credential |
| `FORBIDDEN` | 403 | The token cannot reach this project, or the change is admin-only configuration |
| `ACTOR_NOT_PERMITTED` | 403 | The actor is outside the token's patterns; the message lists them and the command that widens them |
| `NOT_FOUND`, `NOT_INITIALIZED`, `PLAN_NOT_FOUND`, `SESSION_NOT_FOUND` | 404 | No such task, board, plan, session, staged object, or hosted media object |
| `UNKNOWN_OP` | 404 | The server has no such operation; the message names both versions |
| `CONFLICT` | 409 | An expectation or a `from` status did not hold, or an `op_id` was reused (`details.reason: "OP_ID_REUSED"`) |
| `ALREADY_CLAIMED`, `RESOURCE_HELD`, `NOT_HELD`, `EXPIRED`, `FLAG_ALREADY_SET`, `FLAG_NOT_SET` | 409 | State conflicts |
| `STALE_VERSION` | 412 | A file read with `?sha256=` whose content has changed |
| `PAYLOAD_TOO_LARGE` | 413 | JSON body over `limits.max_body_bytes`, custom event data over `limits.max_event_data_bytes`, or media over its per-file or per-issue limit |
| `MEDIA_QUOTA_EXCEEDED` | 413 | An upload would take the project over `limits.max_issue_media_project_bytes`, or a filing token over its `--max-staged-bytes` cap (`details.scope: "token"`) |
| `TOKEN_RESTRICTED` | 403 | A filing-only token attempted a route, method, operation, or source outside its exact filing scope |
| `RANGE_NOT_SATISFIABLE` | 416 | A media `Range` the stored file cannot satisfy; `details.size_bytes` names the size |
| `INVALID_TRANSITION`, `PLAN_REQUIRED`, `COMPLETION_BLOCKED`, `REVIEW_CYCLE_LIMIT` | 422 | Workflow rules |
| `TASK_ERASED` | 422 | A write to an erased task |
| `UPLOAD_TIMEOUT` | 408 | A media upload stalled for more than 30 seconds between chunks |
| `RATE_LIMITED` | 429 | A per-token limit; wait `Retry-After` seconds |
| `INTEGRITY_ERROR` | 500 | A task log failed strict replay |
| `BOARD_BUSY` | 503 | The project lock was not acquired in time; `Retry-After: 2` |
| `BOARD_UNAVAILABLE` | 503 | The project failed its integrity check or recovery, or is unloaded |
| `STORAGE_LOW` | 507 | The server's disk is below its floor; writes refused, reads work |

Issue-media uploads and operation params use `VALIDATION_ERROR` for invalid hashes, sizes, content, or payload shapes. A hosted operation that receives inline `content_b64` fails with `HOSTED_MEDIA_INLINE_UNSUPPORTED`; stage bytes through the raw upload route instead. `MEDIA_QUOTA_EXCEEDED` (413) identifies a project media quota refusal; a staged payload sent where it is not accepted (a local board) is `MEDIA_STAGE_UNAVAILABLE` (400), and a staged object the server does not hold is `NOT_FOUND`.

Every rejection about a task's state (`CONFLICT` from an expectation or `from` mismatch, `ALREADY_CLAIMED`, `FLAG_ALREADY_SET`, `FLAG_NOT_SET`, and the 422 codes) carries the task's current compact snapshot in `details.snapshot`. A reused operation ID:

```json
{"ok": false, "error": {"code": "CONFLICT", "message": "operation id op_01J9Z... was already used with different arguments", "details": {"reason": "OP_ID_REUSED", "seq": 118}}}
```

**Retrying.** Retry with the same `op_id` on a connection error, a timeout, 429, 502, 504, and 503 other than `BOARD_UNAVAILABLE` (and on a 502, 503, or 504 without `Lattice-Protocol`, which a gateway in front of the server sent), honoring `Retry-After`, else backing off from 0.5 s up to 5 s. Use a read timeout of at least 90 seconds for operations: a request can wait up to 60 seconds for the project lock. If you give up after a request may have reached the server, check `GET .../ops/<op_id>` before sending the write again.
