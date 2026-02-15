# Lattice User Guide

Lattice is Linear and JIRA reimagined for the agent-native era. It's a file-based task tracker that lives inside your project directory -- like `.git/` for project management. Where traditional tools assume humans are the primary operators with agents bolted on as integrations, Lattice treats AI agents as first-class participants in the workflow: every command supports machine-readable output, idempotent retries, and structured attribution out of the box. Lattice provides two equal interfaces -- a full-featured CLI and a local web dashboard -- both reading and writing to the same `.lattice/` data directory.

Lattice is purpose-built for coordinating large-scale agentic projects -- the kind where dozens of agents work in parallel across a codebase, each picking up tasks, reporting progress, and handing off to the next. Everything is stored as plain files (JSON, JSONL, Markdown) so your project management lives alongside your code, version-controlled and inspectable.

Lattice is in active development and open source. Pull requests are welcome.

---

## Getting Started

### Install

```bash
cd lattice/
uv venv && uv pip install -e ".[dev]"
```

### Initialize a project

```bash
cd your-project/
lattice init
```

This creates a `.lattice/` directory with default configuration. You only need to do this once per project.

### Set your identity

During `lattice init`, you'll be prompted for your default actor identity. This gets saved to `.lattice/config.json` so you never have to type `--actor` on every command:

```bash
$ lattice init
Default actor identity (e.g., human:atin): human:atin
Initialized empty Lattice in .lattice/
Default actor: human:atin
```

You can also pass it non-interactively:

```bash
lattice init --actor human:atin
```

The `--actor` flag on any write command still overrides the default. Agents can also set the `LATTICE_ACTOR` env var to override per-process.

---

## Core Concepts

### Tasks

A task is the basic unit of work. It has a title, status, priority, type, and can be assigned to someone. Each task gets a unique ID like `task_01HQ...`.

### Statuses

Tasks move through an agent planning workflow. The default pipeline is:

```
backlog → in_planning → planned → in_implementation → implemented → in_review → done
```

Plus `cancelled`, which is reachable from any non-terminal status.

The terminal states are `done` and `cancelled` -- once a task reaches either, the workflow is finished.

Not every transition is allowed. For example, you can't jump directly from `backlog` to `done`. The workflow enforces valid transitions (e.g., `in_review` can go back to `in_implementation` if rework is needed). If you need to force an invalid transition, use `--force --reason "..."`.

### Actors

Every write operation needs an actor to identify who made the change. The format is `prefix:identifier`:

- `human:atin` -- a person
- `agent:claude-opus-4` -- an AI agent
- `team:frontend` -- a team

Actor IDs are free-form strings with no registry. Validation is format-only (must have a recognized prefix, a colon, and a non-empty identifier). There's no uniqueness check -- two agents using `agent:claude` are treated as the same actor.

Set a default during `lattice init` so you don't need `--actor` on every command. The resolution order is:

1. `--actor` flag (highest priority)
2. `LATTICE_ACTOR` environment variable
3. `default_actor` in `.lattice/config.json`

### Events (the source of truth)

Under the hood, Lattice is **event-sourced**. Every change (creating a task, changing status, adding a comment) is recorded as an immutable event in a per-task JSONL file. The task JSON files you see in `tasks/` are **materialized snapshots** -- derived views rebuilt from these events.

This means:
- Events are the authoritative record. Snapshots are a convenience cache.
- If a snapshot gets corrupted, `lattice rebuild` regenerates it from events.
- Writes always append the event **before** updating the snapshot. If a crash happens between the two, rebuild recovers the correct state.
- All timestamps come from the event, not the wall clock, so rebuilds are deterministic.

The lifecycle event log (`_lifecycle.jsonl`) is a derived index of task creation, archival, and unarchival events. It's rebuilt from per-task logs by `lattice rebuild --all`. If per-task logs and the lifecycle log disagree, per-task logs win.

---

## Two Ways to Work

Lattice gives you two interfaces that are fully interchangeable -- both read and write to the same `.lattice/` data directory.

### CLI

The command-line interface is the original and most complete way to use Lattice. It's fully scriptable and supports `--json` for structured machine output and `--quiet` for minimal output (just an ID or "ok"). All write operations are available through the CLI. Agents tend to prefer this interface.

### Dashboard

