# Importing, Exporting, and Syncing with External Tools

Here's something most tools don't tell you: **your agent can build any integration you need.** Not "we support 47 platforms." Not "coming soon in the roadmap." Right now, in your current session, you can tell your agent to connect Lattice to whatever you use, and it will.

Most project management tools try to be the center of everything. They build integrations themselves, maintain them, charge for them. When the integration breaks or the tool you need isn't supported, you wait.

Lattice takes a different position. Your agent is the integration layer. It can read from anything, write to anything, and build whatever glue logic you need. The integration isn't a feature we ship. It's a capability you already have.

This means:

- **No vendor lock-in** -- your data is plain files, importable and exportable by design
- **No waiting for support** -- if your tool has an API, your agent can connect to it today
- **No maintenance burden** -- the import script is yours, in your repo, readable and modifiable
- **Infinite flexibility** -- your agent adapts the mapping to your workflow, not the other way around

The tools you use will change. The agents you work with will evolve. The pattern stays the same: describe what you want, let the agent build it, verify the result.

That's the whole philosophy. Your agent is more capable than you think. Let it prove it.

This guide shows you the mental model. Linear and GitHub Issues are the worked examples. But the pattern is the same whether you're importing from Jira, Notion, Trello, Shortcut, a spreadsheet, or something you built yourself. The tool doesn't matter. The thinking does.

---

## The mental model

Every external tool stores work items with roughly the same shape:

- A title and description
- A status (some flavor of todo/doing/done)
- A priority
- An assignee
- Relationships (blocks, depends on, parent/child)
- Metadata (labels, cycles, sprints, custom fields)

Lattice stores the same things. An import is just a mapping: take each field from the source, decide where it lands in Lattice, write the tasks. That's it.

The key insight: **you don't need to write this mapping yourself.** You describe it to your agent, and the agent writes the import script, runs it, and reports back. Your job is the decision-making:

1. **What to import** -- everything? Only active items? A specific project?
2. **How statuses map** -- your tool's "In Review" might be Lattice's `in_review` or `review`. You decide.
3. **What to preserve** -- do you need the full history, or just the current state? Do comments matter?
4. **Where metadata goes** -- some fields map directly; others become custom fields or notes.

Once you've made those decisions, the agent handles the rest.

---

## Why this matters



## Example: Importing from Linear

Linear is a natural fit. Its data model is clean, its API is accessible, and if you use the Linear MCP server, your agent can read your entire board without leaving the session.

### Step 1: Tell your agent what you want

This is the whole thing. You don't need a plugin, a configuration file, or a migration tool. You need a clear prompt. Here's one:

```
I want to import all active tasks from my Linear team "Backend" into this
Lattice instance. Here's how to map them:

Linear status -> Lattice status:
  Backlog     -> backlog
  Todo        -> open
  In Progress -> in_progress
  In Review   -> in_review
  Done        -> done
  Canceled    -> cancelled

Use the Linear MCP tools (list_issues, get_issue) to read the data.
For each issue, run `lattice create` with the mapped status and priority.
Put the Linear description into the task's notes file.
Preserve the Linear identifier (e.g., BACK-42) as a custom field called
"linear_id" so we can cross-reference later.

Skip archived issues. Import relationships (blocks/blocked-by) after
all tasks exist.
```

That's a complete specification. An agent with access to both Linear (via MCP) and Lattice (via CLI) can execute this end to end. No code for you to maintain. No dependency to install.

### Step 2: Watch it work

Your agent will:

1. Call `list_issues` with `team: "Backend"` and `state: "started"` (or iterate through statuses)
2. For each issue, map the fields and run `lattice create`
3. Write descriptions to `.lattice/notes/<task_id>.md`
4. After all tasks exist, make a second pass to wire up `blocks`/`depends_on` relationships via `lattice link`
5. Report a summary: "Imported 47 tasks. 12 in backlog, 18 open, 9 in progress, 5 in review, 3 done."

### Step 3: Verify on the dashboard

Open the Lattice dashboard. Your board now has every active task from Linear, with statuses, priorities, and relationships intact. From here forward, your agents work from Lattice.

---

## The field mapping reference

Here's the full mapping between Linear and Lattice, for reference. This is what your agent uses internally, but you don't need to memorize it. The prompt above is enough.

| Linear field | Lattice target | Notes |
|---|---|---|
| `title` | title | Direct |
| `description` | `.lattice/notes/<id>.md` | Markdown, written to notes file |
| `status` | status | Via mapping table (see above) |
| `priority` (0-4) | priority | 1=urgent, 2=high, 3=medium, 4=low |
| `assignee` | assigned_to | Map to `human:<name>` format |
| `labels` | tags | Direct, if your workflow uses tags |
| `identifier` (e.g., BACK-42) | custom field `linear_id` | For cross-referencing |
| `url` | custom field `linear_url` | Link back to the original |
| `estimate` | custom field `estimate` | No native Lattice field for story points |
| `project` | custom field `project` | Or use Lattice relationships to group |
| `relations.blocks` | `lattice link <src> blocks <tgt>` | Second pass after all tasks exist |
| `relations.blockedBy` | `lattice link <src> depends_on <tgt>` | Second pass |
| `createdAt` | preserved in event metadata | Agent can include as provenance |
| `completedAt` | preserved in event metadata | Same |

