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
- **Task-local criteria and evidence**: Optional revisioned criteria with stable
  IDs, retirement history, and neutral comment/artifact traceability links.
- **Full task lifecycle + relationships**: Create, update, assign, comment, link
  (7 relationship types), attach files/URLs, archive/unarchive.
- **Configurable workflows**: Custom statuses, transitions, WIP limits, task types, and priority levels via `config.json`.

---

## MCP Tools (26 total)

### Write Operations (20 tools)

| Tool | Description |
|------|-------------|
| `lattice_create` | Create a new task with title, type, priority, status, description, tags, and assignee. Returns the task snapshot. Supports caller-supplied IDs for idempotent retries. |
| `lattice_criterion_add` | Add an optional task-local acceptance criterion with an automatic or explicit stable ID. |
| `lattice_criterion_edit` | Revise an active criterion while preserving immutable outcome history. |
| `lattice_criterion_retire` | Retire a criterion without deleting its history. |
| `lattice_update` | Update task fields (title, description, priority, urgency, type, tags, custom fields via dot notation). Returns the updated snapshot. |
| `lattice_status` | Change a task's workflow status with transition validation. Supports forced transitions with a reason. Returns the updated snapshot. |
| `lattice_assign` | Assign a task to an actor (human or agent). Returns the updated snapshot. |
| `lattice_comment` | Add a comment, optionally linked to task-local criteria. Returns the updated snapshot. |
| `lattice_link` | Create a typed relationship between two tasks (blocks, depends_on, subtask_of, related_to, spawned_by, duplicate_of, supersedes). Deduplication enforced. |
| `lattice_unlink` | Remove a relationship between two tasks. |
| `lattice_attach` | Attach a file or URL with optional evidence role and task-local criterion links. Supports caller-supplied artifact IDs for retry. |
| `lattice_archive` | Archive a completed task (moves snapshot, events, and notes to archive directory). |
| `lattice_unarchive` | Restore an archived task to active status. |
| `lattice_branch_link` | Link a branch to a task. |
| `lattice_branch_unlink` | Remove a branch link from a task. |
| `lattice_event` | Record a custom event on a task. Event type must start with `x_` (extension namespace). Accepts arbitrary data payloads. |
| `lattice_comment_edit` | Edit a comment body or evidence role while preserving criterion links. |
| `lattice_comment_delete` | Delete a comment from the current materialized view. |
| `lattice_react` | Add a reaction to a comment. |
| `lattice_unreact` | Remove a reaction from a comment. |

### Read Operations (6 tools)

| Tool | Description |
|------|-------------|
| `lattice_criteria` | List criteria for an active or archived task, optionally including retired records and revision history. |
| `lattice_comments` | Read materialized current/deleted task comments and reactions. |
| `lattice_list` | List active tasks with optional filters: status, assignee, tag, task type, priority. Returns list of task snapshots. |
| `lattice_show` | Show detailed task information including full event history. Automatically finds archived tasks. |
| `lattice_config` | Read the project configuration (workflow statuses, transitions, task types, defaults). |
| `lattice_doctor` | Run data integrity checks on the `.lattice/` directory. Reports missing directories, orphaned files, and snapshot/event mismatches. Optional auto-fix mode. |

---

## MCP Resources (7 total)

| URI | Description |
|-----|-------------|
| `lattice://tasks` | All active task snapshots as a JSON array. |
| `lattice://tasks/{task_id}` | Full task detail including event history. Accepts ULID or short ID (e.g., `LAT-42`). Checks archive if not found in active tasks. |
| `lattice://tasks/status/{status}` | Tasks filtered by status (e.g., `in_progress`, `backlog`). |
| `lattice://tasks/assigned/{actor}` | Tasks filtered by assignee (e.g., `agent:claude`). |
| `lattice://config` | The project `config.json` contents (workflow definition, statuses, transitions). |
| `lattice://notes/{task_id}` | The task's markdown notes file contents. Checks archive if not found. |
| `lattice://explain/{filepath}` | Reverse lookup: decisions that shaped a file. Scans task snapshots for matching `linked_files` and parses `Decisions.md`. Supports directory prefixes and globs. |

---

## Supported Capabilities

- **Transport**: stdio (standard input/output)
- **Protocol**: MCP (Model Context Protocol) via FastMCP
- **Tools**: 26 (20 write, 6 read)
- **Resources**: 7 URI patterns
- **ID resolution**: All tools accept both ULIDs (`task_01HQ...`) and human-friendly short IDs (`LAT-42`)
- **Actor attribution**: Required on all write operations (`prefix:identifier` format)
- **Idempotent writes**: Caller-supplied IDs prevent duplicate task creation on retry
- **Acceptance criteria**: Optional task-local stable IDs, immutable revisions,
  retirement, archived reads, and evidence traceability without inferred verdicts
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

Gives AI agents persistent shared state for coordinating work across sessions. 26 MCP tools for task lifecycle management, optional acceptance criteria, evidence, comments, and relationships. 7 resource URIs for reading task state. Event-sourced with immutable audit trail and actor attribution. Zero infrastructure -- no database, no server, just files.

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
| Description | File-based, agent-native task tracker with event-sourced core. 26 MCP tools for task lifecycle management, acceptance criteria, relationships, artifacts, and workflow automation. Zero infrastructure -- just files. |
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
| Description | Agent-native task tracker with event-sourced core. Exposes 26 MCP tools and 7 resources for task lifecycle management, optional acceptance criteria, evidence, and relationships. File-based with zero infrastructure. |
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
| Description | File-based, agent-native task tracker. 26 MCP tools for task lifecycle management, optional acceptance criteria, evidence, and relationships. Event-sourced with immutable audit trail and actor attribution. No database or server required. |
| Category | Project Management |

### awesome-mcp-servers (GitHub)

**Submission method**: Pull request to [punkpeye/awesome-mcp-servers](https://github.com/punkpeye/awesome-mcp-servers). Follow CONTRIBUTING.md format.

**Draft entry (for the "Project Management" or "Developer Tools" category):**

```markdown
- [Lattice](https://github.com/Stage-11-Agentics/lattice) 🐍 🏠 🍎 🪟 🐧 - File-based, agent-native task tracker with event-sourced core. 26 tools for task lifecycle management, optional acceptance criteria, evidence, actor attribution, relationship graphs, and configurable workflows.
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
| Author | [Stage 11 Agentics](https://stage11agentics.com) |
| License | MIT |
