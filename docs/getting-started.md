# Getting Started with Lattice

This guide walks you through initializing Lattice, creating and managing tasks, and setting up integrations with AI coding tools. By the end you will have a working Lattice instance tracking tasks in your project.

## Prerequisites

- Python 3.12 or later
- `pip` or `uv` for package installation

## Install Lattice

```bash
pip install lattice-tracker
# or with uv
uv pip install lattice-tracker
```

To include the MCP server (for AI agent integration):

```bash
pip install lattice-tracker[mcp]
```

Verify the installation:

```bash
lattice --help
```

## Initialize Lattice in your project

Navigate to your project directory and run:

```bash
cd /path/to/your/project
lattice init
```

The init command will prompt you for:

1. **Default actor identity** -- your identity for tracking who made changes. Format: `prefix:identifier`. Examples: `human:alice`, `human:atin`. Leave blank to skip.
2. **Project code** -- a short prefix for human-friendly task IDs. For example, entering `MYP` gives you IDs like `MYP-1`, `MYP-2`. Leave blank to use only ULIDs.

You can also provide these non-interactively:

```bash
lattice init --actor human:alice --project-code MYP
```

This creates a `.lattice/` directory in your project:

```
.lattice/
├── config.json          # Workflow configuration, project code, statuses
├── ids.json             # Short ID index (derived, rebuildable)
├── tasks/               # Materialized task snapshots (JSON)
├── events/              # Per-task append-only event logs (JSONL)
├── artifacts/           # Attached files and metadata
│   ├── meta/
│   └── payload/
├── notes/               # Freeform markdown notes per task
├── archive/             # Archived tasks (events preserved)
│   ├── tasks/
│   ├── events/
│   └── notes/
└── locks/               # Concurrency control
```

The `.lattice/` directory is committed to the repo by default. Task state lives alongside your code -- versioned, visible to CI, and accessible to every tool and collaborator. The event logs are append-only JSONL files that merge cleanly, and snapshots are deterministic JSON that can be rebuilt from events at any time.

Use a `.lattice/.gitignore` to exclude transient files (lock files are already excluded by the lock implementation). For sensitive artifact payloads, use the `--sensitive` flag on `lattice attach`.

## Create your first task

```bash
lattice create "Set up project structure" --actor human:alice
```

Output:

```
Created MYP-1: Set up project structure
```

The task starts in `backlog` status by default. Lattice also creates a notes file at `.lattice/notes/<task_ulid>.md` where you can write implementation plans and working notes.

### Task properties

You can set additional properties at creation time:

```bash
lattice create "Fix login redirect bug" \
  --actor human:alice \
  --type bug \
  --priority high \
  --description "Users get a 404 after OAuth redirect" \
  --tags "auth,urgent" \
  --assigned-to human:alice
```

**Types:** `task`, `epic`, `bug`, `spike`, `chore`, `ticket`
**Priorities:** `critical`, `high`, `medium`, `low`

## Move through the workflow

Lattice enforces a workflow with defined status transitions:

```
backlog -> in_planning -> planned -> in_progress -> review -> done
```

Move your task through the pipeline:

```bash
# Start planning
lattice status MYP-1 in_planning --actor human:alice

# Planning complete
lattice status MYP-1 planned --actor human:alice

# Begin implementation
lattice status MYP-1 in_progress --actor human:alice

# Ready for review
lattice status MYP-1 review --actor human:alice

# Approved, mark done
lattice status MYP-1 done --actor human:alice
```

Each status change is recorded as an immutable event with a timestamp and actor.

### Forced transitions

If you need to skip steps (e.g., moving directly from `backlog` to `in_progress`), use `--force` with a `--reason`:

```bash
lattice status MYP-1 in_progress --force --reason "Urgent fix, skipping planning" --actor human:alice
```

### Special statuses

- **`blocked`** -- a task that cannot proceed. Can return to `in_planning`, `planned`, `in_progress`, or `cancelled`.
- **`cancelled`** -- terminal. No transitions out.
- **`done`** -- terminal. No transitions out.

## Understanding events and snapshots

Lattice is event-sourced. Every change creates an immutable event in `.lattice/events/<task_id>.jsonl`:

```json
{"id":"ev_01HQ...","type":"task_created","task_id":"task_01HQ...","actor":"human:alice","ts":"2026-02-16T10:00:00Z","data":{"title":"Set up project structure","status":"backlog",...}}
{"id":"ev_01HQ...","type":"status_changed","task_id":"task_01HQ...","actor":"human:alice","ts":"2026-02-16T10:05:00Z","data":{"from":"backlog","to":"in_planning"}}
```

Task JSON files in `.lattice/tasks/` are **materialized snapshots** -- derived from events for fast reads. If snapshots ever get out of sync:

```bash
# Rebuild a single task
lattice rebuild MYP-1

# Rebuild everything
lattice rebuild --all
```

This replays all events and regenerates snapshots. Events are the source of truth; snapshots are a cache.

## List and inspect tasks