The web dashboard is a local UI you launch with `lattice dashboard`. It provides visual Board, List, and Activity views, drag-and-drop status changes, task creation and editing, comments, and archiving. It runs at `http://127.0.0.1:8799/` by default.

Both interfaces are first-class citizens. A task created in the dashboard shows up immediately in `lattice list`, and a status change made via `lattice status` appears on the board within seconds.

---

## Typical Workflow

Here's what a complete task lifecycle looks like from the CLI:

```bash
# Human creates and assigns a task
lattice create "Fix auth redirect bug" --type bug --priority high --actor human:atin
lattice assign task_01HQ... agent:claude --actor human:atin

# Agent picks it up and starts implementation
lattice status task_01HQ... in_implementation --actor agent:claude

# Agent adds context
lattice comment task_01HQ... "Root cause: expired token refresh logic" --actor agent:claude

# Agent links a related task
lattice link task_01HQ... related_to task_01HX... --actor agent:claude

# Agent attaches a PR
lattice attach task_01HQ... https://github.com/org/repo/pull/42 \
  --title "PR: Fix auth redirect" --actor agent:claude

# Agent moves to review
lattice status task_01HQ... in_review --actor agent:claude

# Human approves and completes
lattice status task_01HQ... done --actor human:atin

# Clean up
lattice archive task_01HQ... --actor human:atin
```

