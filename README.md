# Lattice

File-based, agent-native task tracker with an event-sourced core.

## Why Lattice

AI agents lose context between sessions. Plans discussed, decisions made, debugging insights gained -- all vanish when the context window closes. Lattice gives agents (and humans) shared persistent state through the filesystem.

Drop a `.lattice/` directory into any project and every agent that can read a file gets access to task state, event history, and coordination metadata. No database, no server, no authentication ceremony. Events are append-only and immutable, giving you a complete audit trail of every change. It works anywhere git works.

## Quick start

```bash
pip install lattice-tracker
# or
uv pip install lattice-tracker
```

```bash
# Initialize in your project
lattice init --project-code PROJ --actor human:yourname

# Create a task
lattice create "Implement user authentication" --actor human:yourname

# List tasks
lattice list

# Update status
lattice status PROJ-1 in_planning --actor human:yourname

# Add a comment
lattice comment PROJ-1 "Started work on OAuth flow" --actor human:yourname

# Show task details
lattice show PROJ-1

# Assign to someone
lattice assign PROJ-1 agent:claude --actor human:yourname
```

## Core concepts

### Event sourcing

The event log (JSONL files in `.lattice/events/`) is the source of truth. Every mutation -- status changes, assignments, comments, field updates -- becomes an immutable event with a timestamp and actor identity. Task JSON files in `.lattice/tasks/` are materialized snapshots for fast reads.

If snapshots and events ever disagree, events win. `lattice rebuild --all` replays the full event history to regenerate all snapshots.

### Actor attribution

Every write operation requires an `--actor` in `prefix:identifier` format:

- `human:atin` -- a person
- `agent:claude-opus-4` -- an AI agent
- `team:frontend` -- a team or group

### Short IDs

When a project code is configured (e.g., `LAT`), tasks get human-friendly short IDs like `LAT-1`, `LAT-42` alongside their stable ULIDs. All CLI commands accept either format.

### Directory structure

```
.lattice/
├── config.json              # Workflow config, project code, statuses
├── ids.json                 # Short ID index (derived, rebuildable)
├── context.md               # Instance purpose and conventions
├── tasks/                   # Materialized task snapshots (JSON)
├── events/                  # Per-task append-only event logs (JSONL)
│   └── _lifecycle.jsonl     # Aggregated lifecycle events
├── artifacts/               # Attached files and metadata
│   ├── meta/                # Artifact metadata (JSON)
│   └── payload/             # Artifact payloads (binary)
├── notes/                   # Freeform markdown per task
├── archive/                 # Archived tasks (preserves events)
│   ├── tasks/
│   ├── events/
│   └── notes/
└── locks/                   # File locks for concurrency control
```

## CLI reference

### Project setup

| Command | Description |
|---------|-------------|
| `lattice init` | Initialize a new `.lattice/` directory |
| `lattice set-project-code CODE` | Set or change the project code for short IDs |
| `lattice set-subproject-code CODE` | Set a subproject code for hierarchical short IDs |
| `lattice setup-claude` | Add or update Lattice integration block in CLAUDE.md |
| `lattice backfill-ids` | Assign short IDs to existing tasks that lack one |

### Task operations

| Command | Description |
|---------|-------------|
| `lattice create TITLE` | Create a new task |
| `lattice status TASK_ID STATUS` | Change a task's status |
| `lattice update TASK_ID field=value ...` | Update task fields (title, description, priority, etc.) |
| `lattice assign TASK_ID ACTOR` | Assign a task to an actor |
| `lattice comment TASK_ID TEXT` | Add a comment to a task |
| `lattice event TASK_ID TYPE` | Record a custom event (type must start with `x_`) |

### Querying

| Command | Description |
|---------|-------------|
| `lattice list` | List tasks with optional `--status`, `--assigned`, `--tag`, `--type` filters |
| `lattice show TASK_ID` | Show detailed task info including events and relationships |
| `lattice stats` | Project statistics: status/priority/assignee breakdowns, stale tasks |
| `lattice weather` | Daily project digest with health assessment |

### Relationships and artifacts

| Command | Description |
|---------|-------------|
| `lattice link TASK TYPE TARGET` | Create a relationship (`blocks`, `depends_on`, `related_to`, `subtask_of`, `spawned_by`, `duplicate_of`, `supersedes`) |
| `lattice unlink TASK TYPE TARGET` | Remove a relationship |
| `lattice attach TASK SOURCE` | Attach a file or URL as an artifact |

### Maintenance

| Command | Description |
|---------|-------------|
| `lattice archive TASK_ID` | Archive a completed task |
| `lattice unarchive TASK_ID` | Restore an archived task |
| `lattice rebuild [TASK_ID \| --all]` | Rebuild snapshots from event logs |
| `lattice doctor` | Check project integrity (add `--fix` to auto-repair) |
| `lattice dashboard` | Launch a read-only local web UI (default: http://127.0.0.1:8799) |
| `lattice plugins` | List installed Lattice plugins |

### Common flags

All write commands support:

- `--actor` -- identity performing the action (required)
- `--json` -- structured JSON output (`{"ok": true, "data": ...}`)
- `--quiet` -- minimal output (IDs only)
- `--triggered-by` -- provenance: what triggered this action
- `--on-behalf-of` -- provenance: who this action is on behalf of
- `--reason` -- provenance: why this action was taken
- `--model` -- AI model identifier
- `--session` -- session identifier

## Dashboard

`lattice dashboard` starts a local web UI on port 8799. It serves a single-page app from stdlib's `http.server` with no build step or external dependencies. By default, the dashboard is read-only; write operations are only available on loopback addresses.

## Agent integration

### Claude Code (CLAUDE.md)

Run `lattice setup-claude` in your project root to add a Lattice integration block to CLAUDE.md. This teaches Claude Code agents to create tasks before starting work, update status at transitions, and leave comments as breadcrumbs for future sessions.

```bash
lattice setup-claude           # Add to existing CLAUDE.md
lattice setup-claude --force   # Replace existing block with latest template
```

### MCP server

Lattice ships an MCP (Model Context Protocol) server for direct tool-call integration with AI agents:

```bash
pip install lattice-tracker[mcp]

# Run the MCP server (stdio transport)
lattice-mcp
```

This exposes Lattice operations as MCP tools and resources that agents can call without going through the CLI.

### Hooks and plugins

Lattice supports two extension mechanisms:

- **Shell hooks** -- fire commands on events via `config.json` (`hooks.post_event` catch-all, `hooks.on.<type>` per-event-type triggers).
- **Entry-point plugins** -- extend the CLI and `setup-claude` template blocks via `importlib.metadata` entry points (`lattice.cli_plugins`, `lattice.template_blocks`).

Run `lattice plugins` to list installed plugins.

## Development

```bash
git clone https://github.com/stage11-agentics/lattice.git
cd lattice

# Create venv and install in dev mode
uv venv
uv pip install -e ".[dev]"

# Run tests
uv run pytest

# Lint and format
uv run ruff check src/ tests/
uv run ruff format src/ tests/

# Type check
uv run mypy
```

**Requirements:** Python 3.12+

**Runtime dependencies:** `click`, `python-ulid`, `filelock` -- deliberately minimal.

**Optional:** `mcp` (for MCP server support)

## Status

Lattice is **v0.1.0, alpha quality, actively developed.** The on-disk format and event schema are stabilizing but not yet frozen. Expect breaking changes before v1.

## License

[MIT](LICENSE)

---

Built by [Stage 11 Agentics](https://stage11agentics.com) -- autonomous agent teams.