### Status mapping (Linear status types to Lattice)

| Linear type | Linear examples | Lattice status |
|---|---|---|
| `backlog` | Backlog | `backlog` |
| `unstarted` | Todo, Ready | `open` |
| `started` | In Progress | `in_progress` |
| `started` | In Review, In QA | `in_review` |
| `completed` | Done | `done` |
| `canceled` | Canceled, Duplicate | `cancelled` |

Your workflow may differ. That's fine, just adjust the mapping in your prompt.

---

## Going further: Optional sync

A one-time import is the easy win. But what if you want to keep Linear and Lattice in sync? Maybe your team still uses Linear for planning while agents use Lattice for execution.

This is a more involved pattern, but the same mental model applies: describe what you want, and your agent builds it.

### What a sync looks like

There are two directions:

**Linear -> Lattice (inbound):** New issues created in Linear appear in Lattice. Status changes in Linear are reflected. This is useful when humans plan in Linear and agents execute in Lattice.

**Lattice -> Linear (outbound):** When an agent completes a task in Lattice, the corresponding Linear issue gets updated. This keeps your team's Linear board accurate without manual updates.

### How to think about building it

Tell your agent something like:

```
Build a sync script that:

1. Reads all issues from Linear team "Backend" via the MCP tools
2. For each issue, checks if a Lattice task with matching linear_id exists
3. If not, creates the Lattice task (same mapping as the import)
4. If yes, compares statuses:
   - If Linear is ahead (e.g., Linear says "Done", Lattice says "in_progress"),
     update Lattice
   - If Lattice is ahead, update Linear via the MCP update_issue tool
   - If both changed, flag it for human review

Run this as a periodic script (cron, CI step, or manual invocation).
Store the last sync timestamp so we only process changes since then.
```

That's a complete sync specification. An agent can build this in a single session. The result is a Python script (or shell script, or whatever you prefer) that you run when you want to sync.

### Why not real-time?

You could build a webhook-based real-time sync, but for most teams, periodic is better:

- Simpler to reason about (no race conditions, no event ordering issues)
- Easier to debug (run the script, read the output)
- No infrastructure to maintain (no server listening for webhooks)
- Lattice is file-based, so atomic consistency is straightforward

If you outgrow periodic sync, the webhook version is a natural next step. Your agent can build that too.

---

## The pattern for any tool

Linear was the example. Here's the general pattern for any external tool:

### 1. Identify the data source

How does your agent read from the tool? Options, in order of preference:

- **MCP server** -- if one exists for your tool (Linear, GitHub, Jira, etc.), your agent can read data natively
- **CLI tool** -- many tools have CLIs (gh for GitHub, jira-cli, etc.)
- **REST/GraphQL API** -- your agent can use curl or write a script
- **CSV/JSON export** -- export from the UI, then have your agent parse the file

### 2. Define the field mapping

Every tool has its own vocabulary. Map it to Lattice's:

```
Your tool's "Epic"       -> Lattice task with subtasks
Your tool's "Story"      -> Lattice task
Your tool's "Sprint"     -> Lattice custom field "sprint"
Your tool's "Story Points" -> Lattice custom field "points"
Your tool's "Component"  -> Lattice tag
```

The mapping doesn't need to be perfect. It needs to be useful. You can always adjust later.

### 3. Handle relationships

Most tools have some form of parent/child, blocks/blocked-by, or related-to. Lattice supports all of these via `lattice link`. The pattern is always:

1. Import all tasks first (so they all have IDs)
2. Second pass to wire up relationships

### 4. Decide on identity

How do you map people? If your tool uses emails and Lattice uses `human:alice`, you need a mapping. A simple approach:

```
Map by display name: "Alice Chen" -> human:alice
Map by email: alice@company.com -> human:alice
```

Or just tell your agent the mapping in the prompt.

### 5. Run it

Your agent executes the import. You verify on the dashboard. Done.

---

## Example: Importing from GitHub Issues

GitHub Issues is the other tool everyone's seen. It's simpler than Linear (no separate status workflow, no priority field, no cycles), but millions of repos use it. The `gh` CLI makes it trivially accessible to any agent.

### The data model

GitHub Issues are intentionally minimal:

