# Task: Implement the Lattice dashboard

## Context

Lattice is a file-based, agent-native task tracker with an event-sourced core. The CLI commands, event system, and integrity tools are already implemented (phases 1–2). This prompt adds the read-only local dashboard.

The dashboard is a developer tool — functional and clean, not flashy. It reads `.lattice/` on demand and never writes to it.

## Before writing any code, read these files in order:

1. `CLAUDE.md` — Project guide, architecture, coding conventions
2. `ProjectRequirements_v1.md` — Section 14.1 (dashboard spec)
3. All existing source files — understand the data structures the dashboard will read

## Existing codebase

After phase 2, these are available for the dashboard to use:

- `storage/fs.py` — `find_root()` for locating `.lattice/`, `LATTICE_DIR` constant
- `core/config.py` — `load_config()` for parsing config
- `core/tasks.py` — `compact_snapshot()` for compact task views, `serialize_snapshot()` for JSON
- `core/events.py` — `serialize_event()` for event formatting
- Task snapshots live at `.lattice/tasks/<task_id>.json`
- Event logs live at `.lattice/events/<task_id>.jsonl`
- Global log at `.lattice/events/_global.jsonl`
- Archived tasks at `.lattice/archive/tasks/<task_id>.json`
- Artifact metadata at `.lattice/artifacts/meta/<art_id>.json`
- Config at `.lattice/config.json`

The `dashboard/` package exists as a stub: `__init__.py`, `server.py` (docstring only), `static/.gitkeep`.

## What to build

### 1. `lattice dashboard` command

Add to the CLI:

```
lattice dashboard [--host HOST] [--port PORT] [--json]
```

- Default host: `127.0.0.1` (localhost only — no network exposure).
- Default port: `8799`.
- If `--host 0.0.0.0` is used, print a warning to stderr: "Warning: dashboard is exposed on all network interfaces."
- Resolves `.lattice/` root via `find_root()` before starting. Exits with error if not found.
- **Without `--json`**: Prints: "Lattice dashboard running at http://{host}:{port}/ — press Ctrl+C to stop"
- **With `--json`**: Prints a JSON startup envelope to stdout: `{"ok": true, "data": {"host": "127.0.0.1", "port": 8799, "url": "http://127.0.0.1:8799/"}}` — suppresses the human-readable banner. On startup error (e.g., port in use, no `.lattice/` found), outputs a JSON error envelope and exits non-zero.
- Serves until interrupted (Ctrl+C). Handle `KeyboardInterrupt` cleanly.

### 2. HTTP server (`dashboard/server.py`)

Use **only stdlib** (`http.server`, `json`, `pathlib`, `urllib.parse`). No dependencies.

The server needs access to the `.lattice/` path. Pass it at construction time — don't use global state or `find_root()` inside the server itself (that's the CLI layer's job).

#### Request routing

The server handles two kinds of requests:

1. **API requests** (`/api/...`) — return JSON with the standard envelope: `{"ok": true, "data": ...}`
2. **Static requests** (everything else) — serve `dashboard/static/index.html` for `/`, return 404 for other paths

Use `Content-Type: application/json` for API responses. Use `Content-Type: text/html` for the HTML page.

#### API endpoints

| Endpoint | Returns |
|----------|---------|
| `GET /api/config` | The current `config.json` contents |
| `GET /api/tasks` | Array of compact task snapshots (scan `tasks/*.json`), sorted by task ID (ULID order). Each compact snapshot is augmented with `updated_at` and `created_at` from the full snapshot. |
| `GET /api/tasks/<task_id>` | Enriched task snapshot (see below) |
| `GET /api/tasks/<task_id>/events` | Array of events from the task's JSONL, newest first |
| `GET /api/activity` | Last 50 events from `_global.jsonl`, newest first |
| `GET /api/stats` | Summary object (see below) |
| `GET /api/archived` | Array of compact archived task snapshots (scan `archive/tasks/*.json`), sorted by task ID (ULID order). Augmented with `updated_at` and `created_at` like `/api/tasks`. |

**Enriched task snapshot** (`/api/tasks/<task_id>`):

