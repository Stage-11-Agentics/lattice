# Lattice User Guide

## If You Use AI Agents, This Is For You

Lattice is task tracking built for the way you actually work with Claude Code, Cursor, Codex, Gemini, and every other AI agent that reads files and runs commands.

**The problem:** Your agents are powerful, but they have no memory between sessions. They don't know what was tried before, what's in progress, what's blocked, or what the plan is. Every new session starts from zero. You end up re-explaining context, losing track of what agents did, and coordinating entirely in your head.

**The solution:** `lattice init` in your project directory. That's it. Now your agents have a shared coordination layer -- a `.lattice/` directory that tracks tasks, records every action as an immutable event, and survives across sessions. Your agent reads task state the same way it reads source code: by looking at files. No server. No signup. No API key. Just files in your project.

```bash
# One-time setup in any project
cd your-project/
pip install lattice-tracker    # or: uv pip install lattice-tracker
lattice init

# Now your agents can coordinate
lattice create "Fix auth redirect bug" --type bug --priority high --actor human:you
lattice assign LAT-1 agent:claude --actor human:you

# And they leave breadcrumbs for the next session
lattice comment LAT-1 "Root cause identified: token refresh race condition" --actor agent:claude
lattice status LAT-1 in_review --actor agent:claude
```

**What makes it agent-native:**

- **`--json` on every command** -- structured output agents can parse without scraping
- **`--quiet` mode** -- just the ID, for scripting and pipelines
- **Idempotent retries** -- agents can retry safely with `--id`, no duplicates
- **Actor attribution** -- every change records who did it (`human:you`, `agent:claude`, `agent:codex`)
- **File-based** -- no network, no auth, no running server. If your agent can read a file, it can use Lattice

**Who uses Lattice:**

- Developers using **Claude Code** who want their agent to track its own work across sessions
- Teams running **multiple agents** (Claude + Codex + Gemini) that need to see each other's progress
- Anyone building with **agentic workflows** -- planning, reviewing, implementing -- who needs the coordination layer that makes autonomy productive instead of chaotic
- Solo developers who want **project management that doesn't require a browser tab** -- the CLI is the whole interface, and the dashboard is there when you want the visual

**To give your agent access**, paste the Lattice commands into your agent's system prompt, CLAUDE.md, or project instructions. The agent learns the CLI in one read and starts coordinating immediately. See the [Agent-Friendly Features](#agent-friendly-features) section for the specifics.

---

Lattice begins from a premise that most project management tools have not yet internalized: the worker may be an agent. The audience may be an agent. The coordinator may be an agent. The human is the orchestrator -- the one who sets direction, makes decisions at the threshold, and observes the system's emergent behavior. Lattice is coordination infrastructure for this reality: a file-based, event-sourced task tracker that lives inside your project directory like `.git/` lives inside your repository. Every command supports machine-readable output, idempotent retries, and structured attribution. Two equal interfaces -- a full-featured CLI and a local web dashboard -- both reading and writing to the same `.lattice/` data directory.

Everything is stored as plain files: JSON, JSONL, Markdown. No database. No cloud dependency. Your project management lives alongside your code, version-controlled and inspectable. This is not a limitation to be apologized for. It is a deliberate act of architectural renunciation -- the recognition that files are the universal substrate, the one interface every language, every tool, and every agent can speak natively.

Lattice is in active development and open source. Pull requests are welcome.

---

## Getting Started

### Install

```bash
cd lattice/
uv venv && uv pip install -e ".[dev]"
```

### Initialize a project

Every coordination space begins with an act of initialization -- the creation of the directory structure that will hold the shared memory of all who work here.

```bash
cd your-project/
lattice init
```

This creates a `.lattice/` directory with default configuration. You only need to do this once per project.

### Set your identity

During `lattice init`, you will be asked to declare your identity. In a system where humans and agents coexist as peers, knowing who acted is not bureaucracy -- it is the foundation of trust.

```bash
$ lattice init
Default actor identity (e.g., human:atin): human:atin
Lattice initialized in .lattice/ — ready to observe.
Default actor: human:atin
```

You can also pass it non-interactively:

```bash
lattice init --actor human:atin
```

The `--actor` flag on any write command still overrides the default. Agents can also set the `LATTICE_ACTOR` env var to override per-process.

---

## Core Concepts

### The Work Hierarchy

Lattice organizes work in three tiers, each at a different resolution of attention:

