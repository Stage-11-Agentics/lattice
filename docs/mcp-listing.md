# Lattice MCP Server -- Directory Listing Materials

Prepared descriptions, tool inventories, and submission metadata for listing Lattice's MCP server on MCP tool directories (mcp.so, Smithery, Glama, PulseMCP, awesome-mcp-servers).

---

## Polished Description (Short)

> **Lattice** -- File-based, agent-native task tracker with an event-sourced core. Gives AI agents persistent shared state for coordinating work across sessions through the filesystem. No database, no server, no API key -- just files in your project directory.

## Polished Description (Full)

Lattice is a task tracker built for AI agent workflows. It stores all state as plain files (JSON, JSONL, Markdown) inside a `.lattice/` directory in your project -- the same way `.git/` lives inside a repository. Every mutation is recorded as an immutable event, giving you a complete audit trail with actor attribution (human or agent). Agents coordinate by reading and writing files they already know how to access.

The MCP server exposes Lattice's full task management API as MCP tools and resources, enabling any MCP-compatible AI client to create tasks, update statuses, assign work, add comments, manage relationships between tasks, attach artifacts, and query project state -- all without shelling out to a CLI.

Key properties:

- **Event-sourced**: Append-only event logs are the source of truth. Snapshots are derived and rebuildable.
- **Agent-native**: Every operation requires actor attribution (`agent:claude`, `human:atin`). Idempotent retries via caller-supplied IDs. Structured JSON responses.
- **Zero infrastructure**: No database, no cloud service, no authentication. Works anywhere the filesystem works.
- **Full CRUD + relationships**: Create, update, assign, comment, link (7 relationship types), attach files/URLs, archive/unarchive.
- **Configurable workflows**: Custom statuses, transitions, WIP limits, task types, and priority levels via `config.json`.

---

## MCP Tools (15 total)

### Write Operations (11 tools)

| Tool | Description |
|------|-------------|
| `lattice_create` | Create a new task with title, type, priority, status, description, tags, and assignee. Returns the task snapshot. Supports caller-supplied IDs for idempotent retries. |
| `lattice_update` | Update task fields (title, description, priority, urgency, type, tags, custom fields via dot notation). Returns the updated snapshot. |
| `lattice_status` | Change a task's workflow status with transition validation. Supports forced transitions with a reason. Returns the updated snapshot. |
| `lattice_assign` | Assign a task to an actor (human or agent). Returns the updated snapshot. |
| `lattice_comment` | Add a comment to a task. Returns the updated snapshot. |
| `lattice_link` | Create a typed relationship between two tasks (blocks, depends_on, subtask_of, related_to, spawned_by, duplicate_of, supersedes). Deduplication enforced. |
| `lattice_unlink` | Remove a relationship between two tasks. |
| `lattice_attach` | Attach a file or URL to a task as an artifact. Supports file, reference, conversation, prompt, and log artifact types with optional title and summary. |
| `lattice_archive` | Archive a completed task (moves snapshot, events, and notes to archive directory). |
| `lattice_unarchive` | Restore an archived task to active status. |
| `lattice_event` | Record a custom event on a task. Event type must start with `x_` (extension namespace). Accepts arbitrary data payloads. |

### Read Operations (4 tools)

| Tool | Description |
|------|-------------|
| `lattice_list` | List active tasks with optional filters: status, assignee, tag, task type, priority. Returns list of task snapshots. |
| `lattice_show` | Show detailed task information including full event history. Automatically finds archived tasks. |
| `lattice_config` | Read the project configuration (workflow statuses, transitions, task types, defaults). |
| `lattice_doctor` | Run data integrity checks on the `.lattice/` directory. Reports missing directories, orphaned files, and snapshot/event mismatches. Optional auto-fix mode. |

---

## MCP Resources (6 total)

| URI | Description |
|-----|-------------|
| `lattice://tasks` | All active task snapshots as a JSON array. |
| `lattice://tasks/{task_id}` | Full task detail including event history. Accepts ULID or short ID (e.g., `LAT-42`). Checks archive if not found in active tasks. |
| `lattice://tasks/status/{status}` | Tasks filtered by status (e.g., `in_progress`, `backlog`). |
| `lattice://tasks/assigned/{actor}` | Tasks filtered by assignee (e.g., `agent:claude`). |
| `lattice://config` | The project `config.json` contents (workflow definition, statuses, transitions). |
| `lattice://notes/{task_id}` | The task's markdown notes file contents. Checks archive if not found. |

---

## Supported Capabilities

- **Transport**: stdio (standard input/output)
- **Protocol**: MCP (Model Context Protocol) via FastMCP
- **Tools**: 15 (11 write, 4 read)
- **Resources**: 6 URI patterns
- **ID resolution**: All tools accept both ULIDs (`task_01HQ...`) and human-friendly short IDs (`LAT-42`)
- **Actor attribution**: Required on all write operations (`prefix:identifier` format)
- **Idempotent writes**: Caller-supplied IDs prevent duplicate task creation on retry
- **Workflow validation**: Status transitions are validated against configurable workflow rules
- **Relationship types**: blocks, depends_on, subtask_of, related_to, spawned_by, duplicate_of, supersedes
- **Artifact types**: file, reference, conversation, prompt, log
- **Custom events**: Extension namespace (`x_` prefix) for domain-specific events with arbitrary data payloads
- **Multi-project**: Optional `lattice_root` parameter on every tool for operating on a specific project directory

---

## Installation and Configuration

### Install

