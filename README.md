# Lattice

File-based, agent-native task tracker with an event-sourced core.

## Why Lattice exists

AI agents forget. Every session starts fresh -- plans discussed, debugging insights gained, architectural decisions made all vanish when the context window closes. Your agents have intelligence without memory. Capability without coordination.

Lattice gives agents shared persistent state through files they can already read. Drop a `.lattice/` directory into your project and suddenly every agent that can read a file -- and they all can -- gets access to what happened before it arrived and what needs to happen next. No database, no server, no setup beyond `lattice init`.

## Quick start

```bash
pip install lattice-tracker
# or
uv pip install lattice-tracker
```

```bash
# Initialize in your project directory
lattice init

# Create a task
lattice create "My first task" --actor human:me

# See what's there
lattice list

# Update status
lattice status LAT-1 in_progress --actor human:me
```

## Key features

- **Event-sourced** -- append-only event log is the source of truth. Task snapshots are materialized projections, rebuildable at any time.
- **File-based** -- `.lattice/` directory sits next to your code like `.git/`. Git-friendly, no external services.
- **Agent-native** -- any tool that reads files can participate. No special SDK, no API client, no authentication ceremony.
- **CLI-first** -- every operation available from the command line with `--json` output for programmatic use.
- **MCP server included** -- expose Lattice operations to AI agents via the Model Context Protocol.
- **Local dashboard** -- read-only web UI for human visibility into task state.
- **Short IDs** -- human-friendly identifiers like `LAT-42` alongside stable ULIDs.
- **Relationships and dependencies** -- `blocks`, `blocked_by`, `relates_to`, `parent` links between tasks.
- **Provenance tracking** -- optional deep attribution recording who triggered an action, on whose behalf, and why.
- **Plugin system** -- extend Lattice with custom event handlers and integrations.

## Dashboard

![Dashboard](docs/images/dashboard.png)

*(screenshot coming soon)*

## Architecture in brief

The event log (JSONL files in `.lattice/events/`) is authoritative. Every change -- status transitions, assignments, comments -- becomes an immutable event with a timestamp and actor. Task JSON files in `.lattice/tasks/` are materialized snapshots for fast reads.

If snapshots and events disagree, events win. `lattice rebuild` replays the full event history to regenerate all snapshots from scratch.

```
.lattice/
├── config.json              # Workflow configuration
├── ids.json                 # Short ID index (derived, rebuildable)
├── tasks/                   # Materialized task snapshots
├── events/                  # Per-task append-only event logs
├── artifacts/               # Attached files and metadata
├── notes/                   # Freeform markdown (non-authoritative)
├── archive/                 # Archived tasks (events preserved)
└── locks/                   # Concurrency control
```

For the full architecture, see [CLAUDE.md](CLAUDE.md) or [Philosophy](Philosophy_v2.md).

## MCP Server

Lattice ships an MCP server as a separate entry point for AI agent integration:

```bash
pip install lattice-tracker[mcp]

# Run the MCP server
lattice-mcp
```

This exposes Lattice operations (create, status, list, comment, etc.) as MCP tools that agents can call directly.

## Development

```bash
git clone https://github.com/fractal-agentics/lattice.git
cd lattice

# Create venv and install in dev mode
uv venv
uv pip install -e ".[dev]"

# Run tests
uv run pytest

# Lint and format
uv run ruff check src/ tests/
uv run ruff format src/ tests/
```

**Requirements:** Python 3.12+

**Runtime dependencies:** `click`, `python-ulid`, `filelock` -- deliberately minimal.

## Status

Lattice is **v0.1.0, alpha quality, actively developed.** The on-disk format and event schema are stabilizing but not yet frozen. Expect breaking changes before v1.

## License

[MIT](LICENSE)

## Built by

Built by [Fractal Agentics](https://fractalagentics.com) -- autonomous agent teams.
