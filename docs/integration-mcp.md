# Using the Lattice MCP Server

Lattice ships an MCP (Model Context Protocol) server that exposes task operations as tools and resources. This lets AI agents interact with Lattice through structured tool calls instead of shelling out to the CLI.

## Installation

The MCP server requires the `mcp` optional dependency:

```bash
pip install lattice-tracker[mcp]
# or
uv pip install lattice-tracker[mcp]
```

## Configuring in Claude Code

Add the Lattice MCP server to your Claude Code settings. In your project's `.claude/settings.json` or `~/.claude/settings.json`:

```json
{
  "mcpServers": {
    "lattice": {
      "command": "lattice-mcp",
      "args": [],
      "env": {}
    }
  }
}
```

If you installed Lattice in a virtual environment, use the full path to the `lattice-mcp` binary:

```json
{
  "mcpServers": {
    "lattice": {
      "command": "/path/to/your/venv/bin/lattice-mcp",
      "args": [],
      "env": {}
    }
  }
}
```

Alternatively, with `uv`:

```json
{
  "mcpServers": {
    "lattice": {
      "command": "uv",
      "args": ["run", "lattice-mcp"],
      "env": {}
    }
  }
}
```

The server runs over stdio transport and auto-discovers the `.lattice/` directory by walking up from the working directory (same as the CLI).

## Setting the lattice root

Most tools accept an optional `lattice_root` parameter -- a path to the project directory containing `.lattice/`. If omitted, the server finds the root by walking up from cwd. If your MCP server starts in a directory without a `.lattice/` ancestor, pass `lattice_root` explicitly on each call.

## Available MCP tools

### Write tools

These tools modify Lattice state. All require an `actor` parameter.

#### `lattice_create`

Create a new task.

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `title` | string | yes | Task title |
| `actor` | string | yes | Actor ID (e.g., `agent:claude-opus-4`) |
| `task_type` | string | no | Task type (default: `task`) |
| `priority` | string | no | Priority level (default: `medium`) |
| `status` | string | no | Initial status (default: from config) |
| `description` | string | no | Task description |
| `tags` | string | no | Comma-separated tags |
| `assigned_to` | string | no | Assignee actor ID |
| `task_id` | string | no | Caller-supplied ID for idempotency |
| `lattice_root` | string | no | Project directory path |

Returns the task snapshot.

#### `lattice_criterion_add`

Add an optional task-local acceptance criterion. Parameters are `task_id`,
`outcome`, `actor`, optional explicit `criterion_id`, and optional
`lattice_root`. Automatic IDs use the next exact `AC-N` value.

#### `lattice_criterion_edit`

Revise an active criterion while preserving immutable history. Parameters are
`task_id`, `criterion_id`, `outcome`, `actor`, and optional `lattice_root`.
A normalized no-op is idempotent.

#### `lattice_criterion_retire`

Retire an active criterion without deleting its history. Parameters are
`task_id`, `criterion_id`, `actor`, and optional `lattice_root`.

#### `lattice_update`

Update task fields.

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `task_id` | string | yes | Task ID (ULID or short ID) |
| `actor` | string | yes | Actor ID |
| `fields` | dict | yes | Field-value pairs to update |
| `lattice_root` | string | no | Project directory path |

Updatable fields: `title`, `description`, `priority`, `urgency`, `type`, `tags`. Use `custom_fields.<key>` for custom data. Do not use this for status changes or assignments -- use `lattice_status` and `lattice_assign` instead.

#### `lattice_status`

Change a task's status.

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `task_id` | string | yes | Task ID |
| `new_status` | string | yes | Target status |
| `actor` | string | yes | Actor ID |
| `force` | bool | no | Force invalid transition (default: false) |
| `reason` | string | no | Reason for forced transition |
| `lattice_root` | string | no | Project directory path |

Enforces workflow transitions. Set `force=true` with a `reason` to override.

#### `lattice_assign`

Assign a task to an actor.

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `task_id` | string | yes | Task ID |
| `assignee` | string | yes | Assignee actor ID |
| `actor` | string | yes | Actor performing the assignment |
| `lattice_root` | string | no | Project directory path |

#### `lattice_comment`

Add a comment to a task.

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `task_id` | string | yes | Task ID |
| `text` | string | yes | Comment text |
| `actor` | string | yes | Actor ID |
| `criterion_ids` | list[string] | no | Existing task-local criteria to link |
| `lattice_root` | string | no | Project directory path |

#### `lattice_link`

Create a relationship between two tasks.

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `source_id` | string | yes | Source task ID |
| `relationship_type` | string | yes | Relationship type |
| `target_id` | string | yes | Target task ID |
| `actor` | string | yes | Actor ID |
| `note` | string | no | Optional note |
| `lattice_root` | string | no | Project directory path |

Relationship types: `blocks`, `depends_on`, `subtask_of`, `related_to`, `spawned_by`, `duplicate_of`, `supersedes`.

#### `lattice_unlink`

Remove a relationship between two tasks.

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `source_id` | string | yes | Source task ID |
| `relationship_type` | string | yes | Relationship type to remove |
| `target_id` | string | yes | Target task ID |
| `actor` | string | yes | Actor ID |
| `lattice_root` | string | no | Project directory path |

#### `lattice_attach`

