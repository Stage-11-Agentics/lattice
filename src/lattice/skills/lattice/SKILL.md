---
name: lattice
description: Lattice agent coordination guide — mental model, CLI commands, lifecycle discipline, and reference for file-based task tracking across agents and sessions.
homepage: https://github.com/Stage-11-Agentics/lattice
metadata: {"openclaw":{"emoji":"clipboard","requires":{"bins":["lattice"]},"install":[{"id":"pip","kind":"command","command":"pip install lattice-tracker","bins":["lattice"],"label":"Install Lattice (pip)"},{"id":"pipx","kind":"command","command":"pipx install lattice-tracker","bins":["lattice"],"label":"Install Lattice (pipx)"},{"id":"uv","kind":"command","command":"uv tool install lattice-tracker","bins":["lattice"],"label":"Install Lattice (uv)"}]}}
---

# Lattice — Agent-Native Task Tracker

Lattice is a file-based, event-sourced task tracker. Everything lives in `.lattice/` in the project root. Every change is an immutable event. No accounts, no API keys, no network required.

## The Lifecycle — Opening and Closing Rituals

Every unit of work follows this arc: **claim → understand → work → complete**. The opening and closing rituals are non-negotiable.

### Opening Ritual

```bash
# 1. Claim the next task (or create one)
lattice next --actor agent:claude-cli --claim --json

# 2. If the claim result is in_planning, inspect the task and plan it
lattice plan write <task_id> --file plan.md --actor agent:claude-cli   # only if missing/scaffold; or --stdin
lattice status <task_id> planned --actor agent:claude-cli            # also run when a substantive plan exists
# Follow the status output about plan review, then explicitly advance:
lattice status <task_id> in_progress --actor agent:claude-cli

# 3. Working on a branch or worktree? Link it — BEFORE you reach review.
lattice branch-link <task_id> <branch> --actor agent:claude-cli
```

`lattice next --claim` atomically assigns the highest-priority ready task. On the complete plan-review route (both direct edges `backlog → in_planning` and `in_planning → planned`), a backlog claim stops in `in_planning`, even when a substantive plan already exists; reclaiming a task in `in_planning` keeps it there. Use the returned `lattice status <task> planned` command; it includes the identity option supplied for the claim when present. Write a missing or scaffold plan first; otherwise run it directly. Follow its output about plan review, then explicitly run `lattice status <task> in_progress` before working. Do not re-claim a task already held in `in_planning` or `planned`. If the claim returns `in_progress`, continue under that workflow's existing behavior.

If `next --claim` selects a `planned` task with a live plan-review gate, it returns `claimed: false` with reason `PLAN_REVIEW_IN_FLIGHT`, leaves that task planned and preserves its current assignee, and does not fall through to another task. Plain output names the no-claim reason; `--json` returns the selected task snapshot with `claimed: false` and the reason; `--quiet` prints nothing to stdout and reports the reason on stderr. Retrying returns the same selected task until its plan review lands.

**Link the branch, or review reads the wrong code.** `code-review` resolves its diff from the task's linked branch — authoritative, and a hard error if the branch does not resolve rather than a quiet fall back to whatever the checkout holding `.lattice/` has checked out. Under one-worktree-per-task that would be a *sibling's* branch. `--head`/`--base`/`--worktree` override the resolution; `code-review <task> --dry-run` prints the resolved range (and `--json` makes it assertable) without spending a model run.

If there's no existing task, create one:

```bash
lattice create "Fix the login bug" --actor agent:claude-cli --priority high
```

### One Review Owner Per Gate Cycle

Use the reviewer auto-fired by the `→ planned`/`→ review` transition or one manually spawned fresh-context reviewer, never both. For a manual owner, put `--no-auto-review` on that status transition before spawning the reviewer:

```bash
lattice status <task> planned --no-auto-review --actor agent:<id>
lattice status <task> review --no-auto-review --actor agent:<id>
```

### Closing Ritual

**`lattice complete` is THE way to finish work.** It performs the full completion ceremony in one command: posts a review comment, attaches a review artifact, transitions through review to done.

