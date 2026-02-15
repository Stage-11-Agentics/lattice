# Lattice

Fractal Agentics' file-based, agent-native task tracker with an event-sourced core.

## Disambiguation: "Lattice" the Codebase vs. "Lattice" the Instance

This project **dogfoods itself**. There are two distinct things called "Lattice" in this directory:

1. **The Lattice source code** — the Python project under `src/lattice/` that you build, test, and modify. This is what `git` tracks.
2. **The `.lattice/` data directory** — a live Lattice instance initialized in this repo for tracking development tasks. This is `.gitignore`d runtime state, not source code.

When someone says "is Lattice set up?" they could mean either. Clarify which:
- **"Is the dev environment set up?"** → Can you run `uv run lattice --help`? Are deps installed?
- **"Is Lattice tracking tasks here?"** → Does `.lattice/` exist with a `config.json`? (Yes — it's initialized and ready for use.)

**Rule:** Never confuse changes to `src/lattice/` (source code) with changes to `.lattice/` (instance data). They are independent. Editing source code does not affect the running instance until you reinstall (`uv pip install -e ".[dev]"`).

## Quick Reference

| Item | Value |
|------|-------|
| Language | Python 3.12+ |
| CLI framework | Click |
| Testing | pytest |
| Linting | ruff |
| Package manager | uv |
| Entry point | `lattice` (via `[project.scripts]`) |
| On-disk root | `.lattice/` in any project directory |

## Key Documents

| Document | Purpose |
|----------|---------|
| `ProjectRequirements_v1.md` | Full specification — object model, schemas, CLI commands, invariants |
| `Decisions.md` | Architectural decisions with rationale (append-only log) |

**Read `ProjectRequirements_v1.md` before making any architectural change.** It defines system invariants that must not be violated.

## Architecture

### Core Principle: Events are Authoritative

The event log (JSONL) is the source of truth. Task JSON files are materialized snapshots for fast reads. If they disagree, events win. `lattice rebuild` replays events to regenerate snapshots.

### On-Disk Layout (`.lattice/`)

```
.lattice/
├── config.json                    # Workflow, statuses, transitions, WIP limits, project_code
├── ids.json                       # Derived short ID index (short_id -> ULID mapping + next_seq)
├── tasks/<task_id>.json           # Materialized task snapshots
├── events/<task_id>.jsonl         # Per-task event logs (append-only)
├── events/_lifecycle.jsonl         # Lifecycle event log (derived, rebuildable from per-task logs)
├── artifacts/meta/<art_id>.json   # Artifact metadata
├── artifacts/payload/<art_id>.*   # Artifact payloads
├── notes/<task_id>.md             # Human-editable markdown notes (non-authoritative)
├── archive/                       # Mirrors tasks/events/notes for archived items
│   ├── tasks/
│   ├── events/
│   └── notes/
└── locks/                         # Internal lock files for concurrency
```

### Short IDs

Tasks can have human-friendly short IDs (e.g., `LAT-42`) when a `project_code` is configured. Short IDs are aliases — all CLI commands accept both ULID (`task_01...`) and short ID inputs. Resolution happens at the CLI layer via `resolve_task_id()` in `cli/helpers.py`. The `ids.json` index maps short IDs to ULIDs and is derived (rebuildable via `lattice rebuild --all`).

### Write Path

The CLI is the **only** write interface for authoritative state. All writes are:
- **Event-first** (append event, then materialize snapshot — crash between the two is recoverable via `rebuild`)
- **Lock-protected** (file locks in `.lattice/locks/`)
- **Atomic** (write temp file, fsync, rename for snapshots; lock + append + flush for events)

Multi-lock operations acquire locks in deterministic (sorted) order to prevent deadlocks.

Notes (`notes/<task_id>.md`) are an explicit exception — they are non-authoritative supplementary files edited directly by humans or agents.

### Root Discovery

The CLI finds `.lattice/` by walking up from cwd (like `git` finds `.git/`). Override with `LATTICE_ROOT` env var. Commands other than `lattice init` error if no `.lattice/` is found.

### Identifiers

All entities use ULIDs with type prefixes:
- `task_01HQ...` — tasks
- `ev_01HQ...` — events
- `art_01HQ...` — artifacts

IDs are stable and never change. The CLI supports `--id` for caller-supplied IDs (idempotent retries). Same ID + same payload = success. Same ID + different payload = conflict error.

### Actor IDs

Free-form `prefix:identifier` strings. No registry in v0.
- `agent:claude-opus-4`, `agent:codex`, `agent:session-abc123`
- `human:atin`, `human:joe`
- `team:frontend`

Validation: format only (must have prefix + colon + non-empty id).

## Project Structure

```
lattice/
├── CLAUDE.md
├── Decisions.md
├── ProjectRequirements_v1.md
├── pyproject.toml
├── src/
│   └── lattice/
│       ├── __init__.py
│       ├── cli/                  # Click command groups
│       │   ├── __init__.py
│       │   └── main.py           # CLI entry point
│       ├── core/                 # Business logic (no I/O assumptions)
│       │   ├── __init__.py
│       │   ├── config.py         # Config loading and validation
│       │   ├── events.py         # Event creation, schema, types
│       │   ├── tasks.py          # Task CRUD, snapshot materialization
│       │   ├── artifacts.py      # Artifact metadata and linkage
│       │   ├── relationships.py  # Relationship types and validation
│       │   └── ids.py            # ULID generation and validation
│       ├── storage/              # Filesystem operations
│       │   ├── __init__.py
│       │   ├── fs.py             # Atomic writes, directory management
│       │   └── locks.py          # File locking, deterministic ordering
│       └── dashboard/            # Read-only local web UI
│           ├── __init__.py
│           ├── server.py         # HTTP server (stdlib)
│           └── static/           # Single HTML/JS page (no build step)
└── tests/
    ├── conftest.py               # Shared fixtures (tmp .lattice/ dirs, etc.)
    ├── test_cli/
    ├── test_core/
    └── test_storage/
```

### Layer Boundaries

- **`core/`** contains pure business logic. No filesystem calls. Receives and returns data structures.
- **`storage/`** handles all filesystem I/O. Atomic writes, locking, directory traversal.
- **`cli/`** wires core + storage together via Click commands. Handles output formatting.
- **`dashboard/`** is read-only. Reads `.lattice/` files, serves JSON endpoints + static HTML.

This separation exists so that `core/` can be tested without touching the filesystem, and `storage/` can be tested with temp directories.

## Development Setup

```bash
# Clone and enter
cd lattice

# Create venv and install in dev mode
uv venv
uv pip install -e ".[dev]"

# Run tests
uv run pytest

# Run linter
uv run ruff check src/ tests/
uv run ruff format src/ tests/

# Run the CLI
uv run lattice --help
```

## Dependencies

### Runtime
- `click` — CLI framework
- `python-ulid` — ULID generation
- `filelock` — Cross-platform file locking

### Dev
- `pytest` — testing
- `ruff` — linting and formatting

Minimize dependencies. The dashboard uses only stdlib (`http.server`, `json`). Do not add dependencies without justification.

## Coding Conventions

### JSON Output

All JSON written to `.lattice/` must be:
- Sorted keys
- 2-space indentation
- Trailing newline
- Deterministic (for clean git diffs)

```python
json.dumps(data, sort_keys=True, indent=2) + "\n"
```

### Event Appends

Events are single JSONL lines. Append with lock held, flush immediately.

```python
json.dumps(event, sort_keys=True, separators=(",", ":")) + "\n"
```

Note: JSONL uses compact separators (no spaces) to keep lines short.

### Error Handling

- CLI commands should print human-readable errors to stderr and exit with non-zero codes.
- `--json` mode uses a structured envelope: `{"ok": true, "data": ...}` or `{"ok": false, "error": {"code": "...", "message": "..."}}`.
- Never silently swallow errors. If a write fails, the user must know.

### Testing

- Every CLI command gets integration tests (invoke Click commands, check `.lattice/` state).
- Every core module gets unit tests (pure logic, no filesystem).
- Storage gets tests with real temp directories.
- Use `tmp_path` fixture for isolated `.lattice/` directories in tests.

Critical test categories (add as features land):
- **Concurrent write safety:** Multiple threads/processes writing to the same task simultaneously must not corrupt files.
- **Crash recovery:** Simulate crash between event-write and snapshot-write; verify `rebuild` recovers correctly.
- **Rebuild determinism:** `rebuild` from events must produce byte-identical snapshots regardless of run order.
- **Idempotency conflicts:** Same ID with different payload must error, not silently overwrite.

## Where Things Live

- **Task plans, research, working docs** → `.lattice/notes/<task_id>.md` — tied to the task, lives where Lattice expects it. This is the default home for any document associated with a specific task (implementation plans, spike findings, etc.).
- **Repo-level `notes/`** — only for things NOT tied to a specific task (code reviews, general research, retrospectives).
- **Don't duplicate** — a plan should live in one place, not both.

## Agent Task Discipline

**This project dogfoods Lattice for its own task tracking.** If you are working on a Lattice task (LAT-*), you are expected to keep its status current. This is not optional bookkeeping — it is how the team (humans and agents) knows what's happening.

### Status Lifecycle

Update status at every transition. Do not batch updates or defer them to the end.

| When this happens... | ...you must do this |
|---|---|
| You start planning a task | `lattice status LAT-N in_planning --actor agent:<your-id>` |
| Planning is complete, ready for implementation | `lattice status LAT-N planned --actor agent:<your-id>` |
| You begin implementation | `lattice status LAT-N in_progress --actor agent:<your-id>` |
| Work is blocked | `lattice status LAT-N blocked --actor agent:<your-id>` + comment explaining why |
| Implementation is complete, ready for review | `lattice status LAT-N review --actor agent:<your-id>` |
| Task is verified done | `lattice status LAT-N done --actor agent:<your-id>` |

### Notes Files

Every task has a notes file at `.lattice/notes/<task_id>.md` (auto-created on `lattice create`). Use it as the working document — write plans, record decisions, document open questions. The notes file has two sections:

- **Summary** — human-readable description of what and why
- **Technical Plan** — implementation approach, design decisions, open questions

Update the notes file as your understanding evolves. It is the canonical place for task-level context that outlives a single agent session.

### Assignment

If you are picking up a task, assign yourself: `lattice assign LAT-N agent:<your-id> --actor agent:<your-id>`

### Comments for Context

Use `lattice comment` to leave breadcrumbs that other agents or humans will need:
- Why you chose an approach
- What you tried that didn't work
- What's left to do if you're handing off

## Workflow Reminders

- **Branch naming:** `feat/`, `fix/`, `refactor/`, `test/`, `chore/` prefixes
- **Commits:** Conventional commit messages (`feat:`, `fix:`, etc.)
- **Before merging:** All tests pass, ruff clean, no regressions
- **New decisions:** Append to `Decisions.md` with date, decision, rationale, consequence
- **Schema changes:** Bump `schema_version`, maintain forward compatibility (unknown fields tolerated)

## What Not to Build (v0)

Refer to `ProjectRequirements_v1.md` for full non-goals. Key reminders:
- No agent registry (actor IDs are free-form strings)
- No `lattice note` command (notes are direct file edits)
- ~~No `lattice unarchive`~~ — `lattice unarchive` is now implemented
- No database or index (filesystem scanning is sufficient at v0 scale)
- No real-time dashboard updates
- No authentication or multi-user access control
- No CI/CD integration, alerting, or process management