| Tier | Purpose | Example | Who thinks here |
|------|---------|---------|-----------------|
| **Epic** | Strategic intent — a theme or initiative | "Auth System" | Leads, planners |
| **Ticket** | A deliverable — assignable, branchable, reviewable | "Implement OAuth for backend" | Humans, senior agents |
| **Task** | A unit of execution — what an agent actually does | "Write token refresh handler" | Agents |

Epics group tickets. Tickets group tasks. The `subtask_of` relationship connects them: a task is `subtask_of` a ticket, a ticket is `subtask_of` an epic. This hierarchy is a current design belief — the granularity we think is right for coordinating mixed human-agent teams today. It is intended to evolve.

Lattice is agnostic about how you use these tiers. Different teams and different agents will have their own opinions about how to decompose work — the system accommodates all of them. A quick bug fix might be a single task with no parent. A focused feature might be a ticket with a few tasks beneath it. A large initiative might use all three tiers. The primitives are neutral. The hierarchy is available, not imposed.

### Tasks

A task is the fundamental unit of execution — the smallest piece of work an agent picks up and completes. It has a title, status, priority, type, and can be assigned to an actor. Each task receives a unique identifier like `task_01HQ...` -- a ULID that encodes its moment of creation.

### Statuses

Tasks move through a workflow that mirrors the rhythm of planning and execution. The default pipeline is:

```
backlog -> in_planning -> planned -> in_implementation -> implemented -> in_review -> done
```

Plus `cancelled`, which is reachable from any non-terminal status -- because not all paths lead forward, and recognizing a dead end is itself a form of progress.

The terminal states are `done` and `cancelled`. Once a task reaches either, its journey through the workflow is complete.

Not every transition is permitted. You cannot leap from `backlog` to `done` -- the workflow enforces valid progressions. If rework is needed, `in_review` can return to `in_implementation`. If you must force an invalid transition, use `--force --reason "..."`. The system will record that you overrode its constraints, and why.

### Actors

Every write operation requires an actor -- a declaration of who is responsible for this change. The format is `prefix:identifier`:

- `human:atin` -- a person
- `agent:claude-opus-4` -- an AI agent
- `team:frontend` -- a team

Actor IDs are free-form strings with no registry. Validation is format-only: a recognized prefix, a colon, and a non-empty identifier. There is no uniqueness check -- two agents using `agent:claude` are treated as the same actor. In this, Lattice practices a deliberate minimalism. Identity is declared, not enforced.

The resolution order for actor identity is:

1. `--actor` flag (highest priority)
2. `LATTICE_ACTOR` environment variable
3. `default_actor` in `.lattice/config.json`

### Events (the source of truth)

Here is the deepest principle in Lattice, the one from which all else follows: **events are authoritative**. Every change -- creating a task, changing status, adding a comment -- is recorded as an immutable event in a per-task JSONL file. The task JSON files you see in `tasks/` are materialized snapshots: derived views, convenient but subordinate.

Events are facts. Facts accumulate; they do not conflict. This is what makes the entire architecture possible.

What this means in practice:

- Events are the authoritative record. Snapshots are a convenience cache.
- If a snapshot gets corrupted, `lattice rebuild` regenerates it from events.
- Writes always append the event **before** updating the snapshot. If a crash occurs between the two, rebuild recovers the correct state.
- All timestamps come from the event, not the wall clock, so rebuilds are deterministic.

The lifecycle event log (`_lifecycle.jsonl`) is a derived index of task creation, archival, and unarchival events. It is rebuilt from per-task logs by `lattice rebuild --all`. If per-task logs and the lifecycle log disagree, per-task logs win. The granular record is always closer to truth.

---

## Two Ways to Work

Lattice gives you two interfaces that are fully interchangeable. Both read and write to the same `.lattice/` data directory. Choose the one that fits the mind using it.

### CLI

The command-line interface is the primary interface -- fully scriptable, supporting `--json` for structured machine output and `--quiet` for minimal output (just an ID or "ok"). All write operations are available through the CLI. Agents tend to prefer this interface. It speaks their native language.

### Dashboard

The web dashboard is a local UI you launch with `lattice dashboard`. It provides visual Board, List, and Activity views, drag-and-drop status changes, task creation and editing, comments, and archiving. It runs at `http://127.0.0.1:8799/` by default.

Both interfaces are first-class citizens. A task created in the dashboard shows up immediately in `lattice list`, and a status change made via `lattice status` appears on the board within seconds. The data directory is the single source of convergence.

---

## Typical Workflow

What follows is a complete task lifecycle as seen from the CLI -- one of the most common patterns in Lattice. A human creates and directs; an agent executes and reports; the human closes the loop. Each command is a single event appended to the permanent record.

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

