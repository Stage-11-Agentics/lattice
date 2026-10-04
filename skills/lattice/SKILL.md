---
name: lattice
description: Event-sourced task tracking for agents and humans. Use when managing tasks, tracking work status, coordinating multi-agent workflows, or maintaining an audit trail of who did what and when.
homepage: https://github.com/Stage-11-Agentics/lattice
metadata: {"openclaw":{"emoji":"clipboard","requires":{"bins":["lattice"]},"install":[{"id":"pip","kind":"command","command":"pip install lattice-tracker","bins":["lattice"],"label":"Install Lattice (pip)"},{"id":"pipx","kind":"command","command":"pipx install lattice-tracker","bins":["lattice"],"label":"Install Lattice (pipx)"},{"id":"uv","kind":"command","command":"uv tool install lattice-tracker","bins":["lattice"],"label":"Install Lattice (uv)"}]}}
---

# Lattice — Agent-Native Task Tracker

Lattice is a file-based, event-sourced task tracker. It stores everything in a `.lattice/` directory in the project root (like `.git/` for version control). Every change is an immutable event. No accounts, no API keys, no network required.

## When to Use Lattice

Use Lattice when the user or the conversation involves:

- Creating, tracking, or managing tasks
- Coordinating work across multiple agents
- Maintaining an audit trail of decisions and changes
- Planning sprints, releases, or project milestones
- Checking what work is in progress, blocked, or done

## Setup

Check if Lattice is available and initialized:

```bash
bash {baseDir}/scripts/lattice-check.sh
```

Or simply run `lattice list`. If `lattice` is not found, it needs to be installed (see the install methods in the frontmatter above). If `.lattice/` is not found, initialize it:

```bash
lattice init --project-code PROJ
```

Replace `PROJ` with a short project code (e.g., `APP`, `API`, `WEB`). This creates the `.lattice/` directory.

## Core Commands

### Create a task

```bash
lattice create "Fix the login bug" --actor agent:openclaw --priority high
```

Options: `--priority` (critical/high/medium/low/none), `--type` (task/bug/chore), `--description "details"`, `--assign agent:openclaw`

**No built-in epic or spike type, and never a fake epic.** New boards allow `task`, `bug`, and
`chore`, and custom task types can be configured per board. You must never
create an umbrella task to stand in for an epic, even if a board has a custom
`epic` type. On a local board, add a custom type to `.lattice/config.json`
`task_types` while preserving its existing values. On a hosted board, ask an
admin to replace the list with the command
`lattice server project config <slug> --set 'task_types=[...]'` and keep `task`.
Group related tasks with a shared tag
(`lattice create "..." --tags auth,v2`, then `lattice list --tag auth`), and
order them with dependencies (`lattice link <later> depends_on <earlier>`).
Express exploratory work as a plain `task` whose deliverable is a concrete
artifact (plan doc, prototype, decision). Every ticket is a chunk of work
with a real output, not a bucket or an open question.

### List tasks

```bash
lattice list                           # All active tasks
lattice list --status in_progress      # Filter by status
lattice list --assigned agent:openclaw # Filter by assignee
lattice list --priority high           # Filter by priority
lattice list --json                    # Structured output
```

### Update task status

```bash
lattice status PROJ-1 in_progress --actor agent:openclaw
lattice status PROJ-1 done --actor agent:openclaw
```

### Assign a task

```bash
lattice assign PROJ-1 agent:openclaw --actor agent:openclaw
```

### Add a comment

```bash
lattice comment PROJ-1 "Found the root cause: race condition in auth middleware" --actor agent:openclaw
```

### Show task details

```bash
lattice show PROJ-1              # Summary view
lattice show PROJ-1 --events     # With full event history
```

### Link related tasks

```bash
lattice link PROJ-1 blocks PROJ-2 --actor agent:openclaw
lattice link PROJ-3 subtask_of PROJ-1 --actor agent:openclaw
```

Relationship types: `blocks`, `blocked_by`, `subtask_of`, `parent_of`, `depends_on`, `depended_on_by`, `related_to`

### File-decision links

Record which files embody a task's architectural decisions:

```bash
lattice file-link PROJ-1 src/auth/jwt.ts --reason "JWT validation logic" --actor agent:openclaw
lattice file-unlink PROJ-1 src/auth/jwt.ts --actor agent:openclaw
```

Reverse lookup — show what decisions shaped a file:

```bash
lattice explain src/auth/jwt.ts              # exact file
lattice explain src/auth/                    # directory prefix
lattice explain "src/auth/*.ts"              # glob
```

Link files that embody **decisions**, not every file touched. Use `--reason` to annotate why.

### Archive completed work

```bash
lattice archive PROJ-1 --actor agent:openclaw
```