Attach a file or URL to a task as an artifact.

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `task_id` | string | yes | Task ID |
| `source` | string | yes | File path or URL |
| `actor` | string | yes | Actor ID |
| `title` | string | no | Artifact title |
| `art_type` | string | no | Artifact type (`file`, `reference`, `conversation`, `prompt`, `log`) |
| `summary` | string | no | Short summary |
| `role` | string | no | Optional evidence role |
| `criterion_ids` | list[string] | no | Existing task-local criteria to link |
| `artifact_id` | string | no | Caller-supplied ID for idempotent retry |
| `lattice_root` | string | no | Project directory path |

#### `lattice_archive`

Archive a task. Moves snapshot, events, and notes to `archive/`.

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `task_id` | string | yes | Task ID |
| `actor` | string | yes | Actor ID |
| `lattice_root` | string | no | Project directory path |

#### `lattice_unarchive`

Restore an archived task to active status.

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `task_id` | string | yes | Task ID |
| `actor` | string | yes | Actor ID |
| `lattice_root` | string | no | Project directory path |

#### `lattice_event`

Record a custom event on a task. The event type must start with `x_`.

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `task_id` | string | yes | Task ID |
| `event_type` | string | yes | Custom event type (must start with `x_`) |
| `actor` | string | yes | Actor ID |
| `data` | dict | no | Optional event data |
| `lattice_root` | string | no | Project directory path |

### Read tools

These tools are read-only and do not require an `actor` parameter.

#### `lattice_criteria`

List criteria for an active or archived task. Parameters are `task_id`,
`include_retired` (default false), `include_history` (default false), and
optional `lattice_root`. The result includes an `archived` marker.

#### `lattice_list`

List active tasks with optional filters.

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `status` | string | no | Filter by status |
| `assigned` | string | no | Filter by assignee |
| `tag` | string | no | Filter by tag |
| `task_type` | string | no | Filter by task type |
| `priority` | string | no | Filter by priority |
| `lattice_root` | string | no | Project directory path |

Returns a list of task snapshots.

#### `lattice_show`

Show detailed task information including event history.

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `task_id` | string | yes | Task ID |
| `include_events` | bool | no | Include event history (default: true) |
| `lattice_root` | string | no | Project directory path |

#### `lattice_config`

Read the Lattice project configuration.

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `lattice_root` | string | no | Project directory path |

#### `lattice_doctor`

Check Lattice data integrity.

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `fix` | bool | no | Attempt to fix issues (default: false) |
| `lattice_root` | string | no | Project directory path |

Returns a diagnostic report with issue counts, severity levels, and task/archive counts.

Criterion mutations reject archived tasks. `lattice_show` and
`lattice://tasks/{task_id}` expose the full criterion materialized view for
active or archived tasks. `criterion_ids` on comments and artifacts are
task-local traceability links only: they do not imply pass/fail, satisfaction,
or a completion-policy result.

## Available MCP resources

Resources are auto-surfaced read-only data. Agents can access these without making explicit tool calls.

| URI | Description |
|-----|-------------|
| `lattice://tasks` | All active task snapshots as JSON |
| `lattice://tasks/{task_id}` | Full task detail with events |
| `lattice://tasks/status/{status}` | Tasks filtered by status |
| `lattice://tasks/assigned/{actor}` | Tasks filtered by assignee |
| `lattice://config` | Project configuration |
| `lattice://notes/{task_id}` | Task notes markdown content |

Resources accept both ULIDs and short IDs (e.g., `lattice://tasks/LAT-42`).

## MCP vs CLI

Both interfaces access the same `.lattice/` data. The MCP server uses the same core logic as the CLI -- events are written identically regardless of which interface creates them.

| Consideration | CLI | MCP |
|---------------|-----|-----|
| Best for | Shell scripts, manual use, CI/CD | AI agent tool calls |
| Output | Text, JSON, quiet modes | Structured return values |
| Discovery | `lattice --help` | MCP tool/resource listing |
| Root discovery | Walks up from cwd | Walks up from cwd, or `lattice_root` param |

Use the CLI for human workflows and shell scripting. Use MCP when agents need to call Lattice operations as structured tools without spawning subprocesses.

## Example: agent workflow via MCP

An agent using Lattice through MCP might execute this sequence of tool calls:

```
1. lattice_create(title="Fix login redirect", actor="agent:claude-opus-4", task_type="bug", priority="high")
   -> returns snapshot with task ID

2. lattice_status(task_id="LAT-15", new_status="in_progress", actor="agent:claude-opus-4", force=true, reason="Skipping planning for urgent bug")
   -> returns updated snapshot

3. lattice_comment(task_id="LAT-15", text="Root cause: redirect URL not URL-encoded", actor="agent:claude-opus-4")
   -> returns updated snapshot

4. lattice_criterion_add(task_id="LAT-15", outcome="Login returns to the requested page", actor="agent:claude-opus-4")
   -> returns criterion AC-1 and updated snapshot

5. lattice_comment(task_id="LAT-15", text="Observed the redirect in the running app", criterion_ids=["AC-1"], actor="agent:claude-opus-4")
   -> returns updated snapshot with a traceability link

6. lattice_status(task_id="LAT-15", new_status="review", actor="agent:claude-opus-4")
   -> returns updated snapshot
```

Each call returns the updated task snapshot, so the agent always has the current state.