```bash
lattice complete <task_id> --review "What was done. Key decisions. Test results. What remains." --actor agent:claude-cli
lattice complete <task_id> --review-file review.md --actor agent:claude-cli   # multi-paragraph review
lattice complete <task_id> --review-file review.md --via https://git.example.com/org/repo/pull/42 --actor agent:claude-cli
```

Use `--via` to record the primary task ID (the ticket carrying this bundled work), an ASCII `#N` pull request number, or a printable ASCII HTTP(S) pull request URL. It adds a canonical task or pull-request object to the final completion event; it does not add a task relationship or infer a branch. It only permits the initial move to `review` from a nonterminal status whose workflow has no direct edge. The normal review evidence and completion policies remain required.

If `require_reachable_review_commit` is enabled, `--via` does not supply reachability. Link the completed task's own branch with `lattice branch-link <task> <primary-branch>`, and run `complete` from a checkout whose `HEAD` is an ancestor of that still-existing branch. The link is necessary but not sufficient; finish before deleting the branch. A bundled task's branch is never borrowed.

The `--review` text is your breadcrumb for every future agent and human who reads this task. Be specific: files changed, approach taken, edge cases considered, anything left undone.

**Long prose goes in a file, not in quotes.** `--review-file` on `complete`; `--file` on `comment`, `comment-edit`, and `needs-human`. Inside a double-quoted shell argument, backticks and `$(...)` are command substitution — that has silently eaten a clause from one comment and spliced 15 KB of pytest output into another. A file is read byte-for-byte.

**Do not use raw `lattice status ... done` to finish work.** The `complete` command exists because completion requires evidence — a review comment and artifact. Skipping this ceremony leaves the task without an audit trail.

| Outcome | Action |
|---------|--------|
| **Done** | `lattice complete <task_id> --review "..." --actor agent:claude-cli` |
| **Need human input** | `lattice needs-human <task_id> "<what you need>" --actor agent:claude-cli` (the task keeps its status — the flag is orthogonal) |
| **Blocked on dependency** | `lattice status <task_id> blocked --actor agent:claude-cli` + comment explaining the blocker |

## The Work In Between

Between opening and closing:

1. **Read before writing.** `lattice show <task_id> --json`. Check `.lattice/plans/<task_id>.md` and `.lattice/notes/<task_id>.md` for context from previous minds. Write them with `lattice plan write` / `lattice notes write <task_id> --file <path>`, never by editing the files: the commands work on every board, including a hosted checkout whose `.lattice/` is a read-only mirror.
2. **Check previous work.** If the task has prior events, investigate what happened. `git log --oneline --grep="<short_id>"` for prior commits.
3. **Baseline tests.** Run the test suite before changing anything. You own new failures, not pre-existing ones.
4. **Commit as you go.** Each meaningful unit of progress gets a commit.
5. **Push when done.** Each task is durable on the remote immediately.

## Core Commands

```bash
# Create
lattice create "Title" --actor agent:claude-cli --priority high --type bug

# List
lattice list                           # All active tasks
lattice list --status in_progress      # Filter by status
lattice list --assigned agent:claude-cli

# Show
lattice show PROJ-1                    # Summary
lattice show PROJ-1 --events           # Full event history

# Status transitions
lattice status PROJ-1 in_progress --actor agent:claude-cli

# Complete (the closing ritual)
lattice complete PROJ-1 --review "Review text" --actor agent:claude-cli

# Assign
lattice assign PROJ-1 agent:claude-cli --actor agent:claude-cli

# Comment (--file for anything long — a quoted arg runs backticks/$() as shell)
lattice comment PROJ-1 "Found root cause" --actor agent:claude-cli
lattice comment PROJ-1 --file findings.md --actor agent:claude-cli

# Link
lattice link PROJ-1 blocks PROJ-2 --actor agent:claude-cli

# Flag for human attention (orthogonal to status — task keeps its current status)
lattice needs-human PROJ-1 "Which OAuth provider?" --actor agent:claude-cli
lattice needs-human PROJ-1 --clear --note "Decided: use Google" --actor agent:claude-cli
lattice list --needs-human          # scannable queue of flagged tasks, any status

# Next task
lattice next --actor agent:claude-cli --claim --json

# File-decision links
lattice file-link PROJ-1 src/auth/jwt.ts --reason "JWT validation" --actor agent:claude-cli
lattice file-unlink PROJ-1 src/auth/jwt.ts --actor agent:claude-cli
lattice explain src/auth/jwt.ts              # what decisions shaped this file?
lattice explain src/auth/                    # directory prefix
lattice explain "src/auth/*.ts"              # glob

# Health
lattice weather    # Daily digest
lattice stats      # Statistics
lattice doctor     # Data integrity check

# Archive
lattice archive PROJ-1 --actor agent:claude-cli
```