```bash
# List all active tasks
lattice list

# Filter by status
lattice list --status in_progress

# Filter by assignee
lattice list --assigned human:alice

# Filter by tag
lattice list --tag auth

# JSON output for programmatic use
lattice list --json
```

Show full details for a task:

```bash
lattice show MYP-1
```

This displays the task snapshot, event history, relationships, artifacts, and notes path.

## Add comments

Leave context for yourself or future agents:

```bash
lattice comment MYP-1 "Decided to use JWT instead of session cookies. See notes for tradeoff analysis." --actor human:alice
```

Comments are events -- immutable and timestamped.

## Work with relationships

Link tasks together to express dependencies and structure:

```bash
# Create an epic and subtasks
EPIC=$(lattice create "Implement auth module" --type epic --actor human:alice --quiet)
SUB1=$(lattice create "Set up OAuth provider" --actor human:alice --quiet)
SUB2=$(lattice create "Build token refresh" --actor human:alice --quiet)

# Link subtasks to epic
lattice link $SUB1 subtask_of $EPIC --actor human:alice
lattice link $SUB2 subtask_of $EPIC --actor human:alice

# Express dependency
lattice link $SUB2 depends_on $SUB1 --actor human:alice --note "Token refresh needs OAuth config first"
```

Relationship types: `blocks`, `depends_on`, `subtask_of`, `related_to`, `spawned_by`, `duplicate_of`, `supersedes`.

## Use the dashboard

Lattice includes a local web UI for visual task management:

```bash
lattice dashboard
# Serving at http://127.0.0.1:8799/
```

The dashboard provides:

- **Board view** -- Kanban-style columns per status. Drag and drop to change status.
- **List view** -- Filterable table with search, status/priority/type filters.
- **Activity view** -- Recent events across all tasks.
- **Task detail** -- Click any task for full detail including description, relationships, artifacts, and event timeline.

When bound to localhost (the default), the dashboard supports full write operations: creating tasks, changing status, assigning, commenting, and archiving. When exposed to the network, it runs in read-only mode.

### Dashboard settings

- Set a background image URL in settings
- Customize lane colors by clicking the color dot on column headers
- Auto-refreshes every 10 seconds

## Set up CLAUDE.md integration

If you use Claude Code, add the Lattice integration block to your project's `CLAUDE.md`:

```bash
lattice setup-claude
```

This appends a block that teaches Claude agents to:
- Create a task before starting any work
- Update status at every transition
- Attribute actions to the correct actor
- Leave breadcrumbs via comments and notes

To update an existing block to the latest template:

```bash
lattice setup-claude --force
```

See [integration-claude-code.md](integration-claude-code.md) for full details.

## Set up the MCP server

For AI agents that support MCP (Model Context Protocol), Lattice exposes its operations as MCP tools:

```bash
# Run the MCP server
lattice-mcp
```

Configure it in Claude Code settings:

```json
{
  "mcpServers": {
    "lattice": {
      "command": "lattice-mcp"
    }
  }
}
```

See [integration-mcp.md](integration-mcp.md) for the full list of MCP tools and resources.

## Check project health

Run diagnostics to verify your Lattice instance:

```bash
lattice doctor
```

This checks for missing directories, orphaned event logs, invalid JSON, and snapshot/event mismatches. Use `--fix` to auto-repair what it can:

```bash
lattice doctor --fix
```

## Archive completed tasks

When a task is done and you want to clean up the active list:

```bash
lattice archive MYP-1 --actor human:alice
```

Archived tasks move to `.lattice/archive/` but remain queryable via `lattice show`. To bring one back:

```bash
lattice unarchive MYP-1 --actor human:alice
```

## Quick reference

| Action | Command |
|--------|---------|
| Initialize | `lattice init [--actor A] [--project-code CODE]` |
| Create task | `lattice create "Title" --actor A` |
| Change status | `lattice status ID STATUS --actor A` |
| Assign | `lattice assign ID ASSIGNEE --actor A` |
| Comment | `lattice comment ID "text" --actor A` |
| List tasks | `lattice list [--status S] [--assigned A]` |
| Show task | `lattice show ID` |
| Link tasks | `lattice link SRC TYPE TGT --actor A` |
| Attach file | `lattice attach ID path --actor A` |
| Archive | `lattice archive ID --actor A` |
| Health check | `lattice doctor [--fix]` |
| Rebuild | `lattice rebuild --all` |
| Dashboard | `lattice dashboard` |
| CLAUDE.md setup | `lattice setup-claude [--force]` |
| OpenClaw setup | `lattice setup-openclaw [--global] [--force]` |

## Next steps

- Read [integration-claude-code.md](integration-claude-code.md) for Claude Code integration details
- Read [integration-openclaw.md](integration-openclaw.md) for OpenClaw skill and MCP configuration
- Read [integration-mcp.md](integration-mcp.md) for MCP server configuration and tool reference
- Read [integration-codex.md](integration-codex.md) for Codex CLI workflows
- Read [integration-ci.md](integration-ci.md) for CI/CD pipeline integration patterns
- Explore the full CLI reference with `lattice --help` or the `/lattice` command