### Get next task to work on

```bash
lattice next --actor agent:openclaw          # Suggest next task
lattice next --actor agent:openclaw --claim  # Suggest and auto-assign
```

On a workflow with the complete plan-review route (both direct edges
`backlog → in_planning` and `in_planning → planned`), every backlog claim stops
in `in_planning`, even when a substantive plan already exists; reclaiming an
`in_planning` task keeps it there. Use the returned
`lattice status <task> planned` command; it includes the identity option supplied
for the claim when present. Write a missing or scaffold plan first, then run it; with an
existing plan, run it directly. Follow its output about review, then explicitly run
`lattice status <task> in_progress` before work. Do not re-claim a task already
held in `in_planning` or `planned`. Other workflow configurations keep their
existing claim behavior.

If `next --claim` selects a `planned` task with a live plan-review gate, it
returns `claimed: false` with reason `PLAN_REVIEW_IN_FLIGHT`, leaves that task
planned and preserves its current assignee, and does not fall through to
another task.
Plain output names the no-claim reason; `--json` returns the selected task
snapshot with `claimed: false` and the reason; `--quiet` prints nothing to
stdout and reports the reason on stderr. Retrying returns the same selected
task until its plan review lands.

### Project health

```bash
lattice weather    # Daily digest / weather report
lattice stats      # Project statistics
lattice doctor     # Check data integrity
```

## Status Workflow

```
backlog → in_planning → planned → in_progress → review → done
                                       ↕
                                    blocked
```

- `backlog` — work identified but not started
- `in_planning` — actively being planned or specced
- `planned` — plan is ready, waiting to start
- `in_progress` — actively being worked on
- `review` — implementation done, under review
- `done` — complete
- `blocked` — waiting on an external dependency
- `cancelled` — abandoned

Transitions are enforced. Use `--force --reason "..."` to override when needed.

### Needs-human flag

`needs-human` is a flag, not a status — it rides orthogonally on whatever status a task is in. Set it when you need a human decision, approval, or input; the task keeps its current status.

```bash
lattice needs-human PROJ-1 "Which OAuth provider should we use?" --actor agent:openclaw
lattice needs-human PROJ-1 --clear --note "Decided: Google" --actor agent:openclaw
lattice list --needs-human          # scannable queue of flagged tasks across all statuses
```

A reason is required when setting the flag. Use `blocked` (a status) for generic external dependencies; use the `needs-human` flag for "waiting on a human specifically." A task can be both at once.

## Actor IDs

Every command requires `--actor` to identify who made the change. Format: `prefix:identifier`

- `agent:openclaw` — for your own actions
- `agent:openclaw-worker-1` — for multi-agent setups
- `human:username` — when acting on behalf of a human

Always use `agent:openclaw` as your actor ID unless the user specifies otherwise.

## Task IDs

Tasks have two forms:
- **Short ID:** `PROJ-1`, `PROJ-42` (use these in conversation)
- **Full ULID:** `task_01HQ...` (internal, always accepted)

Short IDs require a project code (set during `lattice init`).

## Structured Output

All commands support `--json` for machine-readable output:

```bash
lattice list --json
```

Returns `{"ok": true, "data": [...]}` on success or `{"ok": false, "error": {"code": "...", "message": "..."}}` on failure.

## Reading review artifacts

When a review prints an artifact ID, read its content with `lattice artifact show <id>`; use `--json` for structured output. In JSON, `data.content` is UTF-8 text or `null`, and `data.payload_path` is relative to `.lattice`. Binary payloads have null content and include their path. Use `lattice review-status <task>` while a single-mode review runs; progress is reported on stderr.

## Plans and Notes

Every task has a plan at `.lattice/plans/<task_id>.md` (scaffolded on creation) and may have notes at `.lattice/notes/<task_id>.md`. Read them there; write them with a command, which works on every board, including a hosted checkout whose `.lattice/` is a read-only mirror:

```bash
lattice plan LAT-42                                             # Read the plan (--json for structured)
lattice plan write LAT-42 --file plan.md --actor agent:claude   # Replace the plan
lattice notes write LAT-42 --stdin --actor agent:claude < notes.md
lattice plan write LAT-42 --file plan.md --expect-sha256 <hex> --actor agent:claude  # Refuse if it changed
```

Each write records a `plan_written` / `notes_written` event with the content's SHA-256 and size. Orchestrator working files go through `lattice board write orchestration/<path> --file <path>`, and the board's `context.md` through `lattice context write --file <path>`.

## Hosted Boards

A checkout with a committed `.lattice-remote.json` is bound to a Lattice server (Lattice v2, optional). Its board lives on the server; `.lattice/` is a read-only mirror, refreshed before every read.

