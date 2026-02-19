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
- `/api/stats`, `/api/activity`, `/api/archived`, `/api/graph`, `/api/epics`
- `/api/git`, `/api/git/branches/<name>/commits`

These are used by the frontend for board, graph, activity, and git overlays.

## Write APIs

Representative mutation endpoints:

- task create/update/status/assign/archive
- comment add/edit/delete
- reaction add/remove
- dashboard config write
- open notes/plans in editor helpers

Dashboard write handlers mirror CLI logic: validate inputs, create events,
materialize updated snapshots, persist through storage operations.

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