```bash
pip install lattice-tracker[mcp]
# or
uv pip install lattice-tracker[mcp]
```

### Initialize a project (if not already done)

```bash
cd your-project/
lattice init --project-code PROJ --actor human:yourname
```

### Run the MCP server

```bash
lattice-mcp
```

The server communicates over stdio and discovers the `.lattice/` directory by walking up from the current working directory (like `git` finds `.git/`). Override with the `LATTICE_ROOT` environment variable or the `lattice_root` parameter on individual tool calls.

### Claude Desktop configuration

Add to your `claude_desktop_config.json`:

```json
{
  "mcpServers": {
    "lattice": {
      "command": "lattice-mcp",
      "cwd": "/path/to/your/project"
    }
  }
}
```

### Claude Code configuration

Add to your `.mcp.json` or project MCP settings:

```json
{
  "lattice": {
    "command": "lattice-mcp",
    "cwd": "/path/to/your/project"
  }
}
```

### Generic MCP client configuration

Any MCP client that supports stdio transport can connect:

```json
{
  "command": "lattice-mcp",
  "transport": "stdio",
  "env": {
    "LATTICE_ROOT": "/path/to/your/project"
  }
}
```

### Requirements

- Python 3.12+
- A `.lattice/` directory in the target project (created by `lattice init`)

---

## Directory Submission Metadata

### mcp.so

**Submission method**: Comment on [GitHub issue #1](https://github.com/chatmcp/mcp-directory/issues/1) with the repository link and description.

**Draft submission comment:**

```
**Lattice** -- File-based, agent-native task tracker with event-sourced core

https://github.com/Stage-11-Agentics/lattice

Gives AI agents persistent shared state for coordinating work across sessions. 15 MCP tools for full task lifecycle management (create, update, assign, comment, link, attach, archive). 6 resource URIs for reading task state. Event-sourced with immutable audit trail and actor attribution. Zero infrastructure -- no database, no server, just files.

Install: `pip install lattice-tracker[mcp]`
Run: `lattice-mcp`

Categories: Project Management, Developer Tools, AI Agent Coordination
```

### Smithery (smithery.ai)

**Submission method**: Publish via [smithery.ai/new](https://smithery.ai/new) or CLI (`smithery mcp publish`). Smithery auto-scans the server to extract tool/resource metadata. For stdio servers, use the local publishing path via `mcpb`.

**Draft metadata:**

| Field | Value |
|-------|-------|
| Name | `@Stage-11-Agentics/lattice` |
| Display name | Lattice |
| Description | File-based, agent-native task tracker with event-sourced core. 15 MCP tools for task lifecycle management, relationships, artifacts, and workflow automation. Zero infrastructure -- just files. |
| Repository | https://github.com/Stage-11-Agentics/lattice |
| Transport | stdio |
| Install command | `pip install lattice-tracker[mcp]` |
| Run command | `lattice-mcp` |

### Glama (glama.ai/mcp)

**Submission method**: Use the "Add Server" flow on [glama.ai/mcp/servers](https://glama.ai/mcp/servers). Glama indexes GitHub repositories and auto-extracts metadata. Alternatively, contact via Discord or submit through their API.

**Draft metadata:**

| Field | Value |
|-------|-------|
| Name | Lattice |
| Repository | https://github.com/Stage-11-Agentics/lattice |
| Description | Agent-native task tracker with event-sourced core. Exposes 15 MCP tools and 6 resources for full task lifecycle management -- create, update, assign, comment, link, attach, archive. File-based with zero infrastructure. |
| Category | Project Management / Developer Tools |
| Language | Python |
| Scope | Local |
| OS | macOS, Linux, Windows |

### PulseMCP (pulsemcp.com)

**Submission method**: Submit via [pulsemcp.com/submit](https://www.pulsemcp.com/submit) or contact hello@pulsemcp.com.

**Draft metadata:**

| Field | Value |
|-------|-------|
| Name | Lattice |
| Repository | https://github.com/Stage-11-Agentics/lattice |
| Description | File-based, agent-native task tracker. 15 MCP tools for creating, updating, assigning, commenting, linking, and archiving tasks. Event-sourced with immutable audit trail and actor attribution. No database or server required. |
| Category | Project Management |

### awesome-mcp-servers (GitHub)

**Submission method**: Pull request to [punkpeye/awesome-mcp-servers](https://github.com/punkpeye/awesome-mcp-servers). Follow CONTRIBUTING.md format.

**Draft entry (for the "Project Management" or "Developer Tools" category):**

```markdown
- [Lattice](https://github.com/Stage-11-Agentics/lattice) 🐍 🏠 🍎 🪟 🐧 - File-based, agent-native task tracker with event-sourced core. 15 tools for full task lifecycle management with actor attribution, relationship graphs, and configurable workflows.
```

Legend: 🐍 = Python, 🏠 = Local, 🍎 = macOS, 🪟 = Windows, 🐧 = Linux

---

## Tags / Keywords

`task-tracker`, `project-management`, `agent-coordination`, `event-sourcing`, `file-based`, `cli`, `developer-tools`, `ai-agents`, `mcp`, `workflow`, `task-management`, `audit-trail`

---

## Links

| Resource | URL |
|----------|-----|
| Repository | https://github.com/Stage-11-Agentics/lattice |
| PyPI | https://pypi.org/project/lattice-tracker/ |
| Issues | https://github.com/Stage-11-Agentics/lattice/issues |
| Author | [Stage 11 Agentics](https://stage11agentic.com) |
| License | MIT |