- Every command works the same, with the same output and error codes. Writes go to the server; reading `.lattice/` files still works. A linked worktree has only `.lattice-remote.json`; the read-only mirror is the primary checkout's `.lattice/`.
- `lattice remote status` shows the token's person; you still pass `--actor agent:<your-id>`. Every event records both your actor and the token's user and machine (`lattice show <task> --full`).
- Whether a status change fires an automatic review depends on the board's config; the `lattice status` output says what happened. `plan-review` and `code-review` run by hand need `--actor`.
- Work not merged through a PR goes `review -> done` with `lattice complete`; say in the review how it was integrated (commit SHA and branch).
- **Never edit files under `.lattice/`.** Write plans and notes with `lattice plan write` / `lattice notes write`, the board's context with `lattice context write`, and orchestration files with `lattice board write orchestration/<path>`.
- **`OUTCOME_UNKNOWN`** means the server may have applied the write. Run `lattice remote op-status <op_id>` (the message names it) before retrying: `committed` means do not run it again.
- `SERVER_UNREACHABLE` ("server ... is not available. Nothing was written") means nothing was written; there is no offline queue. The write shows its retries on stderr for up to 15 s; after that, further writes in the same outage fail at once, so do not loop on them: tell the human the server is down and continue with other work. Reads keep working from the cache with a one-line notice.
- `LOCAL_ONLY` (from `rebuild`, `doctor --fix`, and similar) means the command runs on the server host, not here.
- Setting up a server, binding a checkout, or moving a board to a server and back: follow `docs/hosted/guide.md` in the Lattice repository step by step. It is written for you, the agent: run its command blocks in order and check each result. Ask the human only for what the guide says only they have (the server URL, a token, which project).

## Issue Log (optional)

Some boards keep an issue log for observations that are not yet commitments: a flaky test, a layout glitch, a confusing error outside your task. File one instead of creating a task:

```bash
lattice issue file "Footer overlaps the CTA at 400px" --description "At 400px the footer covers the Sign up button." --actor agent:<id> --confidence definite --evidence screens/footer.png
lattice issue file - --actor agent:<id> < note.md     # first line is title; remaining lines become description
```

Observations go to `lattice issue file`; commitments go to `lattice create`. Discuss an issue with `lattice issue comment <issue> "<text>" --actor agent:<id>`; `lattice issue show <issue>` displays the thread. Triage later with `lattice issue promote` (a new backlog task), `lattice issue link` (an existing task), `lattice issue dismiss --reason`, or `lattice issue duplicate --of`. An issue's state follows its linked tasks. `lattice issue list` shows what is still open; `--by <actor>` finds issues that actor filed or commented on, matching full prefixed keys and bare names.

Pass screenshots and recordings as `--evidence <file>`: photos and videos are copied into the issue (`lattice issue attach <issue> <file>...` adds more later). Read an issue's media with `lattice issue media <issue> --paths` (videos as still frames).

From a service or box with no bound checkout, file through the hosted `issue.file` API with a filing-only token, its bound `source`, and a stable `source_ref`:

```bash
curl -sS -X POST "$LATTICE_URL/v1/projects/<slug>/ops/issue.file" -H "Authorization: Bearer $LATTICE_TOKEN" -H 'Content-Type: application/json' -d '{"actor":"agent:<id>","params":{"title":"<title>","source":"<bound source>","source_ref":"<stable id>"}}'
```

The receipt's `deduplicated` is `false` for a new issue and `true` when the same source/reference returns its original receipt; it does not update that issue. See `docs/hosted/guide.md` → Filing-only issue token for token setup and media uploads.

If a command answers `ISSUES_DISABLED`, the log is off on this board. Do not turn it on yourself: record the observation as a comment on the task you are working, or tell the human.

## Multi-Agent Coordination

Lattice handles concurrent writes safely with file locks. Multiple agents can work simultaneously:

1. Each agent uses a unique actor ID (`agent:openclaw-1`, `agent:openclaw-2`)
2. Create tasks from the orchestrator, assign to workers
3. Workers update status and add comments as they progress
4. Lock-based concurrency prevents file corruption
5. Event log provides full audit trail of who did what

For detailed multi-agent patterns, read `{baseDir}/references/multi-agent-guide.md`.

## Tips

- **Update status before starting work**, not after. If you're about to implement something, move it to `in_progress` first.
- **Leave comments** explaining what you tried, what you chose, and what you left undone. The next agent has no hallway to find you in.
- **Use `lattice next`** to find the highest-priority unblocked task.
- **Use `lattice needs-human`** when you need a human decision — it flags the task (leaving its status intact) and creates a clear queue via `lattice list --needs-human`.
