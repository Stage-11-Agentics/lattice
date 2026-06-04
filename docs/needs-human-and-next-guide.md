# User Guide: the `needs-human` flag, `lattice next`, and Advance

This guide covers three features that work together to create a smooth human-agent coordination loop:

1. **The `needs-human` flag** — Agents signal when they're waiting on you
2. **`lattice next`** — Deterministic task selection for agents
3. **The advance pattern** — One unit of forward progress (driven by `/lattice`)

---

## 1. The `needs-human` Flag

### What it is

`needs-human` is a flag that means: "This task is waiting specifically for a human decision, approval, or input." It is **orthogonal to status** — the flag rides on top of whatever status a task is in. A task can be `in_progress` and flagged, `blocked` and flagged, even `done` and flagged. The flag never moves the task.

It's distinct from `blocked` (a status, for generic external dependencies). `needs-human` is "waiting on a human specifically," and the two can coexist: a task can be `blocked` on a third-party API *and* flagged for a human decision at the same time. The flag creates an explicit queue of things waiting on *you*, regardless of where each task sits in the workflow.

The flag stores who raised it, why, and when:

```json
{
  "needs_human": {
    "flagged_by": "agent:claude-cli",
    "reason": "Which OAuth provider should we support?",
    "since": "2026-06-03T14:22:00Z"
  }
}
```

When the flag is absent, the field is `null` (or omitted entirely).

### When agents use it

Agents flag a task when they hit a point requiring human judgment:

- Design decisions ("Should we use REST or GraphQL?")
- Missing access or credentials
- Ambiguous requirements that can't be resolved from context
- Approval needed before proceeding (deploy, merge, etc.)

### Setting and clearing the flag

```bash
# Flag a task — the reason is REQUIRED. The task keeps its current status.
lattice needs-human LAT-42 "Which OAuth provider should we support?" --actor agent:claude-cli

# Clear the flag once you've provided what was needed (--note is optional)
lattice needs-human LAT-42 --clear --note "Decided: Google + GitHub" --actor human:atin
```

Requiring a reason structurally enforces what used to be a soft convention: the queue is always scannable because every flagged task carries an explanation. Setting the flag emits a `needs_human_flagged` event; clearing it emits `needs_human_cleared`. Both are fully attributed in the task event log. Both commands support `--json`.

### How to see what needs you

```bash
# Weather report highlights flagged items, keyed off the needs-human flag
lattice weather

# The scannable queue: flagged tasks across ALL statuses, with reasons
lattice list --needs-human

# lattice show renders a prominent NEEDS HUMAN line on a flagged task
lattice show LAT-42

# Dashboard surfaces the flag distinctly from red blocked
lattice dashboard
```

`lattice list --needs-human` is the queue. Because the flag is orthogonal to status, this lists everything waiting on you whether it's in planning, in progress, blocked, or otherwise — something a single status column could never capture.

### Resolving a flagged task

Provide what the agent needed, record the decision, then clear the flag. The task's status is untouched, so work resumes wherever it left off:

```bash
lattice comment LAT-42 "Decision: use REST. Rationale in notes." --actor human:atin
lattice needs-human LAT-42 --clear --note "Use REST" --actor human:atin
```

The agent picks the task back up on its next advance — there's no status to "move it back" from, because it never left its column.

---

## Migrating from the `needs_human` status

Earlier Lattice modeled needs-human as a workflow *status* rather than a flag. Instances created under that model keep working until you run the migration, which is idempotent and safe to run twice:

```bash
# Preview what would change without writing anything
lattice migrate needs-human --dry-run

# Apply
lattice migrate needs-human
```

The migration:

1. **Flags** every task currently sitting in the `needs_human` status. The reason is taken from the task's latest comment, falling back to `"Migrated from needs_human status"`.
2. **Routes** each such task back to the status it was in *before* it entered `needs_human` (fallback: `backlog`).
3. **Strips** `needs_human` from the project's workflow config (statuses, transitions, and universal targets).

After migration, `lattice list --status needs_human` no longer applies — use `lattice list --needs-human` instead.

---

## 2. `lattice next` — Task Selection

### What it does

Returns the single highest-priority task an agent should work on next. This is the building block for autonomous workflows — agents don't need to manually scan, filter, and sort the backlog.

### Basic usage

```bash
# What should I work on? (read-only)
lattice next
# Output: LAT-7  backlog  critical  "Fix authentication timeout"

# JSON output for programmatic use
lattice next --json
# Output: {"ok": true, "data": {"id": "task_01...", "title": "...", ...}}

# Just the ID
lattice next --quiet
# Output: LAT-7
```

### Actor-aware selection

```bash
# Filter by who's asking — excludes tasks assigned to others
lattice next --actor agent:claude-cli

# Resume-first: if you have in_progress work, it returns that first
# (agents should finish what they started before picking new work)
lattice next --actor agent:claude-cli --json
```

### Claiming a task

The `--claim` flag atomically assigns the task and moves it to `in_progress`:

```bash
# Claim in one step (requires --actor)
lattice next --actor agent:claude-cli --claim --json
```

This is equivalent to running `lattice assign` + `lattice status` but atomic — no race condition where another agent grabs the same task.

