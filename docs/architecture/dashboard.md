# Dashboard Architecture

## Purpose

The dashboard is a local HTTP UI over `.lattice/` state.

Main implementation:

- `src/lattice/dashboard/server.py`
- `src/lattice/dashboard/git_reader.py`
- static frontend in `src/lattice/dashboard/static/`

## Server Model

`server.py` builds a custom `BaseHTTPRequestHandler` via
`_make_handler_class(lattice_dir, readonly=...)`.

Top-level behavior:

- `GET /` serves static UI
- `GET /api/*` serves JSON data endpoints
- `POST /api/*` handles mutations when not in read-only mode

The server is a `ThreadingHTTPServer`, but one module lock (`_BOARD_LOCK`)
runs every POST and every GET except issue media one at a time, exactly as a
single-threaded server would. Only the issue media GETs below run beside
them, so a browser holding a video stream open never stalls the board.

JSON envelope is consistent:

- success: `{ "ok": true, "data": ... }`
- error: `{ "ok": false, "error": { "code", "message" } }`

## Read APIs

Key read endpoints:

- `/api/config`
- `/api/tasks`
- `/api/tasks/<id>` and `/api/tasks/<id>/events`
- `/api/tasks/<id>/comments`
- `/api/tasks/<id>/full`
- `/api/stats`, `/api/activity`, `/api/archived`, `/api/graph`
- `/api/git`, `/api/git/branches/<name>/commits`

These are used by the frontend for board, graph, activity, and git overlays.

Issue media (LAT-366, `dashboard/media.py`), local boards with the issue log on:

- `GET /api/issues/<iss_ULID>/media/<med_ULID>`: a photo or video's bytes
- `GET /api/issues/<iss_ULID>/media/<med_ULID>/frames/<tNNNN.NNNs.jpg>`: a video's frame

The file must be listed in the issue's snapshot, not removed, with an accepted
type; its path comes from the IDs and the recorded type, and a symlink or
non-regular file is refused (404). Responses carry the recorded
`Content-Type`, `X-Content-Type-Options: nosniff`,
`Content-Security-Policy: default-src 'none'; sandbox`, `Accept-Ranges: bytes`
and, for media, `ETag: "<sha256>"` (304 on `If-None-Match`). One `Range` is
honoured with 206, capped at 1 MiB per response; several ranges or a malformed
header get the whole file (200); a start past the end gets 416. A malformed ID
is 400 `INVALID_ID`, a bound checkout 400 `LOCAL_ONLY`, the log off 409
`ISSUES_DISABLED`. A video poster is its first frame's URL; the server makes
no thumbnails.

`/api/tasks` does not return whole snapshots. Each task is passed through
`compact_snapshot()` in `src/lattice/core/tasks.py`, which projects a fixed
field allowlist, and the handler then layers on a few extras (`created_at`,
`updated_at`, `done_at`, `has_active_session`).

**A field the board UI needs must be added to that allowlist.** This is the
easy trap: the frontend can be complete — filter controls, URL params, i18n,
chips — and still be inert, because the payload never carries the field and
the UI reads it as absent for every task. A filter section that hides itself
when no task has a value will simply never appear, with no error anywhere.
The `created_by` creator filter shipped broken this way. If a new board
feature reads `task.<field>` and always sees null, check the allowlist first.

The graph endpoint (`/api/graph`) keeps its own separate node projection, so
a field added for the board does not automatically reach the graph.

Full task responses preserve complete evidence-reference objects, including
nullable roles and optional criterion IDs. Task details render active and
retired criteria, current revisions and histories, linked-evidence counts and
badges, plus criterion event summaries in activity. The dashboard never infers
or renders criterion satisfaction/pass state.

## Write APIs

Representative mutation endpoints:

- task create/update/status/assign/archive
- comment add/edit/delete
- reaction add/remove
- dashboard config write
- open notes/plans in editor helpers

Dashboard write handlers mirror CLI logic: validate inputs, create events,
and persist through the canonical storage callback mutation path. Criteria
remain read-only in the dashboard; use CLI or MCP to mutate them.

## Safety and Validation

Built-in safeguards include:

- path traversal checks for static/file serving
- request body size cap (`MAX_REQUEST_BODY_BYTES`)
- actor/status/type/transition validation
- branch-name validation for git endpoints

## Git Integration

`git_reader.py` provides optional git summaries, branch metadata, and recent
commits. Dashboard degrades gracefully when git is unavailable or repo root is
missing.

## Design Constraint

The dashboard is intentionally lightweight (stdlib HTTP server, no heavy backend
framework). Keep dependencies minimal and preserve parity with CLI semantics for
state mutation behavior.