Options for `create`: `--priority` (critical/high/medium/low/none), `--type` (task/bug/chore), `--description "..."`, `--assign agent:claude-cli`

**No built-in epic or spike type, and never a fake epic.** New boards allow `task`, `bug`, and `chore`, and custom task types can be configured per board. You must never create an umbrella task to stand in for an epic, even if a board has a custom `epic` type. On a local board, add a custom type to `.lattice/config.json` `task_types` while preserving its existing values. On a hosted board, ask an admin to replace the list with `lattice server project config <slug> --set 'task_types=[...]'` and keep `task`. Group related tasks with a shared tag (`lattice create "..." --tags auth,v2`, then `lattice list --tag auth`), and order them with dependencies (`lattice link <later> depends_on <earlier>`). Express exploratory work as a plain `task` whose deliverable is a concrete artifact (plan doc, prototype, decision). Every ticket is a chunk of work with a real output, not a bucket or an open question.

**Task description depth:** Match description detail to task ambiguity. Bug fixes and chores can be one-liners ("Add regex validation to frequency names"). Features and integration tasks should include: (1) what it does, (2) acceptance criteria, (3) architectural context, (4) what the user/operator experiences when done. Structured task-local criterion records are optional; add them when stable IDs and evidence traceability help, not as a universal task or workflow requirement.

**Optional acceptance-criteria operations:**

```bash
lattice criterion add PROJ-1 "OAuth callback returns to the requested page" --id oauth-return --actor agent:claude-cli
lattice criterion edit PROJ-1 oauth-return "OAuth callback returns to the original requested page" --actor agent:claude-cli
lattice criterion list PROJ-1 --history
lattice comment PROJ-1 "Observed a live callback return to /settings." --criterion oauth-return --actor agent:claude-cli
lattice comment-edit PROJ-1 ev_01ABC "Observed a live callback return to /settings." --clear-role --actor agent:claude-cli
lattice criterion retire PROJ-1 oauth-return --actor agent:claude-cli
lattice criterion list PROJ-1 --include-retired
```

Use structured criteria only when stable IDs help. Treat `--criterion` as traceability, not proof: state the observed result in the evidence and follow the task's configured role and completion policy. `--clear-role` removes only the role; it preserves criterion links.

Relationship types for `link`: `blocks`, `blocked_by`, `subtask_of`, `parent_of`, `depends_on`, `depended_on_by`, `related_to`

## Status Workflow

```
backlog → in_planning → planned → in_progress → review → in_validation → pr_open → done
                                       ↕
                                    blocked
```

Transitions are enforced. Use `--force --reason "..."` to override when genuinely needed.

**`needs-human` is a flag, not a status.** It rides orthogonally on top of whatever status a task is in — a task can be `in_progress` and flagged, `blocked` and flagged, even `done` and flagged. Set it with `lattice needs-human <task> "<reason>"` (reason required) and clear it with `lattice needs-human <task> --clear`. The flag never moves the task. `blocked` stays a status for generic external dependencies; `needs-human` means "waiting on a human specifically," and the two can coexist.

**`in_validation` is the e2e gate.** After local review passes, prove the change works against a running system — browser automation for web, simulator MCP for mobile, curl flows for APIs. Exercise the actual flow the ticket touched, then record evidence with `lattice attach <task> --role validation` (or `lattice comment <task> --role validation`). Transitioning to `pr_open` is blocked until validation evidence is recorded; if e2e genuinely doesn't apply, record a one-line N/A justification instead — explicit, never silent. Validation failure routes back to `in_progress` (impl-level) or `in_planning` (plan-level), and counts toward the 3-cycle rework valve. The bar: **"I saw it work," not "I think it should work."**