**From the dashboard:** The same workflow works visually. Create a task with the "+ New Task" button, drag it across the board columns as it progresses (`backlog` to `in_planning` to `planned` and so on), click into the task detail to add comments and change assignments, and archive it when done. See [The Dashboard](#the-dashboard) for details.

---

## Creating and Managing Tasks

### Create a task

```bash
lattice create "Build login page" --actor human:atin
```

With more options:

```bash
lattice create "Fix auth redirect bug" \
  --type bug \
  --priority high \
  --urgency immediate \
  --tags "auth,security" \
  --assigned-to agent:claude \
  --description "Users are redirected to /undefined after SSO login" \
  --actor human:atin
```

**From the dashboard:** Click the "+ New Task" button in the nav bar. Fill in the title (required), type, priority, description, tags, and assignee, then click "Create Task".

**Task types:** `task`, `epic`, `bug`, `spike`, `chore`

**Priorities:** `critical`, `high`, `medium` (default), `low`

**Urgency:** `immediate`, `high`, `normal`, `low`

### Update fields

```bash
lattice update task_01HQ... title="Updated title" --actor human:atin
lattice update task_01HQ... priority=high urgency=immediate --actor human:atin
lattice update task_01HQ... tags="api,backend,urgent" --actor human:atin
```

You can update multiple fields at once. For status and assignment, use their dedicated commands instead.

**Updatable fields:** `title`, `description`, `priority`, `urgency`, `type`, `tags`

**From the dashboard:** Open a task's detail view by clicking it. Most fields are inline-editable -- click the title, description, or tags to edit them. Use the dropdowns to change priority and type.

#### Custom fields (dot notation)

You can store arbitrary key-value data on tasks using dot notation:

```bash
lattice update task_01HQ... custom_fields.estimate="3d" --actor human:atin
lattice update task_01HQ... custom_fields.sprint="2026-Q1-S3" --actor human:atin
lattice update task_01HQ... custom_fields.complexity="high" --actor agent:claude
```

Custom fields are stored in the `custom_fields` object on the task snapshot. They're useful for domain-specific metadata that doesn't fit the built-in fields. Any string key works after `custom_fields.`.

### Change status

```bash
lattice status task_01HQ... in_implementation --actor agent:claude
```

If the transition isn't allowed by the workflow, you'll get an error listing valid transitions. To override:

```bash
lattice status task_01HQ... done --force --reason "Completed offline" --actor human:atin
```

**From the dashboard:** Drag a task card between columns on the Board view, or use the status dropdown in the task detail view. Invalid transitions are blocked -- columns dim to indicate which moves are allowed during a drag.

### Assign a task

```bash
lattice assign task_01HQ... agent:claude --actor human:atin
```

**From the dashboard:** Click the assignee field in the task detail view and type the new actor ID (e.g., `agent:claude`). Clear it to unassign.

### Add a comment

```bash
lattice comment task_01HQ... "Investigated the root cause, it's a race condition in the token refresh" --actor agent:claude
```

**From the dashboard:** Open the task detail view, type in the comment box at the bottom, and click "Post" (or press Ctrl+Enter / Cmd+Enter).

---

## Viewing Tasks

### List all tasks

```bash
lattice list
```

Output looks like:

```
task_01HQ...  backlog  medium  task  "Build login page"  unassigned
task_01HQ...  in_implementation  high  bug  "Fix auth redirect"  agent:claude
```

### Filter the list

```bash
lattice list --status in_implementation
lattice list --assigned agent:claude
lattice list --tag security
lattice list --type bug
```

Filters combine with AND logic.

### Show task details

```bash
lattice show task_01HQ...
```

This prints the full task including description, relationships (both outgoing and incoming), artifacts, notes, and the complete event timeline. Use `--compact` for a brief view, or `--full` to see raw event data.

The `show` command also finds archived tasks automatically.

### Dashboard views

The web dashboard offers three visual alternatives to CLI viewing:

- **Board** -- Kanban-style columns, one per status. Drag and drop to move tasks.
- **List** -- Sortable, filterable table. Filter by status, priority, type, or search text. Toggle to include archived tasks.
- **Activity** -- Recent events across all tasks, showing actor, event type, and timestamp.

Click any task card or table row to open the full detail view.

---

## Relationships

Tasks can be connected to each other. Lattice supports these relationship types:

| Type | Meaning |
|------|---------|
| `blocks` | This task blocks the target |
| `depends_on` | This task depends on the target |
| `subtask_of` | This task is a subtask of the target (useful for epics) |
| `related_to` | Loosely related |
| `spawned_by` | This task was spawned from work on the target |
| `duplicate_of` | This task duplicates the target |
| `supersedes` | This task replaces the target |

### How relationships are stored

Relationships are stored as **outgoing edges only** on the source task's snapshot. When you run `lattice link A blocks B`, the relationship record lives in task A's `relationships_out` array.

However, `lattice show` displays **both directions**: outgoing relationships (links this task has to others) and incoming relationships (links other tasks have to this task). Incoming relationships are derived by scanning all snapshots at read time.

### Create a link

```bash
lattice link task_01HQ... blocks task_01HX... --actor human:atin
```

With an optional note:

```bash
lattice link task_01HQ... depends_on task_01HX... \
  --note "Need the API endpoint before the UI work" \
  --actor human:atin
```

### Remove a link

```bash
lattice unlink task_01HQ... blocks task_01HX... --actor human:atin
```

---

## Artifacts

Artifacts are files or URLs attached to tasks. Use them for logs, conversation transcripts, specs, or any supporting material.

### Attach a file

```bash
lattice attach task_01HQ... ./report.pdf --actor human:atin
```

The file is copied into `.lattice/artifacts/payload/` and metadata is stored separately.

### Attach a URL

```bash
lattice attach task_01HQ... https://github.com/org/repo/pull/42 \
  --title "PR: Fix auth redirect" \
  --actor human:atin
```

### Options

```bash
lattice attach task_01HQ... ./debug.log \
  --type log \
  --title "Debug output from reproduction" \
  --summary "Stack trace showing the race condition" \
  --sensitive \
  --role "debugging" \
  --actor agent:claude
```

**Artifact types:** `file`, `conversation`, `prompt`, `log`, `reference`

The `--sensitive` flag marks artifacts that shouldn't be committed to version control.

---

## Archiving

When a task is done and you want to clean up the active list:

```bash
lattice archive task_01HQ... --actor human:atin
```

This moves the task's snapshot, events, and notes into `.lattice/archive/`. Artifacts stay in place (since multiple tasks might reference them). Archived tasks still appear in `lattice show`.

### Restoring archived tasks

If you archive a task by mistake, you can bring it back:

```bash
lattice unarchive task_01HQ... --actor human:atin
```

This moves the task's files back from archive to the active directories and records a `task_unarchived` event.

**From the dashboard:** You can archive a task from its detail view using the "Archive" button. To see archived tasks, switch to the List view and check "Show archived".

---

## The Dashboard

The dashboard is a local web UI for Lattice. It reads and writes to the same `.lattice/` directory as the CLI.

### Starting the dashboard

```bash
lattice dashboard
```

This launches the server at `http://127.0.0.1:8799/`. Options:

```bash
lattice dashboard --host 0.0.0.0 --port 9000
```

### Board view

The default view is a Kanban board with one column per workflow status. Each column displays task cards showing the title, priority, type, and assignee.

**Drag and drop** a card between columns to change its status. During a drag, valid target columns are highlighted and invalid ones are dimmed. If the transition isn't allowed by the workflow, the drop is rejected with an error message.

### List view

A filterable table of all tasks. Use the dropdowns and search box to filter by status, type, priority, or title text. Check "Show archived" to include archived tasks in the table.

Click any row to open the task detail.

### Activity view

A feed of the most recent events across all tasks. Each entry shows the timestamp, event type, task ID (linked to its detail), a summary of what changed, and who did it. Useful for a quick overview of what's been happening.

### Task detail

Click any card on the board or row in the list to open the full task detail. From here you can:

- **Edit the title** -- click it to inline-edit.
- **Change status** -- use the status dropdown (shows only valid transitions).
- **Change priority and type** -- use the respective dropdowns.
- **Edit the description** -- click the description area to open a text editor.
- **Edit tags** -- click the tags to edit them (comma-separated).
- **Change assignment** -- click the assignee to edit it. Clear the field to unassign.
- **Add comments** -- type in the comment box and press "Post" or Ctrl+Enter.
- **View relationships, artifacts, and custom fields** -- displayed read-only.
- **View the event history** -- the full timeline of changes, newest first.
- **Archive the task** -- click the "Archive" button.

Archived tasks open in read-only mode.

### Creating tasks

Click the "+ New Task" button in the nav bar. A modal appears with fields for title (required), type, priority, description, tags, and assignee. Press Enter in the title field or click "Create Task" to submit. After creation, you're taken directly to the new task's detail view.

### Settings

Click the gear icon in the nav bar to open the settings panel.

- **Background image** -- Set a URL for a background image on the board view. Click "Apply" to save or "Clear" to remove it.
- **Lane colors** -- Customize the header color of each board column. Pick colors with the color pickers and click "Apply Lane Colors". Use "Reset to Defaults" to restore the built-in palette. You can also click the color dot that appears when hovering over a column header in the board view.

Settings are persisted to `.lattice/config.json` and survive server restarts.

### Auto-refresh

The dashboard automatically polls for changes every 5 seconds when viewing the Board, List, or Activity views. If data has changed (e.g., a CLI command updated a task), the view re-renders. Polling pauses when the browser tab is hidden to avoid unnecessary requests.

### Network binding

By default, the dashboard binds to `127.0.0.1` (localhost), which gives full read-write access. If you bind to a non-loopback address (e.g., `--host 0.0.0.0`), the dashboard is automatically forced into **read-only mode** -- all write operations (status changes, task creation, comments, etc.) are disabled and return a 403 error. A warning is printed to stderr when this happens.

---

## Integrity and Recovery

### Health check

```bash
lattice doctor
```

This scans your `.lattice/` directory and checks for:
- Corrupt JSON/JSONL files
- Snapshot drift (snapshot out of sync with events)
- Broken relationship references
- Missing artifacts
- Self-links and duplicate edges
- Malformed IDs
- Lifecycle log inconsistencies

Use `--fix` to automatically repair truncated event log lines.

### Rebuild snapshots

If a snapshot gets corrupted or out of sync:

```bash
lattice rebuild task_01HQ...    # rebuild one task
lattice rebuild --all           # rebuild everything
```

This replays events from the authoritative event log and regenerates the snapshot files. The `--all` flag also rebuilds the lifecycle event log.

---

## Custom Events

For domain-specific events that don't fit the built-in types, use `lattice event` with an `x_` prefix:

```bash
lattice event task_01HQ... x_deployment_started \
  --data '{"environment": "staging", "sha": "abc123"}' \
  --actor agent:deployer
```

Custom event type names **must** start with `x_`. Built-in types like `status_changed` or `task_created` are reserved. Custom events are recorded in the per-task event log but do **not** go to the lifecycle log.

---

## Notes

Every task can have a markdown notes file at `.lattice/notes/<task_id>.md`. These are **not** event-sourced -- they're just regular files you edit directly with any text editor. Use them for freeform context, design notes, or running logs.

```bash
# Create or edit notes for a task
vim .lattice/notes/task_01HQ....md
```

Notes are moved to the archive alongside their task when you run `lattice archive`.

---

## Agent-Friendly Features

Lattice is built for environments where AI agents write most of the task updates. Several features make this smoother:

### JSON output

Add `--json` to any command to get structured output:

```bash
lattice create "My task" --actor agent:claude --json
```

```json
{
  "ok": true,
  "data": { ... }
}
```

Errors follow the same envelope:

```json
{
  "ok": false,
  "error": {
    "code": "NOT_FOUND",
    "message": "Task task_01HQ... not found."
  }
}
```

### Quiet mode

Add `--quiet` to get just the ID or "ok":

```bash
TASK_ID=$(lattice create "My task" --actor agent:claude --quiet)
```

### Idempotent retries

Agents can supply their own IDs to make operations safe to retry:

```bash
lattice create "My task" --id task_01HQ... --actor agent:claude
```

If the task already exists with the same data, Lattice returns success. If it exists with *different* data, Lattice returns a conflict error. This prevents duplicate tasks from agent retries.

The same pattern works for events (`--id ev_...`) and artifacts (`--id art_...`).

### Telemetry passthrough

Agents can attach metadata to events for observability:

```bash
lattice status task_01HQ... in_implementation \
  --actor agent:claude \
  --model claude-opus-4 \
  --session session-abc123
```

---

## Configuration

The workflow is defined in `.lattice/config.json`. You can customize:

- **Statuses** -- add or remove workflow states
- **Transitions** -- define which status changes are allowed
- **WIP limits** -- set advisory limits per status (warnings only in v0)
- **Task types** -- add custom types beyond the defaults
- **Defaults** -- change the default status and priority for new tasks

The default config ships with sensible agent-planning defaults. Edit it directly -- it's just JSON. There is no `lattice config` command in v0.

### Dashboard configuration

The dashboard stores its settings in `config.json` under a `dashboard` key:

- **`lane_colors`** -- an object mapping status names to hex color strings (e.g., `{"backlog": "#adb5bd", "done": "#198754"}`). Controls the board column header colors.
- **`background_image`** -- a URL string for the board background image. Set to `null` or omit to clear.

These are managed through the dashboard settings panel, but you can also edit `config.json` directly.

---

## File Layout

Here's what `.lattice/` looks like on disk:

```
.lattice/
  config.json                      # Workflow configuration
  tasks/task_01HQ....json          # Task snapshots (one per task)
  events/task_01HQ....jsonl        # Event logs (one per task, append-only)
  events/_lifecycle.jsonl           # Lifecycle events (created/archived/unarchived)
  artifacts/meta/art_01HQ....json  # Artifact metadata
  artifacts/payload/art_01HQ...*   # Artifact files
  notes/task_01HQ....md            # Human-editable notes
  archive/                         # Archived tasks, events, and notes
  locks/                           # Internal lock files
```

Everything is plain text. You can `git add .lattice/` to version-control your task management. Use `.gitignore` to exclude sensitive artifact payloads and lock files.

---

## Quick Reference

| Command | What it does |
|---------|-------------|
| `lattice init` | Create a new `.lattice/` project |
| `lattice create <title>` | Create a task |
| `lattice update <id> field=value...` | Update task fields |
| `lattice status <id> <status>` | Change task status |
| `lattice assign <id> <actor>` | Assign a task |
| `lattice comment <id> "<text>"` | Add a comment |
| `lattice list` | List tasks (with optional filters) |
| `lattice show <id>` | Show full task details (incl. incoming relationships) |
| `lattice link <id> <type> <target>` | Create a relationship |
| `lattice unlink <id> <type> <target>` | Remove a relationship |
| `lattice attach <id> <file-or-url>` | Attach an artifact |
| `lattice event <id> <x_type>` | Record a custom event |
| `lattice archive <id>` | Archive a task |
| `lattice unarchive <id>` | Restore an archived task |
| `lattice dashboard` | Launch the web dashboard |
| `lattice doctor` | Check project integrity |
| `lattice rebuild <id\|--all>` | Rebuild snapshots from events |

All write commands need an actor (via `--actor` flag, `LATTICE_ACTOR` env var, or config `default_actor`). Add `--json` for structured output or `--quiet` for minimal output.

All validation errors list the valid options, so agents don't need to look up allowed values.