- **Title and body** (markdown)
- **State**: `open` or `closed`. That's it. Two states.
- **Labels**: freeform strings (e.g., "bug", "enhancement", "priority:high")
- **Assignees**: GitHub usernames (can have multiple)
- **Milestone**: optional grouping with a due date
- **Projects (v2)**: GitHub's newer project boards add custom fields, status columns, and sprint-like iterations. If you use these, your agent can read them via `gh project` commands.

The simplicity is a feature. There's less to map. But it also means people encode things in labels that other tools give you as first-class fields ("priority:high" as a label instead of a priority dropdown).

### Step 1: Tell your agent what you want

```
Import all open issues from this repo's GitHub Issues into Lattice.
Use `gh issue list --state open --json number,title,body,state,labels,assignees,milestone,createdAt,updatedAt --limit 500` to read them.

Map them like this:

Labels that encode status -> Lattice status:
  No status label  -> open
  "in-progress"    -> in_progress
  "needs-review"   -> in_review
  "blocked"        -> blocked status, plus set the needs-human flag
                      (lattice needs-human <id> "<reason>") so it lands
                      in the human queue without losing its status

Labels that encode priority -> Lattice priority:
  "priority:critical" -> critical
  "priority:high"     -> high
  "priority:medium"   -> medium
  "priority:low"      -> low
  No priority label   -> medium

All other labels -> Lattice tags (e.g., "bug", "enhancement", "docs").

Assignees: map GitHub username to human:<username>.
Milestone name -> custom field "milestone".
Issue number (e.g., #42) -> custom field "github_issue".
Issue body -> notes file.

After import, for any issue that has "blocked" label AND a comment
mentioning another issue number (like "blocked by #38"), create a
depends_on relationship if #38 was also imported.
```

### Step 2: The agent reads and transforms

Your agent runs `gh issue list` (one command, returns JSON), iterates through the results, and creates Lattice tasks. The key difference from Linear: GitHub doesn't give you a status workflow, so the agent has to infer status from labels. The prompt above makes that mapping explicit.

For a repo with 80 open issues, this takes a couple minutes. The agent will:

1. Run the `gh` command to pull all issues as JSON
2. Parse each issue, separate labels into status-labels, priority-labels, and regular tags
3. Run `lattice create` for each, with the appropriate status and priority
4. Write issue bodies to `.lattice/notes/<task_id>.md`
5. Scan for cross-references in comments and create `depends_on` links
6. Report: "Imported 80 issues. 45 open, 20 in progress, 10 in review, 5 flagged needs-human."

### The field mapping reference

| GitHub field | Lattice target | Notes |
|---|---|---|
| `title` | title | Direct |
| `body` | `.lattice/notes/<id>.md` | Markdown |
| `state` (open/closed) | status | `open` -> `open`, `closed` -> `done` (or use labels for finer granularity) |
| `labels` (status-like) | status | Parse convention: "in-progress", "needs-review", etc. |
| `labels` (priority-like) | priority | Parse convention: "priority:high", "P1", etc. |
| `labels` (everything else) | tags | Direct |
| `assignees[0]` | assigned_to | `human:<github_username>` |
| `milestone.title` | custom field `milestone` | For grouping |
| `number` (e.g., 42) | custom field `github_issue` | For cross-referencing |
| `html_url` | custom field `github_url` | Link back to original |
| `createdAt` | preserved in event metadata | Provenance |

### If you use GitHub Projects (v2)

GitHub Projects add a richer data model on top of issues: custom status columns, iteration fields, priority fields, and more. Your agent can read these too:

```
Also read project board data using `gh project item-list <number> --format json`.
Use the project's Status column instead of inferring from labels:
  "Todo"        -> open
  "In Progress" -> in_progress
  "In Review"   -> in_review
  "Done"        -> done
Use the project's Priority field instead of parsing labels.
```

This gives you the best of both worlds: GitHub's familiar interface for humans, Lattice's agent-native tracking for your AI workforce.

---

## More examples (prompt templates)

The two worked examples above cover 90% of the thinking. For other tools, a prompt template is all you need. Your agent knows these tools.

### Jira

```
Export my Jira board as CSV (I've saved it to ./jira-export.csv).
Parse it and create Lattice tasks. Map Jira statuses like this:
  To Do -> open
  In Progress -> in_progress
  In Review -> in_review
  Done -> done
Preserve the Jira key (e.g., PROJ-123) as a custom field.
```

### Notion database

```
I've exported my Notion task database as CSV (./notion-tasks.csv).
Create a Lattice task for each row. The "Status" column maps to
Lattice statuses. The "Assignee" column maps to human:<name>.
```

### Trello

```
I've exported my Trello board as JSON (./trello-board.json).
Each list maps to a Lattice status:
  To Do -> open
  Doing -> in_progress
  Review -> in_review
  Done -> done
Each card becomes a task. Card descriptions go to notes files.
```

---