`--criterion` on a comment or artifact records a traceability link only. It does not prove that criterion passed or was satisfied; state the observed result in the evidence itself and follow the configured role-based validation policy.

## Actor IDs

Every command requires `--actor`. Format: `prefix:identifier`

- `agent:claude-cli` — default for your own actions
- `agent:worker-1`, `agent:worker-2` — multi-agent setups
- `human:username` — when acting on behalf of a human

## Task IDs

- **Short ID:** `PROJ-1`, `PROJ-42` (use these in conversation)
- **Full ULID:** `task_01HQ...` (internal, always accepted)

All commands support `--json` for structured output: `{"ok": true, "data": ...}` or `{"ok": false, "error": {"code": "...", "message": "..."}}`.

## Heartbeat Mode

Check if enabled: look for `"heartbeat": {"enabled": true}` in `.lattice/config.json`.

When enabled, keep advancing after each task. After `lattice next --claim`, if the returned task is `in_planning`, read its details and linked context, write the plan if needed, and use the explicit `status ... planned` command even when a substantive plan already exists. Follow that status output about review, then explicitly move to `in_progress` before work; do not re-claim to advance it. Otherwise continue the existing claim-and-work loop. Stop after `max_advances` (default 10), when the backlog is empty, or when a task is flagged `needs-human` or hits `blocked`.

## Reading review artifacts

When a review prints an artifact ID, read its content with `lattice artifact show <id>`; use `--json` for structured output. In JSON, `data.content` is UTF-8 text or `null`, and `data.payload_path` is relative to `.lattice`. Binary payloads have null content and include their path. Use `lattice review-status <task>` while a single-mode review runs; progress is reported on stderr.

## Hosted Boards

A checkout with a committed `.lattice-remote.json` is bound to a Lattice server (Lattice v2, optional). Its board lives on the server; `.lattice/` is a read-only mirror, refreshed before every read.

- Every command works the same, with the same output and error codes. Writes go to the server; reading `.lattice/` files still works. A linked worktree has only `.lattice-remote.json`; the read-only mirror is the primary checkout's `.lattice/`.
- `lattice remote status` shows the token's person; you still pass `--actor agent:<your-id>`. Every event records both your actor and the token's user and machine (`lattice show <task> --full`).
- Whether a status change fires an automatic review depends on the board's config; the `lattice status` output says what happened. `plan-review` and `code-review` run by hand need `--actor`.
- Work not merged through a PR goes `review -> done` with `lattice complete`; say in the review how it was integrated (commit SHA and branch).
- **Never edit files under `.lattice/`.** Write plans and notes with `lattice plan write` / `lattice notes write`, the board's context with `lattice context write`, and orchestration files with `lattice board write orchestration/<path>`.
- **`OUTCOME_UNKNOWN`** means the server may have applied the write. Run `lattice remote op-status <op_id>` (the message names it) before retrying: `committed` means do not run it again. If op-status itself fails with `SERVER_UNREACHABLE`, the outcome is still unknown: retry the lookup later, never the write.
- `SERVER_UNREACHABLE` ("server ... is not available. Nothing was written") means nothing was written; there is no offline queue. The write shows its retries on stderr for up to 15 s; after that, further writes in the same outage fail at once (behind a gateway that answers 502, 503, or 504 each write retries and ends `OUTCOME_UNKNOWN` instead), so do not loop on them: tell the human the server is down and continue with other work. Reads keep working from the cache with a one-line notice.
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

## Rules

1. **Open before you work.** Every unit of work starts with a Lattice task. Not after. Not during. Before.
2. **Close with `lattice complete`.** Never raw `lattice status ... done`.
3. **One task at a time.** Finish or transition before claiming the next.
4. **Don't force transitions.** If a transition fails, investigate why.
5. **Don't cancel human tasks.** Flag them with `lattice needs-human` instead — let the human decide.
6. **Comment liberally.** The next agent has no hallway to find you in.

## References

Detailed guides live in `{baseDir}/references/`:

- **[Multi-Agent Guide](references/multi-agent-guide.md)** — orchestrator/worker patterns, actor ID conventions, concurrency safety
- **[Worktree Guide](references/worktree-guide.md)** — git worktree protocol for parallel development with shared Lattice state
