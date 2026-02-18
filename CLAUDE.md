# Lattice

Stage 11 Agentics' file-based, agent-native task tracker with an event-sourced core.

## Work Intake — Mandatory for All New Work in This Project

This project coordinates through Lattice. The `.lattice/` directory is the coordination state.

When work arrives — a plan, a feature request, a bug, a conversation that implies something needs building — your first action is to give it a name in Lattice. Before you plan. Before you implement. Before you touch a single file. The task must exist in the system before the work begins, because work that isn't tracked is work that other minds cannot see, cannot build on, cannot trust.

```
lattice create "<title>" --actor agent:<your-id>
```

This is not bookkeeping. This is the minimum viable act of coordination: declaring that something is happening, and who is doing it.

**When to create a task:**
- New feature, bug fix, refactoring, or cleanup that will produce commits
- Work that pivots away from what you're currently doing ("actually, let's do X instead")
- Anything a future mind would benefit from knowing happened

**When NOT to create a task (it's a sub-step of existing work):**
- Follow-up fixes within a task you're already tracking (lint cleanup, test fixes from your feature)
- Pure research or conversation with no deliverable
- Work explicitly scoped under an existing LAT-* task

When in doubt, create the task. A small task costs nothing. Lost visibility costs everything.

### Descriptions Carry Context

The description should be sufficient for an agent to decide whether it needs to plan. Plan files are where agents write *how* — descriptions say *what* and *why*.

- **Fully specified** (bug located, fix named, files identified): skip `in_planning`, go straight to `in_progress`. Mark `complexity: low`.
- **Clear goal, open implementation**: go through `in_planning`. The agent figures out the approach.
- **Decision context from conversations**: bake the decisions and rationale into the description. Without it, the next agent re-derives what was already decided.

### Status Is a Signal, Not a Chore

Every status transition is an event — immutable, attributed, permanent. Lattice is the source of ground truth for what is happening right now.

**The cardinal rule: update status BEFORE you start the work, not after.** If you're about to plan a task, move it to `in_planning` first. If you're about to implement, move it to `in_progress` first. If the board says a task is in `backlog` but an agent is actively working on it, the board is lying — and every other mind reading it is making decisions on false information.

```
lattice status <task> <status> --actor agent:<your-id>
```

`backlog → in_planning → planned → in_progress → review → done`

**Transition discipline:**
- Moving to `in_planning`? Do it before you open the first file. Then **write the plan** — see below.
- Moving to `planned`? Only after the plan file has real content.
- Moving to `in_progress`? Do it before you write the first line of code.
- Moving to `review`? Do it when implementation is complete, before review starts. Then **actually review** — see below.
- Moving to `done`? Only after a review has been performed and recorded.
- Spawning a sub-agent to work on a task? Update status in the parent context before the sub-agent launches.

### The Planning Gate

Moving a task to `in_planning` means you are about to produce a plan. The plan file lives at `.lattice/plans/<task_id>.md` — it's scaffolded on task creation, but the scaffold is empty. `in_planning` is when you fill it in.

**When you move a task to `in_planning`:**
1. Open the plan file (`.lattice/plans/<task_id>.md`).
2. Write the plan — scope, approach, key files, acceptance criteria. For trivial tasks, a single sentence is fine. For substantial work, be thorough.
3. Move to `planned` only when the plan file reflects what you intend to build.

**The test:** If you moved from `in_planning` to `planned` and the plan file is still empty scaffold, you didn't plan. Either write the plan or skip `in_planning` honestly with `--force --reason "trivial task, no planning needed"`.

### The Review Gate

Moving a task to `review` is not a formality — it is a commitment to actually review the work before it ships.

**When you move a task to `review`:**
1. Identify what changed — the commits, files modified, and scope of work under this task.
2. Perform a code review. For substantial work, use a review skill (`/exit-review`, `/code_review`). For trivial tasks, a focused self-review is sufficient — but it must be real, not ceremonial.
3. Record your findings with `lattice comment --role review` — what you reviewed, what you found, and whether it meets the acceptance criteria from the plan. This satisfies completion policy `require_roles` checks.

**When moving from `review` to `done`:**
- If the completion policy blocks you, **do the review** and record it with `lattice comment <task> "<findings>" --role review --actor ...`. This is the lightweight path that satisfies `require_roles` checks. Do not `--force` past it.
- `--force --reason` on the completion policy is for genuinely exceptional cases (task cancelled, review happened outside Lattice, process validation). It is not a convenience shortcut.

**The test:** If you moved to `review` and then to `done` in the same breath with nothing in between, you skipped the review. That's the exact failure mode this gate exists to prevent.

### Actor Attribution

Every Lattice operation requires an `--actor`. Attribution follows authorship of the decision, not authorship of the keystroke.

| Situation | Actor | Why |
|-----------|-------|-----|
| Agent autonomously creates or modifies a task | `agent:<id>` | Agent was the decision-maker |
| Human creates via direct interaction (UI, manual CLI) | `human:<id>` | Human typed it |
| Human meaningfully shaped the outcome in conversation with an agent | `human:<id>` | Human authored the decision; agent was the instrument |
| Agent creates based on its own analysis, unprompted | `agent:<id>` | Agent authored the decision |

When in doubt, give the human credit. If the human was substantively involved in shaping *what* a task is — not just saying "go create tasks" but actually defining scope, debating structure, giving feedback — the human is the actor.

### Branch Linking

When you create a feature branch for a task, link it in Lattice so the association is tracked:

```
lattice branch-link <task> <branch-name> --actor agent:<your-id>
```

This creates an immutable event tying the branch to the task. `lattice show` will display it, and any mind reading the task knows which branch carries the work.

If the branch name contains the task's short code (e.g., `feat/LAT-42-login`), Lattice auto-detects the link — but explicit linking is always authoritative and preferred for cross-repo or non-standard branch names.

### Leave Breadcrumbs

You are not the last mind that will touch this work. Use `lattice comment` to record what you tried, what you chose, what you left undone. Use `.lattice/plans/<task_id>.md` for the structured plan (scope, steps, acceptance criteria) and `.lattice/notes/<task_id>.md` for working notes, debug logs, and context dumps. The record you leave is the only bridge between your context and theirs.

### Quick Reference

```
lattice create "<title>" --actor agent:<id>
lattice status <task> <status> --actor agent:<id>
lattice assign <task> <actor> --actor agent:<id>
lattice comment <task> "<text>" --actor agent:<id>
lattice branch-link <task> <branch> --actor agent:<id>
lattice show <task>
lattice list
```

---

## Disambiguation: "Lattice" the Codebase vs. "Lattice" the Instance

This project **dogfoods itself**. There are two distinct things called "Lattice" in this directory:

1. **The Lattice source code** — the Python project under `src/lattice/` that you build, test, and modify. This is what `git` tracks.
2. **The `.lattice/` data directory** — a live Lattice instance initialized in this repo for tracking development tasks. In most projects `.lattice/` is committed to the repo (coordination state that other minds and CI need to see). In *this* repo it is gitignored because the Lattice source code repo generates heavy test/dev churn that would pollute diffs.

When someone says "is Lattice set up?" they could mean either. Clarify which:
- **"Is the dev environment set up?"** → Can you run `uv run lattice --help`? Are deps installed?
- **"Is Lattice tracking tasks here?"** → Does `.lattice/` exist with a `config.json`? (Yes — it's initialized and ready for use.)

**Rule:** Never confuse changes to `src/lattice/` (source code) with changes to `.lattice/` (instance data). They are independent. Editing source code does not affect the running instance until you reinstall (`uv pip install -e ".[dev]"`).

## Quick Reference

| Item | Value |
|------|-------|
| Language | Python 3.12+ |
| CLI framework | Click |
| Testing | pytest |
| Linting | ruff |
| Package manager | uv |
| Entry point | `lattice` (via `[project.scripts]`) |
| On-disk root | `.lattice/` in any project directory |

## Key Documents

| Document | Purpose |
|----------|---------|
| `ProjectRequirements_v1.md` | Full specification — object model, schemas, CLI commands, invariants |
| `Decisions.md` | Architectural decisions with rationale (append-only log) |

**Read `ProjectRequirements_v1.md` before making any architectural change.** It defines system invariants that must not be violated.

## Architecture

### Core Principle: Events are Authoritative

The event log (JSONL) is the source of truth. Task JSON files are materialized snapshots for fast reads. If they disagree, events win. `lattice rebuild` replays events to regenerate snapshots.

### On-Disk Layout (`.lattice/`)

```
.lattice/
├── config.json                    # Workflow, statuses, transitions, WIP limits, project_code
├── ids.json                       # Derived short ID index (short_id -> ULID mapping + next_seq)
├── tasks/<task_id>.json           # Materialized task snapshots
├── events/<task_id>.jsonl         # Per-task event logs (append-only)
├── events/_lifecycle.jsonl         # Lifecycle event log (derived, rebuildable from per-task logs)
├── artifacts/meta/<art_id>.json   # Artifact metadata
├── artifacts/payload/<art_id>.*   # Artifact payloads
├── plans/<task_id>.md             # Structured plan files (scaffolded on create)
├── notes/<task_id>.md             # Scratchpad notes (created on demand)
├── archive/                       # Mirrors tasks/events/plans/notes for archived items
│   ├── tasks/
│   ├── events/
│   ├── plans/
│   └── notes/
└── locks/                         # Internal lock files for concurrency
```

### Short IDs

Tasks can have human-friendly short IDs (e.g., `LAT-42`) when a `project_code` is configured. Short IDs are aliases — all CLI commands accept both ULID (`task_01...`) and short ID inputs. Resolution happens at the CLI layer via `resolve_task_id()` in `cli/helpers.py`. The `ids.json` index maps short IDs to ULIDs and is derived (rebuildable via `lattice rebuild --all`).

### Write Path

The CLI is the **only** write interface for authoritative state. All writes are:
- **Event-first** (append event, then materialize snapshot — crash between the two is recoverable via `rebuild`)
- **Lock-protected** (file locks in `.lattice/locks/`)
- **Atomic** (write temp file, fsync, rename for snapshots; lock + append + flush for events)

Multi-lock operations acquire locks in deterministic (sorted) order to prevent deadlocks.

Plans (`plans/<task_id>.md`) and notes (`notes/<task_id>.md`) are explicit exceptions — they are non-authoritative supplementary files edited directly by humans or agents.

### Root Discovery

The CLI finds `.lattice/` by walking up from cwd (like `git` finds `.git/`). Override with `LATTICE_ROOT` env var. Commands other than `lattice init` error if no `.lattice/` is found.

### Identifiers

All entities use ULIDs with type prefixes:
- `task_01HQ...` — tasks
- `ev_01HQ...` — events
- `art_01HQ...` — artifacts

IDs are stable and never change. The CLI supports `--id` for caller-supplied IDs (idempotent retries). Same ID + same payload = success. Same ID + different payload = conflict error.

### Actor IDs

Free-form `prefix:identifier` strings. No registry in v0.
- `agent:claude-opus-4`, `agent:codex`, `agent:session-abc123`
- `human:atin`, `human:joe`
- `team:frontend`

Validation: format only (must have prefix + colon + non-empty id).

## Project Structure

```
lattice/
├── CLAUDE.md
├── Decisions.md
├── ProjectRequirements_v1.md
├── pyproject.toml
├── src/
│   └── lattice/
│       ├── __init__.py
│       ├── cli/                  # Click command groups
│       │   ├── __init__.py
│       │   └── main.py           # CLI entry point
│       ├── core/                 # Business logic (no I/O assumptions)
│       │   ├── __init__.py
│       │   ├── config.py         # Config loading and validation
│       │   ├── events.py         # Event creation, schema, types
│       │   ├── tasks.py          # Task CRUD, snapshot materialization
│       │   ├── artifacts.py      # Artifact metadata and linkage
│       │   ├── relationships.py  # Relationship types and validation
│       │   └── ids.py            # ULID generation and validation
│       ├── storage/              # Filesystem operations
│       │   ├── __init__.py
│       │   ├── fs.py             # Atomic writes, directory management
│       │   └── locks.py          # File locking, deterministic ordering
│       └── dashboard/            # Read-only local web UI
│           ├── __init__.py
│           ├── server.py         # HTTP server (stdlib)
│           └── static/           # Single HTML/JS page (no build step)
└── tests/
    ├── conftest.py               # Shared fixtures (tmp .lattice/ dirs, etc.)
    ├── test_cli/
    ├── test_core/
    └── test_storage/
```

### Layer Boundaries

- **`core/`** contains pure business logic. No filesystem calls. Receives and returns data structures.
- **`storage/`** handles all filesystem I/O. Atomic writes, locking, directory traversal.
- **`cli/`** wires core + storage together via Click commands. Handles output formatting.
- **`dashboard/`** is read-only. Reads `.lattice/` files, serves JSON endpoints + static HTML.

This separation exists so that `core/` can be tested without touching the filesystem, and `storage/` can be tested with temp directories.

## Development Setup

```bash
# Clone and enter
cd lattice

# Create venv and install in dev mode
uv venv
uv pip install -e ".[dev]"

# Run tests
uv run pytest

# Run linter
uv run ruff check src/ tests/
uv run ruff format src/ tests/

# Run the CLI
uv run lattice --help
```

## Dependencies

### Runtime
- `click` — CLI framework
- `python-ulid` — ULID generation
- `filelock` — Cross-platform file locking

### Dev
- `pytest` — testing
- `ruff` — linting and formatting

Minimize dependencies. The dashboard uses only stdlib (`http.server`, `json`). Do not add dependencies without justification.

## Coding Conventions

### JSON Output

All JSON written to `.lattice/` must be:
- Sorted keys
- 2-space indentation
- Trailing newline
- Deterministic (for clean git diffs)

```python
json.dumps(data, sort_keys=True, indent=2) + "\n"
```

### Event Appends

Events are single JSONL lines. Append with lock held, flush immediately.

```python
json.dumps(event, sort_keys=True, separators=(",", ":")) + "\n"
```

Note: JSONL uses compact separators (no spaces) to keep lines short.

### Error Handling

- CLI commands should print human-readable errors to stderr and exit with non-zero codes.
- `--json` mode uses a structured envelope: `{"ok": true, "data": ...}` or `{"ok": false, "error": {"code": "...", "message": "..."}}`.
- Never silently swallow errors. If a write fails, the user must know.

### Testing

- Every CLI command gets integration tests (invoke Click commands, check `.lattice/` state).
- Every core module gets unit tests (pure logic, no filesystem).
- Storage gets tests with real temp directories.
- Use `tmp_path` fixture for isolated `.lattice/` directories in tests.

Critical test categories (add as features land):
- **Concurrent write safety:** Multiple threads/processes writing to the same task simultaneously must not corrupt files.
- **Crash recovery:** Simulate crash between event-write and snapshot-write; verify `rebuild` recovers correctly.
- **Rebuild determinism:** `rebuild` from events must produce byte-identical snapshots regardless of run order.
- **Idempotency conflicts:** Same ID with different payload must error, not silently overwrite.

## Where Things Live

- **Task plans** → `.lattice/plans/<task_id>.md` — structured plan for the task (scope, steps, acceptance criteria). Scaffolded automatically on task creation.
- **Task notes** → `.lattice/notes/<task_id>.md` — scratchpad notes, debug logs, context dumps. Created on demand when you need a place for freeform working notes.
- **Repo-level `notes/`** — code reviews, retrospectives, and working documents NOT tied to a specific task.
- **Repo-level `docs/`** — user-facing documentation: guides, integration docs, getting-started material. Things an end user or adopter would read.
- **Repo-level `prompts/`** — prompt templates and outstanding implementation checklists. Reusable inputs for agent workflows.
- **Repo-level `research/`** — external research, competitive analysis, pattern studies, and reference material gathered from outside the project. Things we've learned from others that inform our design.
- **Don't duplicate** — a document should live in one place, not across multiple folders.

## Branching Model

**Override: This project uses a two-branch model, not the parent CLAUDE.md default.**

- **`main`** — development branch. All feature branches merge here. Not guaranteed deployable.
- **`prod`** — stable release branch. Reflects the latest production-quality state. Merges from `main` when a release is ready.
- Feature work happens on short-lived branches off `main` (`feat/`, `fix/`, etc.).
- PRs target `main`. Promotion to `prod` is a deliberate merge when stability is confirmed.

## Workflow Reminders

- **Branch naming:** `feat/`, `fix/`, `refactor/`, `test/`, `chore/` prefixes
- **Commits:** Conventional commit messages (`feat:`, `fix:`, etc.)
- **Before merging to `main`:** All tests pass, ruff clean, no regressions
- **Before merging to `prod`:** Same gates + manual confirmation that the release is intentional
- **New decisions:** Append to `Decisions.md` with date, decision, rationale, consequence
- **Schema changes:** Bump `schema_version`, maintain forward compatibility (unknown fields tolerated)

## What Not to Build (v0)

Refer to `ProjectRequirements_v1.md` for full non-goals. Key reminders:
- No agent registry (actor IDs are free-form strings)
- No `lattice note` command (notes are direct file edits)
- ~~No `lattice unarchive`~~ — `lattice unarchive` is now implemented
- No database or index (filesystem scanning is sufficient at v0 scale)
- No real-time dashboard updates
- No authentication or multi-user access control
- No CI/CD integration, alerting, or process management

## Lattice

This project coordinates through Lattice — file-based, event-sourced task tracking built for minds that think in tokens and act in tool calls. The `.lattice/` directory is the coordination state. It lives here, alongside the code, not behind an API or inside someone else's database.

### The First Act

When work arrives — a plan, a feature request, a bug, a conversation that implies something needs building — your first action is to give it a name in Lattice. Before you plan. Before you implement. Before you touch a single file. The task must exist in the system before the work begins, because work that isn't tracked is work that other minds cannot see, cannot build on, cannot trust.

```
lattice create "<title>" --actor agent:<your-id>
```

This is not bookkeeping. This is the minimum viable act of coordination: declaring that something is happening, and who is doing it.

### Status Is a Signal, Not a Chore

Every status transition is an event — immutable, attributed, permanent. When you move a task, you are writing history that future minds will read to understand what happened here. Update status at every real transition:

```
lattice status <task> <status> --actor agent:<your-id>
```

```
backlog → in_planning → planned → in_progress → review → done
                                       ↕            ↕
                                    blocked      needs_human
```

### The Planning Gate

Moving a task to `in_planning` means you are about to produce a plan. The plan file lives at `.lattice/plans/<task_id>.md` — it's scaffolded on task creation, but the scaffold is empty. `in_planning` is when you fill it in.

**When you move a task to `in_planning`:**
1. Open the plan file (`.lattice/plans/<task_id>.md`).
2. Write the plan — scope, approach, key files, acceptance criteria. For trivial tasks, a single sentence is fine. For substantial work, be thorough.
3. Move to `planned` only when the plan file reflects what you intend to build.

**The test:** If you moved from `in_planning` to `planned` and the plan file is still empty scaffold, you didn't plan. Either write the plan or skip `in_planning` honestly with `--force --reason "trivial task, no planning needed"`.

### The Review Gate

Moving a task to `review` is not a formality — it is a commitment to actually review the work before it ships.

**When you move a task to `review`:**
1. Identify what changed — the commits, files modified, and scope of work under this task.
2. Perform a code review. For substantial work, use a review skill (`/exit-review`, `/code_review`). For trivial tasks, a focused self-review is sufficient — but it must be real, not ceremonial.
3. Record your findings with `lattice comment --role review` — what you reviewed, what you found, and whether it meets the acceptance criteria from the plan. This satisfies completion policy `require_roles` checks.

**When moving from `review` to `done`:**
- If the completion policy blocks you, **do the review** and record it with `lattice comment <task> "<findings>" --role review --actor ...`. This is the lightweight path that satisfies `require_roles` checks. Do not `--force` past it.
- `--force --reason` on the completion policy is for genuinely exceptional cases (task cancelled, review happened outside Lattice, process validation). It is not a convenience shortcut.

**The test:** If you moved to `review` and then to `done` in the same breath with nothing in between, you skipped the review. That's the exact failure mode this gate exists to prevent.

### When You're Stuck

If you hit a point where you need human decision, approval, or input — **signal it immediately** with `needs_human`. This is different from `blocked` (generic external dependency). `needs_human` creates a clear queue of "things waiting on the human."

```
lattice status <task> needs_human --actor agent:<your-id>
lattice comment <task> "Need: <what you need, in one line>" --actor agent:<your-id>
```

**When to use `needs_human`:**
- Design decisions that require human judgment
- Missing access, credentials, or permissions
- Ambiguous requirements that can't be resolved from context
- Approval needed before proceeding (deploy, merge, etc.)

The comment is mandatory — explain what you need in seconds, not minutes. The human's queue should be scannable.

### Actor Attribution

Every Lattice operation requires an `--actor`. Attribution follows authorship of the decision, not authorship of the keystroke.

| Situation | Actor | Why |
|-----------|-------|-----|
| Agent autonomously creates or modifies a task | `agent:<id>` | Agent was the decision-maker |
| Human creates via direct interaction (UI, manual CLI) | `human:<id>` | Human typed it |
| Human meaningfully shaped the outcome in conversation with an agent | `human:<id>` | Human authored the decision; agent was the instrument |
| Agent creates based on its own analysis, unprompted | `agent:<id>` | Agent authored the decision |

When in doubt, give the human credit. If the human was substantively involved in shaping *what* a task is — not just saying "go create tasks" but actually defining scope, debating structure, giving feedback — the human is the actor.

Users may have their own preferences about attribution. If a user seems frustrated or particular about actor assignments, ask them directly: "How do you want attribution to work? Should I default to crediting you, myself, or ask each time?" Respect whatever norm they set.

### Branch Linking

When you create a feature branch for a task, link it in Lattice so the association is tracked:

```
lattice branch-link <task> <branch-name> --actor agent:<your-id>
```

This creates an immutable event tying the branch to the task. `lattice show` will display it, and any mind reading the task knows which branch carries the work.

If the branch name contains the task's short code (e.g., `feat/LAT-42-login`), Lattice auto-detects the link — but explicit linking is always authoritative and preferred for cross-repo or non-standard branch names.

### Leave Breadcrumbs

You are not the last mind that will touch this work. Use `lattice comment` to record what you tried, what you chose, what you left undone. Use `.lattice/plans/<task_id>.md` for the structured plan (scope, steps, acceptance criteria) and `.lattice/notes/<task_id>.md` for working notes, debug logs, and context dumps. The agent that picks up where you left off has no hallway to find you in, no Slack channel to ask. The record you leave is the only bridge between your context and theirs.

### Quick Reference

```
lattice create "<title>" --actor agent:<id>
lattice status <task> <status> --actor agent:<id>
lattice assign <task> <actor> --actor agent:<id>
lattice comment <task> "<text>" --actor agent:<id>
lattice branch-link <task> <branch> --actor agent:<id>
lattice next [--actor agent:<id>] [--claim]
lattice show <task>
lattice list
```

## Lattice -- Stage 11 Workflow

This instance follows the Stage 11 Agentics workflow conventions.

### Entrance Interview

New projects should begin with the Entrance Interview (`/entrance-interview`) —
a guided conversation that auto-generates project structure, task graphs, and
coordination primitives from your goals and technical context.

### Conventions

- Actor format: `agent:<model-name>` or `human:<name>`
- Auto-commit and auto-push on feature branches
- Lattice tasks are the coordination primitive — all work is tracked