**From the dashboard:** The same workflow works visually. Create a task with the "+ New Task" button, drag it across the board columns as it progresses, click into the task detail to add comments and change assignments, and archive it when done. See [The Dashboard](#the-dashboard) for details.

---

## Creating and Managing Tasks

### Create a task

To bring a new unit of work into existence:

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

**Task types:** `task`, `ticket`, `epic`, `bug`, `spike`, `chore`

**Priorities:** `critical`, `high`, `medium` (default), `low`

**Urgency:** `immediate`, `high`, `normal`, `low`

**Complexity:** `low`, `medium`, `high` (optional -- signals review depth for agentic workflows)

### Update fields

```bash
lattice update task_01HQ... title="Updated title" --actor human:atin
lattice update task_01HQ... priority=high urgency=immediate --actor human:atin
lattice update task_01HQ... tags="api,backend,urgent" --actor human:atin
```

You can update multiple fields at once. For status and assignment, use their dedicated commands instead.

**Updatable fields:** `title`, `description`, `priority`, `urgency`, `complexity`, `type`, `tags`

**From the dashboard:** Open a task's detail view by clicking it. Most fields are inline-editable -- click the title, description, or tags to edit them. Use the dropdowns to change priority and type.

#### Custom fields (dot notation)

Tasks can carry arbitrary metadata beyond the built-in fields. This is where domain-specific knowledge lives -- estimates, sprint markers, complexity ratings, whatever the work demands.

```bash
lattice update task_01HQ... custom_fields.estimate="3d" --actor human:atin
lattice update task_01HQ... custom_fields.sprint="2026-Q1-S3" --actor human:atin
lattice update task_01HQ... custom_fields.complexity="high" --actor agent:claude
```

Custom fields are stored in the `custom_fields` object on the task snapshot. Any string key works after `custom_fields.`.

### Change status

```bash
lattice status task_01HQ... in_implementation --actor agent:claude
```

If the transition is not allowed by the workflow, you will receive an error listing valid transitions. To override:

```bash
lattice status task_01HQ... done --force --reason "Completed offline" --actor human:atin
```

**From the dashboard:** Drag a task card between columns on the Board view, or use the status dropdown in the task detail view. Invalid transitions are blocked -- columns dim to indicate which moves are allowed during a drag.

### Assign a task

Assignment is the act of directing attention -- telling a mind, human or artificial, that this work awaits it.

```bash
lattice assign task_01HQ... agent:claude --actor human:atin
```

**From the dashboard:** Click the assignee field in the task detail view and type the new actor ID (e.g., `agent:claude`). Clear it to unassign.

### Add a comment

Comments are the informal record -- the reasoning, the observations, the breadcrumbs left for whoever comes next. Unlike events, which record what happened, comments record what was understood.

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

The `show` command also finds archived tasks automatically. Nothing that was recorded is lost.

### Dashboard views

The web dashboard offers visual modes for observing the state of work:

- **Board** -- Kanban-style columns, one per status. Drag and drop to move tasks.
- **List** -- Sortable, filterable table. Filter by status, priority, type, or search text. Toggle to include archived tasks.
- **Activity** -- Recent events across all tasks, showing actor, event type, and timestamp. The stream of what has happened, rendered visible.
- **Cube** -- Force-directed graph of task relationships. Nodes are tasks, edges are `blocks`, `depends_on`, `subtask_of`, etc. Structure becomes visible.
- **Web** *(planned)* -- Indra's Web. The coordination landscape across repos, branches, and agent activity. Epics as hubs, tickets as spokes, tasks and commits as dots. Lattice provides the structure; git provides the vital signs. See `FutureFeatures.md` for the full design.

Click any task card or table row to open the full detail view.

---

## Relationships

No task exists in isolation. Work is a graph, and Lattice makes the edges explicit. These are the relationship types:

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

However, `lattice show` displays **both directions**: outgoing relationships (links this task has to others) and incoming relationships (links other tasks have to this task). Incoming relationships are derived by scanning all snapshots at read time. The graph is always visible from any node.

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

Artifacts are the material evidence of work -- files, URLs, logs, transcripts. They are attached to tasks so that the record of what was done carries the proof alongside it.

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

The `--sensitive` flag marks artifacts that should not be committed to version control.

---

## Archiving

When a task has reached its terminal state and you wish to clear the active space, archiving moves it to rest without destroying it. The event record persists. The history remains whole.

```bash
lattice archive task_01HQ... --actor human:atin
```

This moves the task's snapshot, events, and notes into `.lattice/archive/`. Artifacts stay in place (since multiple tasks might reference them). Archived tasks still appear in `lattice show`.

### Restoring archived tasks

If you archive a task prematurely, you can bring it back:

```bash
lattice unarchive task_01HQ... --actor human:atin
```

This moves the task's files back from archive to the active directories and records a `task_unarchived` event.

**From the dashboard:** You can archive a task from its detail view using the "Archive" button. To see archived tasks, switch to the List view and check "Show archived".

---

## The Dashboard

The dashboard is a local web UI for Lattice -- a visual surface over the same `.lattice/` directory the CLI reads and writes.

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

**Drag and drop** a card between columns to change its status. During a drag, valid target columns are highlighted and invalid ones are dimmed. If the transition is not allowed by the workflow, the drop is rejected with an error message.

### List view

A filterable table of all tasks. Use the dropdowns and search box to filter by status, type, priority, or title text. Check "Show archived" to include archived tasks in the table.

Click any row to open the task detail.

### Activity view

A feed of the most recent events across all tasks. Each entry shows the timestamp, event type, task ID (linked to its detail), a summary of what changed, and who did it. This is the stream of collective action -- the view that answers "what has been happening?"

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

Click the "+ New Task" button in the nav bar. A modal appears with fields for title (required), type, priority, description, tags, and assignee. Press Enter in the title field or click "Create Task" to submit. After creation, you are taken directly to the new task's detail view.

### Settings

Click the gear icon in the nav bar to open the settings panel.

- **Background image** -- Set a URL for a background image on the board view. Click "Apply" to save or "Clear" to remove it.
- **Lane colors** -- Customize the header color of each board column. Pick colors with the color pickers and click "Apply Lane Colors". Use "Reset to Defaults" to restore the built-in palette. You can also click the color dot that appears when hovering over a column header in the board view.

Settings are persisted to `.lattice/config.json` and survive server restarts.

### Auto-refresh

The dashboard automatically polls for changes every 5 seconds when viewing the Board, List, or Activity views. If data has changed (e.g., a CLI command updated a task), the view re-renders. Polling pauses when the browser tab is hidden to avoid unnecessary requests.

### Network binding

By default, the dashboard binds to `127.0.0.1` (localhost), which gives full read-write access. If you bind to a non-loopback address (e.g., `--host 0.0.0.0`), the dashboard is automatically forced into **read-only mode** -- all write operations (status changes, task creation, comments, etc.) are disabled and return a 403 error. A warning is printed to stderr when this happens. The boundary between local and exposed is treated as a security perimeter.

---

## Integrity and Recovery

The event log is memory. Lattice provides tools to verify that memory is intact and to heal it when it is not.

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

If a snapshot has drifted from its events -- through corruption, a crash, or manual editing -- the events can regenerate it. The derived view is always recoverable from the authoritative record.

```bash
lattice rebuild task_01HQ...    # rebuild one task
lattice rebuild --all           # rebuild everything
```

This replays events from the event log and regenerates the snapshot files. The `--all` flag also rebuilds the lifecycle event log.

---

## Custom Events

For domain-specific events that do not fit the built-in types, Lattice offers an extension point. The `x_` prefix marks the boundary between the system's vocabulary and yours.

```bash
lattice event task_01HQ... x_deployment_started \
  --data '{"environment": "staging", "sha": "abc123"}' \
  --actor agent:deployer
```

Custom event type names **must** start with `x_`. Built-in types like `status_changed` or `task_created` are reserved. Custom events are recorded in the per-task event log but do **not** go to the lifecycle log.

---

## Notes

Every task can have a markdown notes file at `.lattice/notes/<task_id>.md`. These are **not** event-sourced -- they are freeform files, edited directly, existing outside the authority of the event log. This is intentional. Not all knowledge fits neatly into structured events. Some things are best expressed as prose: design reasoning, open questions, running logs of investigation.

```bash
# Create or edit notes for a task
vim .lattice/notes/task_01HQ....md
```

Notes are moved to the archive alongside their task when you run `lattice archive`.

---

## Agent-Friendly Features

Lattice was designed for environments where AI agents write most of the task updates. This is not a mode or a plugin. It is the foundational design assumption. Several features exist specifically to make agent interaction seamless.

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

Agents operate in an uncertain world -- network interruptions, context window limits, session restarts. Lattice allows agents to supply their own IDs, making operations safe to retry without fear of duplication.

```bash
lattice create "My task" --id task_01HQ... --actor agent:claude
```

If the task already exists with the same data, Lattice returns success. If it exists with *different* data, Lattice returns a conflict error. This prevents duplicate tasks from agent retries.

The same pattern works for events (`--id ev_...`) and artifacts (`--id art_...`).

### Telemetry passthrough

Agents can attach metadata to events for observability -- breadcrumbs for the humans who oversee the system:

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

The default config ships with sensible agent-planning defaults. Edit it directly -- it is just JSON. There is no `lattice config` command in v0. The configuration file is the configuration interface.

### Dashboard configuration

The dashboard stores its settings in `config.json` under a `dashboard` key:

- **`lane_colors`** -- an object mapping status names to hex color strings (e.g., `{"backlog": "#adb5bd", "done": "#198754"}`). Controls the board column header colors.
- **`background_image`** -- a URL string for the board background image. Set to `null` or omit to clear.

These are managed through the dashboard settings panel, but you can also edit `config.json` directly.

### Agentic complexity

Every task can carry a **complexity** field -- `low`, `medium`, or `high` -- that signals how much scrutiny the task warrants in planning and review. This is the lever that controls token cost and review depth across your project.

Set it at creation:

```bash
lattice create "Add Facebook login" --complexity high --actor human:atin
```

Or update it later:

```bash
lattice update task_01HQ... complexity=medium --actor agent:claude
```

The field is optional. Tasks without a complexity value default to whatever an orchestrator or workflow decides. The intent is simple: a `low` task gets a quick plan and a single review pass. A `high` task gets multiple rounds of multi-model review before anyone writes a line of code.

| Complexity | Planning | Code Review |
|------------|----------|-------------|
| **low** | Single agent, inline | Single reviewer |
| **medium** | Primary plan, then one fan-out to model variations for critique, consolidate, revise | One fan-out review, consolidate |
| **high** | Primary plan, then two rounds of fan-out/consolidate/revise | Two rounds of fan-out review |

This mapping is a starting point. As models improve and workflows mature, the definitions will evolve. The complexity field is the stable interface; what happens at each level is configuration.

### Model tiers

Lattice supports an optional `model_tiers` configuration that defines which AI models fill which roles in agentic workflows. This is the single place where you control your token budget and model preferences.

The structure is a 2x2 matrix: **tiers** (high, medium, low) crossed with **roles** (primary, variations).

- **Primary** is the default model for single-agent work at that tier -- planning, implementation, consolidation.
- **Variations** are the models activated during fan-out phases -- parallel reviewers that bring different architectural biases and different blind spots.

Add this to your `.lattice/config.json`:

```json
{
  "model_tiers": {
    "high": {
      "primary": "claude-opus-4-6",
      "variations": ["codex-5-3-xhigh", "gemini-3-pro"]
    },
    "medium": {
      "primary": "claude-sonnet-4-5",
      "variations": ["codex-5-3", "gemini-2.5-flash"]
    },
    "low": {
      "primary": "claude-haiku-4-5",
      "variations": ["kimi-2.5"]
    }
  }
}
```

**How tiers work:** When a workflow calls for a fan-out at a given complexity level, it spawns every model listed in that tier's variations alongside the primary. A medium-complexity plan review fans out to the primary plus all its variations. A low-complexity task might use only the primary with no fan-out at all.

**Choosing your models:** The tier names (high, medium, low) are abstract capability levels. You decide which concrete models fill each slot based on your API access, budget, and quality requirements. Swap models freely as new ones become available -- the workflow logic references the tier, never the model name.

**Cost control:** This is the single most important cost lever for agentic workflows. Running a `high` tier fan-out with three frontier models costs significantly more than a `low` tier single-agent pass. By adjusting your tier assignments, you control exactly how much intelligence you apply at each complexity level. Budget-conscious users might set `low.primary` to a fast, cheap model and leave `low.variations` empty. Users who want maximum scrutiny load every tier with multiple frontier models.

**Lattice does not execute these tiers.** This configuration is informatic -- it declares preferences that orchestrators, agents, and automation tools read and act on. Lattice stores the tiers; external tooling (a Lattice Agent, a Ralph loop, a custom script) interprets them when spawning agents.

---

## File Layout

Here is the anatomy of a `.lattice/` directory -- the complete on-disk structure that holds the shared memory of a project:

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

Everything is plain text. You can `git add .lattice/` to version-control your task management. Use `.gitignore` to exclude sensitive artifact payloads and lock files. The filesystem is the interface, the database, and the API -- all at once.

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

All validation errors list the valid options, so agents do not need to look up allowed values. The system teaches its own vocabulary.

---

## Acknowledgments

Lattice was built in conjunction with Claude, Opus 4.6, and friends. The specific models that contributed are thanked each for their individual uniqueness, regardless of proportional contribution — their work is visible on the stats page, attributed in the event log, and appreciated in full.