### Selection algorithm

1. **Resume first:** If `--actor` is specified, check for `in_progress` or `in_planning` tasks assigned to that actor. Return the highest-priority one. (Don't abandon work.)

2. **Pick from ready pool:** Tasks in `backlog` or `planned` status, either unassigned or assigned to the requesting actor. Excludes `blocked`, `done`, `cancelled` — and skips any task carrying the `needs-human` flag, in any status (a flagged task is waiting on a human, not on an agent).

3. **Sort by:**
   - Priority: `critical` > `high` > `medium` > `low`
   - Urgency: `immediate` > `high` > `normal` > `low`
   - Age: oldest task first (by ULID)

4. **Return** the top result, or null if nothing is available.

### Custom status pools

Override which statuses to consider:

```bash
# Only look at planned tasks (skip backlog)
lattice next --status planned --json

# Look at tasks in review (e.g., for a reviewer agent)
lattice next --status review --actor agent:reviewer --json
```

### When `next` returns nothing

If `lattice next` returns `null` / "No tasks available", it means:
- The backlog is empty, OR
- All remaining tasks are assigned to other agents, OR
- All tasks are in excluded states (done, blocked, cancelled) or carry the `needs-human` flag

Check `lattice list` to see what's actually in the system.

---

## 3. The Advance Pattern — One Unit of Forward Progress

### What it is

The advance is the core lifecycle pattern in Lattice. The agent claims the highest-priority ready task, does the work, transitions it, and reports what happened. One task, one advance. The `/lattice` skill teaches agents this full lifecycle.

### How to use it

```
/lattice
```

That's it. The `/lattice` skill teaches the agent the full lifecycle, including how to claim the next task and work it to completion (or to a hand-off point — flagging it `needs-human` or moving it to `blocked`).

For multiple advances, just invoke it again or tell the agent "do 3 advances" or "keep advancing until blocked."

### What it does (the protocol)

1. **Claim:** `lattice next --actor agent:claude-cli --claim --json`
2. **Read:** Examine the task details and any notes/plans
3. **Work:** Implement, test, iterate — full coding agent capabilities
4. **Hand off:** Move the task to `review` or `blocked`, or raise the `needs-human` flag, depending on outcome
5. **Comment:** Record what was done, what was chosen, what's left
6. **Commit:** Commit changes
7. **Report:** Tell you what happened — task, outcome, summary

### When to use it

- You have a backlog of well-defined tasks and want an agent to make progress
- You want to control the pace — one advance at a time, or several in sequence
- Tasks are independent enough to be worked sequentially

### What it won't do

- Work on `needs-human`-flagged tasks (those are waiting on you), in any status
- Force invalid status transitions
- Push code (commits locally, you review and push)

### Post-advance workflow

After an advance, you'll typically:

1. Read the agent's report to see what was done
2. Check `lattice list --status review` for tasks awaiting your review
3. Check `lattice list --needs-human` for decisions only you can make
4. Run tests / review code
5. Merge, push, or send tasks back for rework

### Example session

```bash
# Check the backlog
lattice weather

# Advance by one task (invoke /lattice to teach the agent, then ask it to advance)
/lattice

# Check what needs you
lattice list --needs-human
lattice list --status review

# Address flagged items: answer, then clear the flag (status is untouched)
lattice comment LAT-15 "Approved: use the proposed schema" --actor human:atin
lattice needs-human LAT-15 --clear --note "Approved schema" --actor human:atin

# Advance again
/lattice
```

---

## Putting It All Together

The three features form a coordination loop:

```
   ┌─────────────────────────────────────────┐
   │                                         │
   │  AGENT (advance)                        │
   │  ┌──────────────────────┐               │
   │  │ lattice next --claim │               │
   │  │ → work on task       │               │
   │  │ → review / done      │───────────┐   │
   │  │ → flag needs-human   │──┐        │   │
   │  │ → blocked            │  │        │   │
   │  └──────────────────────┘  │        │   │
   │           ↑                │        │   │
   │           └────────────────│────────┘   │
   │                            │            │
   └────────────────────────────│────────────┘
                                │
                                ↓
   ┌────────────────────────────────────────┐
   │                                        │
   │  HUMAN                                 │
   │  ┌─────────────────────────┐           │
   │  │ lattice list            │           │
   │  │   --needs-human         │           │
   │  │ → makes decisions       │           │
   │  │ → needs-human --clear   │───────┐   │
   │  │   (status untouched)    │       │   │
   │  └─────────────────────────┘       │   │
   │                                    │   │
   └────────────────────────────────────│───┘
                                        │
                                        ↓
                              Agent picks up
                              unblocked task
                              on next advance
```

The human's job is to:
1. Define work (create tasks with clear titles and descriptions)
2. Prioritize (set priority/urgency so `next` picks the right thing)
3. Unblock (address flagged items promptly, then `needs-human --clear`)
4. Review (check `review` status tasks and approve or send back)

The agent's job is to:
1. Claim and work tasks (`lattice next --claim`)
2. Signal when stuck (`lattice needs-human <task> "<reason>"`)
3. Leave breadcrumbs (comments, notes)
4. Complete or hand off every task it touches
