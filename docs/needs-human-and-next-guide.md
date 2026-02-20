# User Guide: `needs_human`, `lattice next`, and Advance

This guide covers three features that work together to create a smooth human-agent coordination loop:

1. **`needs_human` status** — Agents signal when they're blocked on you
2. **`lattice next`** — Deterministic task selection for agents
3. **The advance pattern** — One unit of forward progress (driven by `/lattice`)

---

## 1. The `needs_human` Status

### What it is

A workflow status that means: "This task is waiting specifically for a human decision, approval, or input." It's distinct from `blocked` (generic external dependency) — `needs_human` creates an explicit queue of things waiting on *you*.

### When agents use it

Agents move a task to `needs_human` when they hit a point requiring human judgment:

- Design decisions ("Should we use REST or GraphQL?")
- Missing access or credentials
- Ambiguous requirements that can't be resolved from context
- Approval needed before proceeding (deploy, merge, etc.)

When moving to `needs_human`, the agent is required to leave a comment explaining what they need. This keeps your queue scannable.

### How to see what needs you

```bash
# Weather report highlights needs_human items with [HUMAN] tag
lattice weather

# Filter the task list directly
lattice list --status needs_human

# Dashboard shows needs_human in amber/orange (distinct from red blocked)
lattice dashboard
```

### Unblocking a task

After providing what the agent needed, move the task back to an active status:

```bash
# Resume planning
lattice status LAT-42 in_planning --actor human:atin
lattice comment LAT-42 "Decision: use REST. Rationale in notes." --actor human:atin

# Or skip ahead to in_progress
lattice status LAT-42 in_progress --actor human:atin
```

### Workflow transitions

```
                    in_planning ──→ needs_human
                    planned ──────→ needs_human
                    in_progress ──→ needs_human
                    review ───────→ needs_human

needs_human ──→ in_planning
needs_human ──→ planned
needs_human ──→ in_progress
needs_human ──→ review
needs_human ──→ cancelled
```

`needs_human` is NOT reachable from `backlog` (work hasn't started) or `done`/`cancelled` (terminal).

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

2. **Pick from ready pool:** Tasks in `backlog` or `planned` status, either unassigned or assigned to the requesting actor. Excludes `needs_human`, `blocked`, `done`, `cancelled`.

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
- All tasks are in excluded states (done, blocked, needs_human, cancelled)

Check `lattice list` to see what's actually in the system.

---

## 3. The Advance Pattern — One Unit of Forward Progress

### What it is

The advance is the core lifecycle pattern in Lattice. The agent claims the highest-priority ready task, does the work, transitions it, and reports what happened. One task, one advance. The `/lattice` skill teaches agents this full lifecycle.

### How to use it

```
/lattice
```

That's it. The `/lattice` skill teaches the agent the full lifecycle, including how to claim the next task and work it to completion (or to a transition point like `needs_human` or `blocked`).

For multiple advances, just invoke it again or tell the agent "do 3 advances" or "keep advancing until blocked."

### What it does (the protocol)

1. **Claim:** `lattice next --actor agent:claude-cli --claim --json`
2. **Read:** Examine the task details and any notes/plans
3. **Work:** Implement, test, iterate — full coding agent capabilities
4. **Transition:** Move the task to `review`, `needs_human`, or `blocked` depending on outcome
5. **Comment:** Record what was done, what was chosen, what's left
6. **Commit:** Commit changes
7. **Report:** Tell you what happened — task, outcome, summary

### When to use it

- You have a backlog of well-defined tasks and want an agent to make progress
- You want to control the pace — one advance at a time, or several in sequence
- Tasks are independent enough to be worked sequentially

### What it won't do

- Work on `needs_human` tasks (those are waiting on you)
- Force invalid status transitions
- Push code (commits locally, you review and push)

### Post-advance workflow

After an advance, you'll typically:

1. Read the agent's report to see what was done
2. Check `lattice list --status review` for tasks awaiting your review
3. Check `lattice list --status needs_human` for decisions only you can make
4. Run tests / review code
5. Merge, push, or send tasks back for rework

### Example session

```bash
# Check the backlog
lattice weather

# Advance by one task (invoke /lattice to teach the agent, then ask it to advance)
/lattice

# Check what needs you
lattice list --status needs_human
lattice list --status review

# Address needs_human items
lattice status LAT-15 in_progress --actor human:atin
lattice comment LAT-15 "Approved: use the proposed schema" --actor human:atin

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
   │  │ → needs_human        │──┐        │   │
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
   │  │ lattice weather         │           │
   │  │ → sees [HUMAN] items    │           │
   │  │ → makes decisions       │           │
   │  │ → lattice status → back │───────┐   │
   │  │   to active             │       │   │
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
3. Unblock (address `needs_human` items promptly)
4. Review (check `review` status tasks and approve or send back)

The agent's job is to:
1. Claim and work tasks (`lattice next --claim`)
2. Signal when stuck (`needs_human` + comment)
3. Leave breadcrumbs (comments, notes)
4. Complete or transition every task it touches