The server returns the full task snapshot JSON, plus two additional fields it resolves at request time:

- `notes_exists` (bool): `true` if `notes/<task_id>.md` exists on disk, `false` otherwise.
- `artifacts` (array): For each entry in the snapshot's `artifact_refs`, read the corresponding `artifacts/meta/<art_id>.json` and include `{id, title, type}`. If a metadata file is missing or unreadable, include `{id, title: null, type: null}` so the UI can still show the ID.

These fields are NOT stored in the snapshot file — they are computed by the server on each request.

**Stats response shape:**
```json
{
  "total_active": 42,
  "total_archived": 7,
  "by_status": {
    "backlog": 12,
    "ready": 8,
    "in_progress": 10,
    "review": 5,
    "done": 4,
    "blocked": 2,
    "cancelled": 1
  },
  "by_type": {
    "task": 30,
    "bug": 8,
    "epic": 2,
    "spike": 1,
    "chore": 1
  },
  "by_priority": {
    "critical": 2,
    "high": 10,
    "medium": 20,
    "low": 10
  }
}
```

All endpoints read from disk on every request — no caching. This is correct for v0 (low traffic, local only). Scanning `tasks/` is fast up to the v0 scale target of ~10,000 tasks.

If a task ID is not found, return `{"ok": false, "error": {"code": "NOT_FOUND", "message": "Task not found"}}` with HTTP 404.

#### Input validation (path traversal prevention)

All `<task_id>` parameters in URL paths **must** be validated before use in file operations. Task IDs have a well-defined format: `task_` followed by a 26-character ULID. Use a regex check (e.g., `^task_[0-9A-Z]{26}$` — Crockford Base32) or import `validate_id` from `core/ids.py`. Reject invalid IDs with HTTP 400 and a JSON envelope: `{"ok": false, "error": {"code": "INVALID_ID", "message": "Invalid task ID format"}}`.

This is critical when `--host 0.0.0.0` is used, as the server is network-accessible. A naive path join without validation could allow path traversal (e.g., `../../etc/passwd`).

#### Error handling

- Invalid task ID format: 400 with JSON envelope
- Unknown API routes: 404 with JSON envelope
- JSON parse errors reading files: 500 with JSON envelope
- Server errors: log to stderr, return 500

### 3. Static UI (`dashboard/static/index.html`)

A single HTML file with embedded `<style>` and `<script>` — no build step, no external CDN dependencies, no frameworks.

The UI has four views, navigable via tabs or a sidebar:

#### Board view (default)

- One column per status (from config's `workflow.statuses`).
- Each column shows task cards sorted by creation time (ULID order).
- Each card shows: title, priority badge, type badge, assigned-to.
- Clicking a card navigates to the task detail view.
- Column headers show the count of tasks in that status.

#### List view

- Sortable table: ID, title, status, priority, type, assigned-to, updated.
- Filter controls: dropdowns for status, type, priority. Text input for search (filters on title).
- Filters combine with AND.

#### Task detail view

- Full task fields (all snapshot data).
- Outgoing relationships list (with target task title resolved via an extra fetch or preloaded data).
- Artifact references (from the `artifacts` array in the enriched response — show ID, title, and type for each).
- Event timeline: chronological list of events, newest first. Show event type, actor, timestamp, and relevant data (e.g., "status: backlog → ready", "comment: Starting work").
- Notes indicator: if `notes_exists` is `true` in the enriched response, show "Notes: notes/<task_id>.md" (the dashboard can't read the file content, just signals it exists).

#### Activity feed view

- Recent events from the global log.
- Each entry shows: timestamp, event type, task title (resolve from task ID), actor.

#### Styling guidelines

- Clean, minimal design. Light background, readable fonts, adequate spacing.
- Color-code priority levels (e.g., critical=red, high=orange, medium=blue, low=gray).
- Color-code status columns (e.g., done=green, blocked=red, cancelled=gray).
- Responsive enough to work at different window widths but don't obsess over mobile — this is a local developer tool.
- No dark mode needed (but don't make it painful to look at).

#### Data fetching

- On load, fetch `/api/config`, `/api/tasks`, `/api/stats`, `/api/activity`.
- Task detail fetches `/api/tasks/<id>` and `/api/tasks/<id>/events` on demand.
- Add a manual refresh button. No auto-polling needed — the user can click refresh when they want fresh data.
- Show a simple loading indicator while fetches are in flight.

### 4. Package static files

The HTML file must be accessible at runtime. Since `dashboard/static/` is inside the package directory (`src/lattice/dashboard/static/`), it should be included automatically by the hatch build config (`packages = ["src/lattice"]`).

At runtime, resolve the path to `index.html` using:
```python
from pathlib import Path
STATIC_DIR = Path(__file__).parent / "static"
```

This is simpler and more reliable than `importlib.resources` for a directory of files.

## Tests

Dashboard tests should be lightweight — the server is read-only and mostly glue code.

**Server tests** (use stdlib `http.client` or `urllib.request` — start server in a thread):
- Server starts and responds to requests
- `GET /` returns HTML content
- `GET /api/tasks` returns JSON envelope with task array
- `GET /api/tasks/<id>` returns full task for existing task
- `GET /api/tasks/<id>` returns 404 for non-existent task
- `GET /api/tasks/<id>` with invalid ID format returns 400
- `GET /api/tasks/<id>` includes `notes_exists` and `artifacts` fields
- `GET /api/tasks/<id>/events` returns event array
- `GET /api/config` returns config
- `GET /api/stats` returns counts matching actual task data
- `GET /api/activity` returns recent events
- `GET /api/archived` returns archived tasks
- Unknown API route returns 404

**Fixture**: create a populated `.lattice/` with several tasks in different states, a few events, and at least one archived task. Reuse or extend existing test fixtures.

Don't test the HTML/JS rendering (no headless browser needed). The API tests verify the data layer; manual browser testing verifies the UI.

## Conventions

- **No new dependencies.** The server uses only stdlib.
- **Read-only.** The dashboard never writes to `.lattice/`. No POST/PUT/DELETE handlers.
- **JSON envelope.** All API responses use `{"ok": true, "data": ...}` or `{"ok": false, "error": {...}}`.
- **Layer boundaries.** The server reads files from `.lattice/` directly (it's in the storage/read layer). It may import from `core/` for helpers like `compact_snapshot()` but should not import CLI code.

## What NOT to do

- Don't add any dependencies (no Flask, no Jinja, no external CSS/JS)
- Don't implement write endpoints (no POST/PUT/DELETE)
- Don't implement WebSocket or server-sent events
- Don't implement authentication
- Don't implement drag-and-drop for the board view
- Don't implement auto-polling (a manual refresh button is sufficient)
- Don't create multiple HTML files — everything in one `index.html`
- Don't add a build step

## Validation

After implementation:

```bash
uv pip install -e ".[dev]"
uv run pytest -v
uv run ruff check src/ tests/
uv run ruff format --check src/ tests/
```

Then smoke test:

```bash
# Use an existing .lattice/ with data, or create one
uv run lattice init --path /tmp/dash-test
cd /tmp/dash-test
uv run lattice create "First task" --type task --priority high --actor human:atin
uv run lattice create "Second task" --type bug --actor human:atin
TASK_ID=$(uv run lattice list --json | python3 -c "import sys,json; print(json.load(sys.stdin)['data'][0]['id'])")
uv run lattice status "$TASK_ID" ready --actor human:atin

# Start dashboard
uv run lattice dashboard &
DASH_PID=$!
sleep 1

# Verify API
curl -s http://127.0.0.1:8799/api/config | python3 -m json.tool
curl -s http://127.0.0.1:8799/api/tasks | python3 -m json.tool
curl -s http://127.0.0.1:8799/api/stats | python3 -m json.tool
curl -s http://127.0.0.1:8799/ | head -5  # should be HTML

# Open in browser for visual check
open http://127.0.0.1:8799/

kill $DASH_PID
cd -
rm -rf /tmp/dash-test
```

All tests pass. API returns valid JSON envelopes. HTML page loads and renders the board/list views with task data.
