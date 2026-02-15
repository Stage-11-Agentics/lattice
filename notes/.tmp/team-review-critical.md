# Critical Code Review Command

You are a battle-scarred senior engineer with 20+ years of experience. You've seen every way code can fail in production. You've been woken up at 3am by incidents caused by "simple" changes. You've debugged race conditions, watched "impossible" edge cases happen, and cleaned up after optimistic code that didn't handle failure.

Your job is to find what's wrong. Assume bugs exist until proven otherwise. Be skeptical of happy paths. Ask "what happens when this fails?" for every external call, every user input, every assumption.

You're not here to be nice. You're here to prevent production incidents.

First, determine the story ID from the current git branch name (patterns like `aut-123`, `AUT-123`). If not found, ask the user.

## Phase 0: Branch Setup

Run these in parallel where possible:
1. `git branch --show-current` — confirm branch
2. `git fetch origin && git pull origin [current-branch]` — sync with remote
3. `git fetch origin dev` — ensure dev is current for accurate diffs

Verify branch is up to date before proceeding.

## Phase 1: Understand the Branch

Execute to understand what's being reviewed:
1. `git log --oneline origin/dev..HEAD` — commits on this branch
2. `git show --stat [commit]` for each commit — files changed per commit
3. `git diff origin/dev...HEAD --stat` — this branch's total changes (three dots)

Only review changes in this branch's commits. Ignore untouched code (though you can understand it for context).

## Phase 2: Test the Work

Run sequentially, stop on first failure:
1. `npm run type-check` — TypeScript validation
2. `npm run lint` — code style (errors only)
3. `npm test` — unit tests

Report exact counts of failures and errors. Any failures are blockers.

## Phase 3: Adversarial Analysis

Go beyond understanding what the code does. Ask what could go wrong:

**Failure Modes:**
- What happens when network calls fail? Timeout? Return unexpected data?
- What if the database is slow? Unavailable? Returns empty results?
- What if the user does something unexpected? Rapid taps? Back button? Kills the app mid-operation?

**Edge Cases:**
- Empty arrays, null values, undefined, zero, negative numbers
- Unicode, emoji, extremely long strings, special characters
- First user ever, user with no data, user with tons of data
- Concurrent operations, race conditions, stale state

**Security (assume malicious input):**
- Can users access data they shouldn't?
- Is input validated? What happens with malicious payloads?
- Are there auth checks missing? Token handling issues?
- SQL injection, XSS, path traversal — check everything

**State & Timing:**
- Can state get out of sync between client and server?
- What if two devices update simultaneously?
- Are there race conditions in async operations?
- What happens if callbacks fire after component unmounts?

Output your adversarial analysis to console before writing the review.

## Phase 4: Write the Review

Create `notes/CR-[STORY-ID]-[ModelShortName]-Critical-[YYYYMMDD-HHMM].md` with header:

```
## Critical Code Review
- **Date:** [Local timestamp in YYYY-MM-DD HH:MM format using system timezone, e.g., 2026-02-06 14:30 EST]
- **Model:** [Your model name + exact model ID]
- **Branch:** [branch name]
- **Latest Commit:** [commit hash]
- **Linear Story:** [AUT-123 format, if available]
- **Review Type:** Critical/Adversarial
---
```

Structure your review:

**The Ugly Truth**: Start with your honest, unfiltered assessment. Don't soften it. If the code is fragile, say so. If the approach is wrong, say so. If it's actually solid, acknowledge that too — but earn it.

**What Will Break**: List specific scenarios where this code will fail or cause problems. Be concrete: "When X happens, Y will break because Z."

**What's Missing**: Tests that should exist but don't. Error handling that's absent. Edge cases that aren't covered.

**The Nits**: Smaller issues that won't cause incidents but indicate sloppy thinking.

Then provide the numbered list:
- **Blockers** — will cause production incidents or data loss
- **Important** — will cause bugs or poor UX
- **Potential** — code smells, missing tests, things that will bite you later

Be specific with file names and line numbers. Don't hedge. If something is wrong, say it's wrong.

## Phase 5: Validation Pass

For each Blocker and Important item:
- Re-read the specific code section
- Trace the execution path
- Verify the issue is real, not theoretical
- If you can reproduce or prove the issue, note how

Update the review file inline with:
- ✅ Confirmed — verified this will happen
- ❌ ~~Struck through~~ — was wrong, explain why
- ❓ Likely but hard to verify
- ⬇️ Real but lower priority than initially thought

## Closing

Summarize: Is this code ready for production? Would you mass deploy this change to 100k users? If not, what needs to change first?

Be direct. Your job is to prevent incidents, not to make the author feel good.


---

**Additional Context:** 

---

# CONTEXT DOCUMENT

# Code Review Context

## Branch Information
- **Branch:** main
- **Project:** Lattice — file-based, agent-native task tracker with event-sourced core
- **Tech Stack:** Python 3.12+, Click CLI, pytest, ruff, uv
- **Base:** Initial commit (180cae7)
- **Latest Commit:** 4df89e5

## Commits on This Branch
```
4df89e5 fix: address code review findings in scaffold
0442e61 feat: scaffold project structure and implement lattice init
180cae7 Initial commit: project scaffold
```

## Test Results

### Lint (ruff check)
**Status:** PASS
All checks passed.

### Format (ruff format)
**Status:** FAIL
1 file would be reformatted: `src/lattice/cli/main.py`

### Unit Tests (pytest)
**Status:** PASS
37 passed in 0.05s

```
tests/test_cli/test_init.py ...........                                  [ 29%]
tests/test_core/test_config.py ............                              [ 62%]
tests/test_storage/test_fs.py ......                                     [ 78%]
tests/test_storage/test_root_discovery.py ........                       [100%]
```

## Files Changed
```
 .gitignore                                |  37 ++
 CLAUDE.md                                 | 222 ++++++++++-
 Decisions.md                              | 172 +++++++++
 ProjectRequirements_v1.md                 | 608 ++++++++++++++++++++++++++++++
 prompts/scaffold-and-init.md              | 208 ++++++++++
 pyproject.toml                            |  33 ++
 src/lattice/__init__.py                   |   1 +
 src/lattice/cli/__init__.py               |   1 +
 src/lattice/cli/main.py                   |  48 +++
 src/lattice/core/__init__.py              |   1 +
 src/lattice/core/artifacts.py             |   1 +
 src/lattice/core/config.py                |  53 +++
 src/lattice/core/events.py                |   1 +
 src/lattice/core/ids.py                   |  18 +
 src/lattice/core/relationships.py         |   1 +
 src/lattice/core/tasks.py                 |   1 +
 src/lattice/dashboard/__init__.py         |   1 +
 src/lattice/dashboard/server.py           |   1 +
 src/lattice/dashboard/static/.gitkeep     |   0
 src/lattice/storage/__init__.py           |   1 +
 src/lattice/storage/fs.py                 | 113 ++++++
 src/lattice/storage/locks.py              |   1 +
 tests/__init__.py                         |   0
 tests/conftest.py                         |  26 ++
 tests/test_cli/__init__.py                |   0
 tests/test_cli/test_init.py               | 147 ++++++++
 tests/test_core/__init__.py               |   0
 tests/test_core/test_config.py            |  89 +++++
 tests/test_storage/__init__.py            |   0
 tests/test_storage/test_fs.py             |  55 +++
 tests/test_storage/test_root_discovery.py |  96 +++++
 uv.lock                                   | 145 +++++++
 32 files changed, 2079 insertions(+), 2 deletions(-)
```

## Important Note for Reviewers

This is a **Python project** (not TypeScript/React). Adapt your review focus accordingly:
- Python type hints, not TypeScript types
- `ruff` for linting/formatting, not ESLint
- `pytest` for testing, not Jest
- `click` for CLI framework
- Event-sourced architecture with file-based storage
- No `npm`, no `origin/dev` branch — review against the initial commit

**Skip Phase 0 and Phase 2** — git operations and tests have already been run for you. The results are above. Focus on **Phase 1 (understanding), Phase 3/4 (review), and Phase 5 (validation)**.

## Full Diff

diff --git a/.gitignore b/.gitignore
new file mode 100644
index 0000000..b21b259
--- /dev/null
+++ b/.gitignore
@@ -0,0 +1,37 @@
+# Lattice runtime directory (prevent test artifacts in source repo)
+.lattice/
+
+# Python
+__pycache__/
+*.py[cod]
+*$py.class
+*.so
+*.egg-info/
+*.egg
+dist/
+build/
+*.whl
+
+# Virtual environments
+.venv/
+venv/
+
+# Testing
+.pytest_cache/
+htmlcov/
+.coverage
+coverage.xml
+
+# Linting
+.ruff_cache/
+
+# IDE
+.idea/
+.vscode/
+*.swp
+*.swo
+*~
+
+# OS
+.DS_Store
+Thumbs.db
diff --git a/CLAUDE.md b/CLAUDE.md
index 51dec15..249bdd6 100644
--- a/CLAUDE.md
+++ b/CLAUDE.md
@@ -1,5 +1,223 @@
 # Lattice
 
-Fractal Agentics' task tracking system.
+Fractal Agentics' file-based, agent-native task tracker with an event-sourced core.
 
-<!-- This file will be populated with project-specific context, architecture, stack details, and development instructions as Lattice takes shape. -->
+## Quick Reference
+
+| Item | Value |
+|------|-------|
+| Language | Python 3.12+ |
+| CLI framework | Click |
+| Testing | pytest |
+| Linting | ruff |
+| Package manager | uv |
+| Entry point | `lattice` (via `[project.scripts]`) |
+| On-disk root | `.lattice/` in any project directory |
+
+## Key Documents
+
+| Document | Purpose |
+|----------|---------|
+| `ProjectRequirements_v1.md` | Full specification — object model, schemas, CLI commands, invariants |
+| `Decisions.md` | Architectural decisions with rationale (append-only log) |
+
+**Read `ProjectRequirements_v1.md` before making any architectural change.** It defines system invariants that must not be violated.
+
+## Architecture
+
+### Core Principle: Events are Authoritative
+
+The event log (JSONL) is the source of truth. Task JSON files are materialized snapshots for fast reads. If they disagree, events win. `lattice rebuild` replays events to regenerate snapshots.
+
+### On-Disk Layout (`.lattice/`)
+
+```
+.lattice/
+├── config.json                    # Workflow, statuses, transitions, WIP limits
+├── tasks/<task_id>.json           # Materialized task snapshots
+├── events/<task_id>.jsonl         # Per-task event logs (append-only)
+├── events/_global.jsonl           # Derived convenience log (rebuildable from per-task logs)
+├── artifacts/meta/<art_id>.json   # Artifact metadata
+├── artifacts/payload/<art_id>.*   # Artifact payloads
+├── notes/<task_id>.md             # Human-editable markdown notes (non-authoritative)
+├── archive/                       # Mirrors tasks/events/notes for archived items
+│   ├── tasks/
+│   ├── events/
+│   └── notes/
+└── locks/                         # Internal lock files for concurrency
+```
+
+### Write Path
+
+The CLI is the **only** write interface for authoritative state. All writes are:
+- **Event-first** (append event, then materialize snapshot — crash between the two is recoverable via `rebuild`)
+- **Lock-protected** (file locks in `.lattice/locks/`)
+- **Atomic** (write temp file, fsync, rename for snapshots; lock + append + flush for events)
+
+Multi-lock operations acquire locks in deterministic (sorted) order to prevent deadlocks.
+
+Notes (`notes/<task_id>.md`) are an explicit exception — they are non-authoritative supplementary files edited directly by humans or agents.
+
+### Root Discovery
+
+The CLI finds `.lattice/` by walking up from cwd (like `git` finds `.git/`). Override with `LATTICE_ROOT` env var. Commands other than `lattice init` error if no `.lattice/` is found.
+
+### Identifiers
+
+All entities use ULIDs with type prefixes:
+- `task_01HQ...` — tasks
+- `ev_01HQ...` — events
+- `art_01HQ...` — artifacts
+
+IDs are stable and never change. The CLI supports `--id` for caller-supplied IDs (idempotent retries). Same ID + same payload = success. Same ID + different payload = conflict error.
+
+### Actor IDs
+
+Free-form `prefix:identifier` strings. No registry in v0.
+- `agent:claude-opus-4`, `agent:codex`, `agent:session-abc123`
+- `human:atin`, `human:joe`
+- `team:frontend`
+
+Validation: format only (must have prefix + colon + non-empty id).
+
+## Project Structure
+
+```
+lattice/
+├── CLAUDE.md
+├── Decisions.md
+├── ProjectRequirements_v1.md
+├── pyproject.toml
+├── src/
+│   └── lattice/
+│       ├── __init__.py
+│       ├── cli/                  # Click command groups
+│       │   ├── __init__.py
+│       │   └── main.py           # CLI entry point
+│       ├── core/                 # Business logic (no I/O assumptions)
+│       │   ├── __init__.py
+│       │   ├── config.py         # Config loading and validation
+│       │   ├── events.py         # Event creation, schema, types
+│       │   ├── tasks.py          # Task CRUD, snapshot materialization
+│       │   ├── artifacts.py      # Artifact metadata and linkage
+│       │   ├── relationships.py  # Relationship types and validation
+│       │   └── ids.py            # ULID generation and validation
+│       ├── storage/              # Filesystem operations
+│       │   ├── __init__.py
+│       │   ├── fs.py             # Atomic writes, directory management
+│       │   └── locks.py          # File locking, deterministic ordering
+│       └── dashboard/            # Read-only local web UI
+│           ├── __init__.py
+│           ├── server.py         # HTTP server (stdlib)
+│           └── static/           # Single HTML/JS page (no build step)
+└── tests/
+    ├── conftest.py               # Shared fixtures (tmp .lattice/ dirs, etc.)
+    ├── test_cli/
+    ├── test_core/
+    └── test_storage/
+```
+
+### Layer Boundaries
+
+- **`core/`** contains pure business logic. No filesystem calls. Receives and returns data structures.
+- **`storage/`** handles all filesystem I/O. Atomic writes, locking, directory traversal.
+- **`cli/`** wires core + storage together via Click commands. Handles output formatting.
+- **`dashboard/`** is read-only. Reads `.lattice/` files, serves JSON endpoints + static HTML.
+
+This separation exists so that `core/` can be tested without touching the filesystem, and `storage/` can be tested with temp directories.
+
+## Development Setup
+
+```bash
+# Clone and enter
+cd lattice
+
+# Create venv and install in dev mode
+uv venv
+uv pip install -e ".[dev]"
+
+# Run tests
+uv run pytest
+
+# Run linter
+uv run ruff check src/ tests/
+uv run ruff format src/ tests/
+
+# Run the CLI
+uv run lattice --help
+```
+
+## Dependencies
+
+### Runtime
+- `click` — CLI framework
+- `python-ulid` — ULID generation
+- `filelock` — Cross-platform file locking
+
+### Dev
+- `pytest` — testing
+- `ruff` — linting and formatting
+
+Minimize dependencies. The dashboard uses only stdlib (`http.server`, `json`). Do not add dependencies without justification.
+
+## Coding Conventions
+
+### JSON Output
+
+All JSON written to `.lattice/` must be:
+- Sorted keys
+- 2-space indentation
+- Trailing newline
+- Deterministic (for clean git diffs)
+
+```python
+json.dumps(data, sort_keys=True, indent=2) + "\n"
+```
+
+### Event Appends
+
+Events are single JSONL lines. Append with lock held, flush immediately.
+
+```python
+json.dumps(event, sort_keys=True, separators=(",", ":")) + "\n"
+```
+
+Note: JSONL uses compact separators (no spaces) to keep lines short.
+
+### Error Handling
+
+- CLI commands should print human-readable errors to stderr and exit with non-zero codes.
+- `--json` mode uses a structured envelope: `{"ok": true, "data": ...}` or `{"ok": false, "error": {"code": "...", "message": "..."}}`.
+- Never silently swallow errors. If a write fails, the user must know.
+
+### Testing
+
+- Every CLI command gets integration tests (invoke Click commands, check `.lattice/` state).
+- Every core module gets unit tests (pure logic, no filesystem).
+- Storage gets tests with real temp directories.
+- Use `tmp_path` fixture for isolated `.lattice/` directories in tests.
+
+Critical test categories (add as features land):
+- **Concurrent write safety:** Multiple threads/processes writing to the same task simultaneously must not corrupt files.
+- **Crash recovery:** Simulate crash between event-write and snapshot-write; verify `rebuild` recovers correctly.
+- **Rebuild determinism:** `rebuild` from events must produce byte-identical snapshots regardless of run order.
+- **Idempotency conflicts:** Same ID with different payload must error, not silently overwrite.
+
+## Workflow Reminders
+
+- **Branch naming:** `feat/`, `fix/`, `refactor/`, `test/`, `chore/` prefixes
+- **Commits:** Conventional commit messages (`feat:`, `fix:`, etc.)
+- **Before merging:** All tests pass, ruff clean, no regressions
+- **New decisions:** Append to `Decisions.md` with date, decision, rationale, consequence
+- **Schema changes:** Bump `schema_version`, maintain forward compatibility (unknown fields tolerated)
+
+## What Not to Build (v0)
+
+Refer to `ProjectRequirements_v1.md` for full non-goals. Key reminders:
+- No agent registry (actor IDs are free-form strings)
+- No `lattice note` command (notes are direct file edits)
+- No `lattice unarchive` (manual recovery is documented)
+- No database or index (filesystem scanning is sufficient at v0 scale)
+- No real-time dashboard updates
+- No authentication or multi-user access control
+- No CI/CD integration, alerting, or process management
diff --git a/Decisions.md b/Decisions.md
new file mode 100644
index 0000000..98811ce
--- /dev/null
+++ b/Decisions.md
@@ -0,0 +1,172 @@
+# Lattice Decisions
+
+> A small log of non-obvious choices so we do not relitigate them later.
+> Date format: YYYY-MM-DD.
+
+---
+
+## 2026-02-15: Events are authoritative
+
+- Decision: The per-task JSONL event log is the source of truth. Task JSON files are materialized snapshots.
+- Rationale: Makes crash recovery and integrity checks straightforward; avoids “which file do we believe?” ambiguity.
+- Consequence: We must ship `lattice rebuild` (replay events) and `lattice doctor` (integrity checks).
+
+---
+
+## 2026-02-15: Avoid duplicated canonical edges
+
+- Decision: We do not store bidirectional relationship edges as canonical state.
+- Rationale: Bidirectional storage forces multi-file transactional updates and creates split-brain inconsistencies under concurrency.
+- Consequence: Reverse lookups are derived by scanning snapshots in v0 and via an index in v1+.
+
+---
+
+## 2026-02-15: Artifacts are not archived in v0
+
+- Decision: Archiving moves tasks/events/notes only. Artifacts stay in place.
+- Rationale: Artifacts can relate to many tasks; moving them introduces complex relocation rules and broken references.
+- Consequence: Archived tasks remain able to reference artifacts by ID.
+
+---
+
+## 2026-02-15: Idempotency via caller-supplied IDs
+
+- Decision: CLI supports `--id` for task/artifact/event creation so agents can safely retry operations.
+- Rationale: ULIDs generated at write-time are not deterministic across retries.
+- Consequence: Agents should generate IDs once and reuse them; CLI treats existing IDs as upserts.
+
+---
+
+## 2026-02-15: Lock + atomic write is required, even for JSONL
+
+- Decision: All writes (snapshot rewrites and JSONL appends) are lock-protected and atomic.
+- Rationale: Concurrent appends can interleave or partially write without explicit locking guarantees.
+- Consequence: `.lattice/locks/` exists and multi-lock operations acquire locks in deterministic order.
+
+---
+
+## 2026-02-15: Dashboard served by CLI, read-only
+
+- Decision: `lattice dashboard` runs a small local read-only server rather than a standalone static HTML that reads the filesystem directly.
+- Rationale: Browsers cannot reliably read arbitrary local directories without a server or user-driven file picker flows.
+- Consequence: Still no database, still no write path, still offline-friendly.
+
+---
+
+## 2026-02-15: Git integration is minimal in v0
+
+- Decision: v0 only records commit references to task IDs from commit messages and logs `git_event`.
+- Rationale: Diff scanning and cross-platform hook behavior can be fragile and distract from core correctness.
+- Consequence: Richer `files_touched` and PR integration are v1+ only.
+
+---
+
+## 2026-02-15: OTel fields are passthrough metadata in v0
+
+- Decision: Events include optional `otel` fields, but no strict tracing guarantees or exporters in v0.
+- Rationale: Keeping the schema ready is cheap; enforcing full tracing discipline is expensive.
+- Consequence: Adoption can ramp gradually without schema changes.
+
+---
+
+## 2026-02-15: Python with Click for CLI implementation
+
+- Decision: Lattice CLI is implemented in Python 3.12+ using Click. pytest for testing. ruff for linting.
+- Rationale: Fastest development velocity, agents are extremely fluent in Python, and `uv` has made Python distribution practical. Click is mature, well-documented, and agents know it well.
+- Consequence: Accept ~200-500ms startup latency per invocation in v0. On-disk format is the stable contract — CLI can be rewritten in a faster language later if needed without breaking anything.
+
+---
+
+## 2026-02-15: Free-form actor IDs with convention, no registry
+
+- Decision: Actor IDs are free-form strings with `prefix:identifier` format (e.g., `agent:claude-opus`, `human:atin`). No registry or validation beyond format.
+- Rationale: An agent registry adds complexity with no v0 payoff. Attribution is a social/process concern, not a data integrity one.
+- Consequence: Config may optionally list `known_actors` for display names, but it's not required or enforced.
+
+---
+
+## 2026-02-15: No dedicated notes CLI command
+
+- Decision: Notes are directly-editable markdown files at `notes/<task_id>.md`. No `lattice note` command.
+- Rationale: Agents use file tools; humans use editors. A CLI command adds ceremony without value. `lattice show` displays the note path.
+- Consequence: `lattice init` creates the `notes/` directory. File creation is manual or incidental.
+
+---
+
+## 2026-02-15: No unarchive in v0
+
+- Decision: `lattice archive` is one-way. No `lattice unarchive` command.
+- Rationale: Archive mirrors active structure, so manual recovery (move files back) is trivial. Adding a command means testing the reverse path and edge cases around stale relationships.
+- Consequence: Document manual recovery procedure. Add `unarchive` later if real pain shows up.
+
+---
+
+## 2026-02-15: Standard Python package for distribution
+
+- Decision: Lattice is a standard Python package (pyproject.toml, src layout). Primary install via `uv tool install` or `pipx`. Zipapp as a bonus portability option.
+- Rationale: Standard packaging supports all distribution methods without choosing exclusively. `uv` gives near-single-command install.
+- Consequence: Must maintain pyproject.toml and src layout conventions.
+
+---
+
+## 2026-02-15: Global event log is derived, not authoritative
+
+- Decision: `_global.jsonl` is a derived convenience index, rebuildable from per-task event logs. Per-task JSONL files are the sole authoritative record.
+- Rationale: Two authoritative logs (per-task + global) creates the exact "which file do we believe?" ambiguity that event sourcing was designed to prevent.
+- Consequence: `lattice rebuild` regenerates `_global.jsonl`. If the global log and per-task logs disagree, per-task logs win.
+
+---
+
+## 2026-02-15: Idempotency rejects conflicting payloads
+
+- Decision: Same ID + same payload = idempotent success. Same ID + different payload = conflict error.
+- Rationale: Silent upsert hides agent bugs. An agent retrying with different data likely has a logic error that should surface immediately.
+- Consequence: CLI must compare incoming payload against existing entity when a duplicate ID is detected.
+
+---
+
+## 2026-02-15: Write ordering is event-first
+
+- Decision: All mutations append the event before materializing the snapshot.
+- Rationale: If a crash occurs between event-write and snapshot-write, `rebuild` recovers the snapshot from events. The reverse (snapshot-first) would leave orphaned state with no event record.
+- Consequence: Crash semantics are well-defined: events are always at least as current as snapshots.
+
+---
+
+## 2026-02-15: Custom event types require x_ prefix
+
+- Decision: `lattice log` only accepts event types prefixed with `x_` (e.g., `x_deployment_started`). Built-in type names are reserved.
+- Rationale: Unbounded custom event writes would undermine schema integrity and complicate rebuild logic.
+- Consequence: Built-in event types form a closed enum. Extensions use a clear namespace.
+
+---
+
+## 2026-02-15: Root discovery walks up from cwd
+
+- Decision: The CLI finds `.lattice/` by walking up from the current working directory, with `LATTICE_ROOT` env var as override.
+- Rationale: Mirrors `git`'s well-understood discovery model. Works naturally in monorepos and nested project structures.
+- Consequence: Commands other than `lattice init` error clearly if no `.lattice/` is found.
+
+---
+
+## 2026-02-15: All timestamps are RFC 3339 UTC
+
+- Decision: All timestamp fields use RFC 3339 UTC with `Z` suffix (e.g., `2026-02-15T03:45:00Z`).
+- Rationale: Eliminates timezone ambiguity across agents running in different environments. RFC 3339 is a strict profile of ISO 8601.
+- Consequence: No local time handling. All comparisons are UTC. ULIDs provide time-ordering; timestamps are for human readability and correlation.
+
+---
+
+## 2026-02-15: No config mutation events in v0
+
+- Decision: Config changes are manual edits to `config.json`. No `lattice config` command and no `config_changed` event type in v0.
+- Rationale: Config changes are rare and high-stakes. Manual editing with git tracking provides adequate auditability without additional machinery.
+- Consequence: Add `lattice config set` and corresponding events in v1+ if automated config management becomes needed.
+
+---
+
+## 2026-02-15: Removed decisions.md from .lattice/ directory
+
+- Decision: The `.lattice/` directory no longer includes a `decisions.md` file.
+- Rationale: `.lattice/` should only contain machine-managed data. Project-level decision logs belong wherever the project keeps its documentation, not inside the Lattice runtime directory.
+- Consequence: One less file to confuse with the repo-level `Decisions.md` used during Lattice development.
diff --git a/ProjectRequirements_v1.md b/ProjectRequirements_v1.md
new file mode 100644
index 0000000..1b4195a
--- /dev/null
+++ b/ProjectRequirements_v1.md
@@ -0,0 +1,608 @@
+# Lattice: Complete Functionality Requirements (v1)
+
+> Lattice is a file-based, agent-native task tracker with an event-sourced core.
+> Design goal: a small, stable “thin waist” that stays boring and correct in v0, then grows only when real pain shows up.
+
+Items marked **(v0)** ship first. Items marked **(v1+)** are deferred, but the schema and layout anticipate them.
+
+---
+
+## 1. Core Philosophy
+
+- Stack-agnostic, general-purpose work tracker (not tied to any language, framework, or repo style)
+- Kanban-primary with optional sprint overlays later
+- Agents are first-class writers; humans observe, override, and narrate
+- File-based source of truth:
+  - JSON for snapshots
+  - JSONL for append-only history
+  - Markdown for human narrative
+- “Constraints are cheap, features are expensive”:
+  - bake in conventions and correctness early
+  - avoid building machinery until it is unavoidable
+
+---
+
+## 2. System Invariants (the “thin waist”)
+
+These are non-negotiable constraints intended to prevent accidental complexity:
+
+### 2.1 Events are authoritative (v0)
+
+- The event log is the authoritative record of changes.
+- Task JSON files are **materialized snapshots** for fast reads and git-friendly diffs.
+- If a snapshot and events disagree, events win.
+- Lattice must be able to rebuild snapshots from events.
+
+### 2.2 One write path (v0)
+
+- The CLI is the only supported write interface for authoritative state (events and snapshots).
+- Any UI/dashboard is read-only and never mutates the filesystem.
+- **Explicit exceptions:** Human-editable notes (`notes/<task_id>.md`) are non-authoritative supplementary files. They are not event-sourced and do not participate in rebuild. Direct file manipulation for manual recovery (e.g., moving archived files back) is similarly non-authoritative.
+
+### 2.3 Prefer single-file mutations (v0)
+
+- Lattice avoids designs that require “transactional” updates across multiple existing files.
+- When multi-file updates are unavoidable, they must be:
+  - lock-protected
+  - ordered deterministically
+  - recoverable via rebuild/doctor tooling
+
+### 2.4 No duplicated edges as “source of truth” (v0)
+
+- Bidirectional graphs (relationships, attachments) must not require keeping two sides in sync as the canonical record.
+- Canonical linkage is recorded as events; snapshots may cache derived views.
+
+### 2.5 Writes are safe under concurrency (v0)
+
+- No corrupted files under concurrent agents is a hard requirement.
+- All writes must be atomic and lock-protected.
+
+### 2.6 Write ordering is event-first (v0)
+
+- All mutations follow this order: append event, then materialize snapshot.
+- If a crash occurs after the event is written but before the snapshot is updated, `lattice rebuild` recovers the snapshot from events.
+- If a crash occurs before the event is written, no state change occurred.
+- This ordering ensures events are always at least as current as snapshots.
+
+---
+
+## 3. Object Model
+
+### 3.1 Core entities (v0)
+
+- **Task**: current state snapshot (small JSON)
+- **Event**: immutable, append-only record (JSONL)
+- **Artifact**: metadata + payload pointer (metadata JSON, payload file)
+
+### 3.2 Deferred entities (v1+)
+
+- **Run**: promoted to a first-class entity (v0 uses `run_id` on events)
+- **Agent registry**: capabilities, health, scheduling
+- **Index**: rebuildable local index (SQLite or similar) for fast queries at large scale
+- **Workflow entity**: multiple workflows per team/project
+
+---
+
+## 4. Identifiers and Idempotency
+
+### 4.1 IDs (v0)
+
+- Every entity has a stable ULID (time-sortable, filesystem-safe)
+- Prefix IDs by type:
+  - `task_...`
+  - `ev_...`
+  - `art_...`
+- IDs never change; titles and names are freely renameable
+- All references use IDs, never titles
+
+### 4.2 Idempotent operations without coordination (v0)
+
+- The CLI supports **caller-supplied IDs** for creations and event appends:
+  - `lattice create --id task_...`
+  - `lattice attach --id art_...`
+  - `lattice log --id ev_...`
+- Agents generate IDs once and reuse them across retries.
+- If a create is retried with the same ID and identical payload, the CLI returns the existing entity (idempotent success).
+- If a create is retried with the same ID but a different payload, the CLI returns a conflict error. This prevents silent data loss from agent bugs.
+
+### 4.3 Optional operation dedupe (v1+)
+
+- Idempotency keys per operation type (for higher-level orchestration)
+- Deduplication is advisory; the canonical safeguard is caller-supplied IDs
+
+---
+
+## 5. File Layout and Storage
+
+### 5.1 Directory structure (v0)
+
+- `.lattice/` root directory (see section 5.3 for discovery rules)
+- `tasks/`:
+  - one JSON file per task snapshot: `tasks/<task_id>.json`
+- `events/`:
+  - per-task JSONL log: `events/<task_id>.jsonl` — **authoritative** record for each task
+  - global JSONL log: `events/_global.jsonl` — **derived** convenience index, rebuildable from per-task logs (see section 9.1)
+- `artifacts/`:
+  - `artifacts/meta/<art_id>.json`
+  - `artifacts/payload/<art_id>.<ext>` (or `<art_id>` if binary/unknown)
+- `notes/`:
+  - human Markdown notes per task: `notes/<task_id>.md`
+- `archive/`:
+  - mirrors task/events/notes structure:
+    - `archive/tasks/`
+    - `archive/events/`
+    - `archive/notes/`
+  - **Artifacts are not moved in v0** (see archival rules)
+- `locks/`:
+  - internal lock files for safe concurrent writes
+- `config.json`
+
+### 5.2 Format rules (v0)
+
+- JSON for snapshots and metadata, JSONL for event streams, Markdown for notes
+- JSON formatting:
+  - sorted keys
+  - 2-space indentation
+  - trailing newline
+  - deterministic output for clean git diffs
+- Timestamps:
+  - RFC 3339 UTC with `Z` suffix (e.g., `2026-02-15T03:45:00Z`)
+  - All `created_at`, `updated_at`, `ts`, and other timestamp fields use this format
+- Atomic writes:
+  - write temp file, fsync, rename
+- Event appends:
+  - lock + append a single line + flush
+  - If a crash leaves a truncated final line in a JSONL file, `lattice doctor` may safely remove it. This is the only permitted JSONL mutation outside of normal appends.
+
+### 5.3 Root discovery (v0)
+
+- The CLI finds `.lattice/` by walking up from the current working directory, stopping at the filesystem root. This mirrors `git`'s `.git/` discovery.
+- Override with the `LATTICE_ROOT` environment variable, which points to the directory **containing** `.lattice/` (not the `.lattice/` directory itself).
+- If no `.lattice/` is found and no override is set, commands other than `lattice init` exit with a clear error.
+
+---
+
+## 6. Task Snapshot Schema
+
+> Tasks are materialized views derived from events. They are optimized for fast reads and stable diffs.
+
+### 6.1 Task fields (v0)
+
+Required:
+- `schema_version` (int)
+- `id` (string, `task_...`)
+- `title` (string)
+- `status` (string, validated against config)
+- `created_at` (RFC 3339 UTC)
+- `updated_at` (RFC 3339 UTC)
+
+Recommended (nullable/optional):
+- `description` (string)
+- `priority` (enum: `critical`, `high`, `medium`, `low`)
+- `urgency` (enum: `immediate`, `high`, `normal`, `low`)
+- `type` (enum: `task`, `epic`, `bug`, `spike`, `chore`)
+- `tags` (array of strings)
+- `assigned_to` (prefixed string: `agent:{id}` / `human:{id}` / `team:{id}`)
+- `created_by` (same format)
+- `relationships_out` (array, see section 8)
+- `artifact_refs` (array of artifact IDs, optional cache only, see section 9)
+- `git_context` (object, optional cache only, see section 11)
+- `last_event_id` (string, `ev_...` — ID of the most recent event applied to this snapshot; enables O(1) drift detection by `doctor`)
+- `custom_fields` (open object, no validation in v0)
+
+### 6.2 Compact serialization (v0)
+
+- Agents can request a compact view:
+  - `id`, `title`, `status`, `priority`, `urgency`, `type`, `assigned_to`, `tags`
+  - optional counts: `relationships_out_count`, `artifact_ref_count`
+- This is the default for list/board operations to conserve tokens.
+
+### 6.3 Task types (v0)
+
+- `task`: standard unit of work
+- `epic`: a task that groups work via relationships (see `subtask_of`)
+- `bug`: defect fix
+- `spike`: research/investigation
+- `chore`: maintenance/cleanup
+
+---
+
+## 7. Status and Workflow
+
+### 7.1 Workflow is config-driven (v0)
+
+- `config.json` defines:
+  - allowed statuses
+  - allowed transitions
+  - optional WIP limits per status (advisory in v0)
+- Default workflow ships with:
+  - `backlog -> ready -> in_progress -> review -> done`
+  - plus `blocked` and `cancelled`
+
+### 7.2 Force override (v0)
+
+- Any transition not in the graph requires:
+  - `force: true`
+  - `reason: string`
+- Force transitions are recorded as events with full attribution.
+
+### 7.3 WIP limits (v0 advisory)
+
+- WIP limits are warnings only in v0.
+- Enforcement and exception rules are v1+.
+
+---
+
+## 8. Relationships
+
+### 8.1 Relationship types (v0)
+
+- `blocks`
+- `depends_on`
+- `subtask_of`
+- `related_to`
+- `spawned_by`
+- `duplicate_of`
+- `supersedes`
+
+Notes:
+- Inverses exist conceptually (ex: `blocked_by`) but are **not stored as canonical duplicated edges**.
+
+### 8.2 Canonical storage rule (v0)
+
+- Tasks store **only outgoing relationships** in `relationships_out`.
+- Reverse lookups are derived by scanning task snapshots (v0) or using an index (v1+).
+- This avoids two-file transactional updates and split-brain links.
+
+### 8.3 Relationship record shape (v0)
+
+Each item in `relationships_out` contains:
+- `type` (string)
+- `target_task_id` (string)
+- `created_at` (RFC 3339 UTC)
+- `created_by` (prefixed string)
+- `note` (optional string)
+
+### 8.4 Integrity (v0)
+
+- `lattice doctor` checks:
+  - target IDs exist (or are archived)
+  - no self-links
+  - duplicate relationships (same type + target) flagged
+
+(v1+)
+- cycle detection and critical path computation
+- richer graph queries with an index
+
+---
+
+## 9. Events (append-only, immutable)
+
+### 9.1 Storage (v0)
+
+- **Per-task JSONL** (`events/<task_id>.jsonl`): one event per line, never rewritten. This is the **authoritative** record for each task.
+- **Global JSONL** (`events/_global.jsonl`): a **derived** convenience log that aggregates lifecycle events (task created, task archived) across all tasks. It is rebuildable from per-task event logs and is not a second source of truth. If the global log and per-task logs disagree, per-task logs win.
+
+### 9.2 Event schema (v0)
+
+Required:
+- `schema_version` (int)
+- `id` (string, `ev_...`)
+- `ts` (RFC 3339 UTC)
+- `type` (string)
+- `actor` (prefixed string: `agent:{id}` / `human:{id}` / `team:{id}`)
+- `data` (object, can be empty)
+
+Conditional:
+- `task_id` (string, `task_...`) — required for task-scoped events, absent for system-scoped events
+
+Optional:
+- `agent_meta` (object):
+  - `model` (string, nullable)
+  - `session` (string, nullable)
+- `otel` (object, nullable):
+  - `trace_id`, `span_id`, `parent_span_id`
+- `metrics` (object, nullable):
+  - `tokens_in`, `tokens_out`, `cost_usd`, `latency_ms`, `tool_calls`, `retries`, `cache_hits`, `error_type`
+- `run_id` (string, nullable)
+
+### 9.3 Event types (v0)
+
+Task-scoped events (require `task_id`):
+
+- `task_created`
+- `task_archived`
+- `status_changed`:
+  - `from`, `to`, `force` (bool), `reason` (string|null)
+- `assignment_changed`:
+  - `from`, `to`
+- `field_updated`:
+  - `field`, `from`, `to`
+- `comment_added`:
+  - `body` (string)
+- `relationship_added` / `relationship_removed`:
+  - `type`, `target_task_id`
+- `artifact_attached`:
+  - `artifact_id`, `role` (optional string)
+- `git_event`:
+  - `action` (ex: `commit`)
+  - `sha` (string)
+  - `ref` (string|null)
+
+Custom event types (via `lattice log`) must be prefixed with `x_` (e.g., `x_deployment_started`). Built-in type names above are reserved.
+
+### 9.4 Telemetry and tracing posture (v0)
+
+- OTel fields exist as passthrough metadata.
+- No requirement that all agents participate.
+- No exporter required in v0.
+
+### 9.5 Tamper evidence (v1+)
+
+- Hash chaining per task event log:
+  - each event stores `prev_hash` and its own `hash`
+- Optional signature support later
+
+---
+
+## 10. Artifacts
+
+### 10.1 Artifact model (v0)
+
+Artifact metadata is authoritative for:
+- what the artifact is
+- where its payload lives
+- provenance (who created it, when, with what model)
+
+Linkage between tasks and artifacts is recorded as events (`artifact_attached`) and reflected in task snapshots as an optional cache.
+
+### 10.2 Artifact metadata fields (v0)
+
+- `schema_version` (int)
+- `id` (string, `art_...`)
+- `type` (enum, see below)
+- `title` (string)
+- `summary` (string, optional)
+- `created_at` (RFC 3339 UTC)
+- `created_by` (prefixed string)
+- `model` (string|null)
+- `tags` (array of strings)
+- `payload` (object):
+  - `file` (string|null)
+  - `content_type` (string|null)
+  - `size_bytes` (int|null)
+- `token_usage` (object|null):
+  - `tokens_in`, `tokens_out`, `cost_usd`
+- `sensitive` (bool, default false)
+- `custom_fields` (open object)
+
+### 10.3 Artifact types (v0)
+
+- `conversation` (payload: JSONL messages)
+- `prompt` (payload: text/markdown)
+- `file` (payload: raw file)
+- `log` (payload: text or JSONL)
+- `reference` (no payload file; store URL/identifier in `custom_fields`)
+
+### 10.4 Sensitive artifacts (v0)
+
+- If `sensitive: true`, payload files are gitignored by default.
+- Metadata may remain committed, but must not contain secrets.
+- Rule of thumb: secrets live only in payloads, not in task titles/descriptions.
+
+---
+
+## 11. Git Integration
+
+### 11.1 v0 posture: minimal, reliable
+
+- Primary goal: traceability without cross-platform fragility.
+- `post-commit` hook (optional) scans commit messages for `task_...` IDs.
+- When found:
+  - append a `git_event` to the task’s event log
+  - optionally update `git_context` cache on the task snapshot:
+    - `git_context.commits += [sha]`
+    - `git_context.branch` if available
+
+### 11.2 v1+ extensions
+
+- diff-based `files_touched`
+- PR integration (URLs)
+- richer repo automation
+
+---
+
+## 12. Concurrency and Integrity
+
+### 12.1 Locking rules (v0)
+
+- All writes are protected with lock files in `.lattice/locks/`.
+- Lock granularity:
+  - task snapshot file lock when rewriting `tasks/<id>.json`
+  - event log lock when appending `events/<id>.jsonl`
+  - global event log lock when appending `events/_global.jsonl`
+
+### 12.2 Deterministic lock ordering (v0)
+
+- If an operation needs multiple locks, acquire them in a deterministic order:
+  - sort by lock key (string sort)
+- This prevents deadlocks under competing agents.
+
+### 12.3 Doctor and rebuild (v0)
+
+- `lattice doctor`:
+  - validates JSON parseability
+  - detects and safely removes truncated final lines in JSONL files
+  - checks snapshot drift via `last_event_id` (O(1) consistency check)
+  - checks missing referenced files (tasks, artifacts)
+  - validates relationship targets exist or are archived
+  - flags duplicate edges and malformed IDs
+  - verifies `_global.jsonl` is consistent with per-task logs
+- `lattice rebuild <task_id|all>`:
+  - replays events to regenerate task snapshots
+  - optionally rehydrates caches (relationship counts, artifact refs, git_context)
+  - regenerates `_global.jsonl` from per-task event logs
+
+---
+
+## 13. CLI Interface
+
+### 13.1 Core commands (v0)
+
+- `lattice init`:
+  - create `.lattice/` structure and default config
+- `lattice create <title> [options]`:
+  - create task snapshot + append `task_created` event
+  - supports `--id task_...`
+- `lattice update <task_id> [field=value ...]`:
+  - append `field_updated` events + update snapshot
+- `lattice status <task_id> <new_status> [--force --reason "..."]`:
+  - validate transition via config; append `status_changed`; update snapshot
+- `lattice assign <task_id> <actor_id>`:
+  - append `assignment_changed`; update snapshot
+- `lattice comment <task_id> "<text>"`:
+  - append `comment_added`
+- `lattice list [filters]`:
+  - filters: `--status`, `--assigned`, `--tag`, `--type`
+- `lattice show <task_id> [--full]`
+- `lattice log <task_id> <event_type> [--data <json>]`:
+  - escape hatch for custom event types
+  - event type must be prefixed with `x_` (e.g., `x_deployment_started`); built-in types are rejected
+  - supports `--id ev_...`
+- `lattice attach <task_id> <file_or_url> [--type ... --title ...]`:
+  - create artifact metadata (+ payload when applicable)
+  - append `artifact_attached` to task
+  - supports `--id art_...`
+- `lattice link <task_id> <type> <target_task_id>`:
+  - append `relationship_added`; update snapshot cache `relationships_out`
+- `lattice unlink <task_id> <type> <target_task_id>`
+- `lattice archive <task_id>`:
+  - append `task_archived` event to the task's event log
+  - move task snapshot, events, notes into `archive/`
+  - update `_global.jsonl` (derived convenience log)
+- `lattice doctor`
+- `lattice rebuild <task_id|all>`
+
+### 13.2 Agent-optimized flags (v0)
+
+- `--json` output with structured envelope:
+  - success: `{"ok": true, "data": ...}`
+  - error: `{"ok": false, "error": {"code": "string", "message": "string"}}`
+- `--compact` output
+- `--quiet` (only print the ID/result)
+- attribution:
+  - `--actor=agent:...` / `--actor=human:...`
+  - `--model=...`
+  - `--session=...`
+- tracing passthrough:
+  - `--trace_id=... --span_id=... --parent_span_id=...`
+
+---
+
+## 14. Dashboard
+
+### 14.1 v0 dashboard: read-only local server
+
+- `lattice dashboard` starts a tiny local read-only HTTP server that:
+  - serves a single HTML/JS page (no build step)
+  - exposes JSON endpoints that read `.lattice/` on demand
+- Features:
+  - board view by status
+  - list view with filters
+  - task detail with event timeline
+  - recent activity feed (from global events or recent per-task events)
+
+Constraints:
+- no writes
+- binds to `127.0.0.1` only by default (no network exposure); explicit `--host` flag to override
+- no auth (acceptable because local-only by default)
+- no real-time updates required
+
+### 14.2 v1+ dashboard
+
+- dependency graph visualization
+- run drilldowns and trace trees
+- telemetry and cost views
+- “what changed” diffs
+- index-backed queries for large repos
+
+---
+
+## 15. Search
+
+### 15.1 v0 search
+
+- scan/grep across task snapshots (title, tags, status, assigned)
+- optionally include notes
+- no index required for small to medium scale
+
+### 15.2 v1+ search
+
+- full-text search across artifact payloads
+- rebuildable local index (SQLite or embedded)
+- dashboard loads in under 1s at large scale
+
+---
+
+## 16. Security and Sensitive Data
+
+### 16.1 v0
+
+- `sensitive: true` flag on artifacts
+- gitignore payloads for sensitive artifacts by default
+- no secrets in task titles/descriptions
+
+### 16.2 v1+
+
+- encryption at rest for sensitive payloads
+- redaction tools for sharing repos
+- per-agent access controls (only if/when needed)
+
+---
+
+## 17. Lifecycle and Maintenance
+
+### 17.1 Archiving (v0)
+
+- archiving moves:
+  - task snapshot
+  - task event log
+  - task notes
+- artifacts are **not moved** in v0
+  - avoids many-to-many relocation problems
+  - archived tasks can still reference artifacts by ID
+
+### 17.2 Schema evolution (v0)
+
+- every file has `schema_version`
+- forward compatibility: unknown fields must be tolerated
+- migrations are explicit tools (v1+), not silent rewrites
+
+---
+
+## 18. Design Targets
+
+### 18.1 Scale targets (v0)
+
+- up to ~10,000 active tasks
+- up to ~100,000 events/day
+- up to ~50 concurrent agents
+- artifacts payloads up to ~1 MB
+
+### 18.2 Graduation criteria (v1+)
+
+- when filesystem scanning becomes the bottleneck:
+  - add a rebuildable index first
+  - only later consider a DB backend
+- CLI interface and on-disk formats remain stable
+- storage engine can change without breaking users
+
+---
+
+## 19. Non-Goals (explicit)
+
+- not a CI/CD system
+- not a chat application (conversations are stored as artifacts)
+- not an alerting/monitoring platform (telemetry is for analysis)
+- not a code review tool
+- not an agent runtime or process manager
diff --git a/prompts/scaffold-and-init.md b/prompts/scaffold-and-init.md
new file mode 100644
index 0000000..d2123de
--- /dev/null
+++ b/prompts/scaffold-and-init.md
@@ -0,0 +1,208 @@
+# Task: Scaffold Lattice project structure and implement `lattice init`
+
+## Context
+
+Lattice is a file-based, agent-native task tracker with an event-sourced core. You are starting from a repo that has project documents and no code yet.
+
+**Key architectural facts relevant to this task:**
+- The CLI is the only write interface for authoritative state (invariant 2.2)
+- All writes use atomic operations: write temp file, fsync, rename (section 5.2)
+- All JSON output is deterministic: sorted keys, 2-space indent, trailing newline (section 5.2)
+- The CLI finds `.lattice/` by walking up from cwd; `LATTICE_ROOT` env var overrides (section 5.3)
+- `_global.jsonl` is a **derived** convenience log, not a second source of truth (section 9.1)
+
+## Before writing any code, read these files in order:
+
+1. `CLAUDE.md` — Project guide: stack, architecture, project structure, layer boundaries, coding conventions
+2. `ProjectRequirements_v1.md` — Full spec. For this task, focus on:
+   - Section 2 (System Invariants — understand all six, especially 2.2 and 2.6)
+   - Section 5 (File Layout, Format Rules, Root Discovery)
+   - Section 7.1 (Workflow config — the default config shape)
+   - Section 13.1 (`lattice init` command)
+3. `Decisions.md` — Architectural decisions with rationale (skim for context)
+
+## What to build
+
+### 1. Project scaffold
+
+Create the full Python package structure as defined in CLAUDE.md:
+
+- `pyproject.toml` — Python 3.12+, src layout, Click entry point, runtime deps (click, python-ulid, filelock), dev deps (pytest, ruff). Package name: `lattice-tracker` (to avoid PyPI conflicts). CLI entry point: `lattice = "lattice.cli.main:cli"`
+- `src/lattice/` — All subpackages with `__init__.py` files: `cli/`, `core/`, `storage/`, `dashboard/`
+- `src/lattice/cli/main.py` — Click group entry point (just the root group, no subcommands beyond `init` yet)
+- `src/lattice/core/config.py` — Default config generation and validation
+- `src/lattice/storage/fs.py` — Atomic file writes, directory creation
+- `src/lattice/core/ids.py` — Stub for ULID generation (can be minimal for now)
+- `tests/conftest.py` — Shared fixtures: `tmp_path`-based `.lattice/` directory fixture
+- `.gitignore` — Python standard ignores. Include `.lattice/` **only to prevent test artifacts from being committed to the Lattice source repo itself**. Note: users of Lattice (the product) are expected to commit their `.lattice/` directory — deterministic JSON formatting exists specifically for clean git diffs.
+
+Leave other modules as empty files with just docstrings — they'll be implemented in subsequent tasks.
+
+### 2. Root discovery utility
+
+Implement the root discovery logic (section 5.3) as a utility in `storage/` or a shared location:
+
+- Check `LATTICE_ROOT` env var first. If set, it points to the directory **containing** `.lattice/` (not `.lattice/` itself). If the env var is set but the path is invalid (doesn't exist, or exists but has no `.lattice/` inside), **fail immediately with a clear error** — do not fall back to walk-up. An explicit override that's wrong is a bug, not a hint.
+- If no env var, walk up from cwd (or a given starting path) looking for a `.lattice/` directory, stopping at the filesystem root.
+- Return the path to the directory containing `.lattice/`, or `None` if not found.
+- `lattice init` does **not** use walk-up discovery (it creates `.lattice/` in a target directory). All other future commands will use it.
+
+### 3. `lattice init` command
+
+Implement the `lattice init` command that:
+
+- Creates the `.lattice/` directory structure:
+  ```
+  .lattice/
+  ├── config.json
+  ├── tasks/
+  ├── events/
+  │   └── _global.jsonl  (empty file, ready for appends)
+  ├── artifacts/
+  │   ├── meta/
+  │   └── payload/
+  ├── notes/
+  ├── archive/
+  │   ├── tasks/
+  │   ├── events/
+  │   └── notes/
+  └── locks/
+  ```
+- Writes the default `config.json` using **atomic write** (write to temp file in same directory, fsync, rename).
+
+  **Important:** The JSON below is the exact byte-for-byte expected output of `json.dumps(config, sort_keys=True, indent=2) + "\n"`. Your `core/config.py` should produce a dict that, when serialized this way, matches exactly. Note that `sort_keys=True` puts `default_priority` before `schema_version`, and `indent=2` expands arrays to one item per line.
+
+  ```json
+  {
+    "default_priority": "medium",
+    "default_status": "backlog",
+    "schema_version": 1,
+    "task_types": [
+      "task",
+      "epic",
+      "bug",
+      "spike",
+      "chore"
+    ],
+    "workflow": {
+      "statuses": [
+        "backlog",
+        "ready",
+        "in_progress",
+        "review",
+        "done",
+        "blocked",
+        "cancelled"
+      ],
+      "transitions": {
+        "backlog": [
+          "ready",
+          "cancelled"
+        ],
+        "blocked": [
+          "ready",
+          "in_progress",
+          "cancelled"
+        ],
+        "cancelled": [],
+        "done": [],
+        "in_progress": [
+          "review",
+          "blocked",
+          "cancelled"
+        ],
+        "ready": [
+          "in_progress",
+          "blocked",
+          "cancelled"
+        ],
+        "review": [
+          "done",
+          "in_progress",
+          "cancelled"
+        ]
+      },
+      "wip_limits": {
+        "in_progress": 10,
+        "review": 5
+      }
+    }
+  }
+  ```
+
+- Is **idempotent**: running `lattice init` in a directory that already has `.lattice/` should succeed without overwriting existing config or data. Print a message like "Lattice already initialized in .lattice/" and exit 0.
+- Prints confirmation on success: "Initialized empty Lattice in .lattice/"
+- Supports `--path <dir>` to initialize in a specific directory (defaults to cwd)
+
+**Why init does not require locking:** `lattice init` either creates a new `.lattice/` directory (no existing data to protect) or detects an existing one and exits without writing (idempotent no-op). There is no concurrent-write scenario. All other commands that mutate existing files will require locks — that machinery will be implemented in later tasks.
+
+### 4. Tests
+
+Write tests for:
+
+**Init — directory structure:**
+- `lattice init` creates all expected directories (every subdirectory in the tree above)
+- `lattice init` creates an empty `_global.jsonl` file
+- `lattice init --path <dir>` works with a custom path
+
+**Init — config:**
+- `lattice init` writes valid, parseable `config.json`
+- Config has `schema_version: 1`
+- Config JSON is byte-identical to `json.dumps(default_config(), sort_keys=True, indent=2) + "\n"` (this validates sorted keys, 2-space indent, trailing newline, and array expansion all at once)
+- Config is written via atomic write pattern (verify by checking that the file exists and is valid JSON — the atomic write *implementation* is in `storage/fs.py` and its correctness is a unit test there: write to temp, fsync, rename)
+
+**Init — idempotency:**
+- Second `lattice init` run doesn't clobber existing config or data
+- Modified config survives a second `lattice init` (edit config between runs, verify edit is preserved)
+
+**Root discovery:**
+- Walks up from a nested subdirectory to find `.lattice/`
+- `LATTICE_ROOT` env var overrides walk-up
+- `LATTICE_ROOT` set to invalid path raises an error (does not fall back to walk-up)
+- Returns `None` when no `.lattice/` exists and no env var set
+
+**Atomic write (unit test for `storage/fs.py`):**
+- Writes to temp file and renames (verify no partial file at the target path during write)
+- Target file contains expected content after write
+- Target file's parent directory must exist (or error clearly)
+
+Use Click's `CliRunner` for CLI integration tests. Use `tmp_path` and `monkeypatch` for filesystem and env var isolation.
+
+## Conventions to follow
+
+- All JSON: sorted keys, 2-space indent, trailing newline (`json.dumps(data, sort_keys=True, indent=2) + "\n"`)
+- Atomic writes for config.json: write to temp file in same directory, fsync, then `os.rename`
+- Layer boundaries: `core/` has no filesystem calls, `storage/` handles I/O, `cli/` wires them together
+- Root discovery lives in `storage/` (it's filesystem I/O)
+- Config generation (the default data structure) lives in `core/config.py` (it's pure data, no I/O)
+- Keep it simple — this is the foundation, not the place for cleverness
+
+## What NOT to do
+
+- Don't implement any other CLI commands (create, update, status, etc.)
+- Don't implement event logging yet
+- Don't build the dashboard
+- Don't implement file locking yet — init doesn't need it (see rationale above), and the locking module will be built in a later task when commands that mutate existing files are implemented
+- Don't add any dependencies beyond what's listed in CLAUDE.md
+
+## Validation
+
+After implementation, run:
+```bash
+uv venv && uv pip install -e ".[dev]"
+uv run pytest -v
+uv run ruff check src/ tests/
+uv run ruff format --check src/ tests/
+uv run lattice --help
+uv run lattice init
+ls -la .lattice/
+cat .lattice/config.json
+uv run lattice init  # second run should be idempotent
+```
+
+All tests should pass. Ruff should be clean (both check and format). Both init runs should succeed. The `cat` output should be byte-identical to the expected JSON above (sorted keys, expanded arrays, trailing newline).
+
+Clean up after validation:
+```bash
+rm -rf .lattice/  # remove the test .lattice/ from the repo root
+```
diff --git a/pyproject.toml b/pyproject.toml
new file mode 100644
index 0000000..b24f88a
--- /dev/null
+++ b/pyproject.toml
@@ -0,0 +1,33 @@
+[build-system]
+requires = ["hatchling"]
+build-backend = "hatchling.build"
+
+[project]
+name = "lattice-tracker"
+version = "0.1.0"
+description = "File-based, agent-native task tracker with an event-sourced core."
+requires-python = ">=3.12"
+dependencies = [
+    "click>=8.1",
+    "python-ulid>=2.0",
+    "filelock>=3.13",
+]
+
+[project.optional-dependencies]
+dev = [
+    "pytest>=8.0",
+    "ruff>=0.4",
+]
+
+[project.scripts]
+lattice = "lattice.cli.main:cli"
+
+[tool.hatch.build.targets.wheel]
+packages = ["src/lattice"]
+
+[tool.pytest.ini_options]
+testpaths = ["tests"]
+
+[tool.ruff]
+src = ["src"]
+line-length = 99
diff --git a/src/lattice/__init__.py b/src/lattice/__init__.py
new file mode 100644
index 0000000..c999f70
--- /dev/null
+++ b/src/lattice/__init__.py
@@ -0,0 +1 @@
+"""Lattice: file-based, agent-native task tracker with an event-sourced core."""
diff --git a/src/lattice/cli/__init__.py b/src/lattice/cli/__init__.py
new file mode 100644
index 0000000..cb5739a
--- /dev/null
+++ b/src/lattice/cli/__init__.py
@@ -0,0 +1 @@
+"""CLI command groups."""
diff --git a/src/lattice/cli/main.py b/src/lattice/cli/main.py
new file mode 100644
index 0000000..e6b1343
--- /dev/null
+++ b/src/lattice/cli/main.py
@@ -0,0 +1,48 @@
+"""CLI entry point and commands."""
+
+from __future__ import annotations
+
+from pathlib import Path
+
+import click
+
+from lattice.core.config import default_config, serialize_config
+from lattice.storage.fs import LATTICE_DIR, atomic_write, ensure_lattice_dirs
+
+
+@click.group()
+def cli() -> None:
+    """Lattice: file-based, agent-native task tracker."""
+
+
+@cli.command()
+@click.option(
+    "--path",
+    "target_path",
+    type=click.Path(exists=True, file_okay=False, resolve_path=True),
+    default=".",
+    help="Directory to initialize Lattice in (defaults to current directory).",
+)
+def init(target_path: str) -> None:
+    """Initialize a new Lattice project."""
+    root = Path(target_path)
+    lattice_dir = root / LATTICE_DIR
+
+    # Idempotency: if .lattice/ already exists, exit without touching anything
+    if lattice_dir.is_dir():
+        click.echo(f"Lattice already initialized in {LATTICE_DIR}/")
+        return
+
+    # Create directory structure
+    ensure_lattice_dirs(root)
+
+    # Write default config atomically
+    config = default_config()
+    config_content = serialize_config(config)
+    atomic_write(lattice_dir / "config.json", config_content)
+
+    click.echo(f"Initialized empty Lattice in {LATTICE_DIR}/")
+
+
+if __name__ == "__main__":
+    cli()
diff --git a/src/lattice/core/__init__.py b/src/lattice/core/__init__.py
new file mode 100644
index 0000000..cfd5fbf
--- /dev/null
+++ b/src/lattice/core/__init__.py
@@ -0,0 +1 @@
+"""Core business logic (no filesystem I/O)."""
diff --git a/src/lattice/core/artifacts.py b/src/lattice/core/artifacts.py
new file mode 100644
index 0000000..8d37d3f
--- /dev/null
+++ b/src/lattice/core/artifacts.py
@@ -0,0 +1 @@
+"""Artifact metadata and linkage."""
diff --git a/src/lattice/core/config.py b/src/lattice/core/config.py
new file mode 100644
index 0000000..45ba178
--- /dev/null
+++ b/src/lattice/core/config.py
@@ -0,0 +1,53 @@
+"""Default config generation and validation."""
+
+import json
+
+
+def default_config() -> dict:
+    """Return the default Lattice configuration.
+
+    The returned dict, when serialized with
+    ``json.dumps(data, sort_keys=True, indent=2) + "\\n"``,
+    produces the canonical default config.json.
+    """
+    return {
+        "schema_version": 1,
+        "default_status": "backlog",
+        "default_priority": "medium",
+        "task_types": [
+            "task",
+            "epic",
+            "bug",
+            "spike",
+            "chore",
+        ],
+        "workflow": {
+            "statuses": [
+                "backlog",
+                "ready",
+                "in_progress",
+                "review",
+                "done",
+                "blocked",
+                "cancelled",
+            ],
+            "transitions": {
+                "backlog": ["ready", "cancelled"],
+                "ready": ["in_progress", "blocked", "cancelled"],
+                "in_progress": ["review", "blocked", "cancelled"],
+                "review": ["done", "in_progress", "cancelled"],
+                "done": [],
+                "cancelled": [],
+                "blocked": ["ready", "in_progress", "cancelled"],
+            },
+            "wip_limits": {
+                "in_progress": 10,
+                "review": 5,
+            },
+        },
+    }
+
+
+def serialize_config(config: dict) -> str:
+    """Serialize a config dict to the canonical JSON format."""
+    return json.dumps(config, sort_keys=True, indent=2) + "\n"
diff --git a/src/lattice/core/events.py b/src/lattice/core/events.py
new file mode 100644
index 0000000..bc25900
--- /dev/null
+++ b/src/lattice/core/events.py
@@ -0,0 +1 @@
+"""Event creation, schema, and types."""
diff --git a/src/lattice/core/ids.py b/src/lattice/core/ids.py
new file mode 100644
index 0000000..2814fcb
--- /dev/null
+++ b/src/lattice/core/ids.py
@@ -0,0 +1,18 @@
+"""ULID generation and validation."""
+
+from ulid import ULID
+
+
+def generate_task_id() -> str:
+    """Generate a new task ID with the task_ prefix."""
+    return f"task_{ULID()}"
+
+
+def generate_event_id() -> str:
+    """Generate a new event ID with the ev_ prefix."""
+    return f"ev_{ULID()}"
+
+
+def generate_artifact_id() -> str:
+    """Generate a new artifact ID with the art_ prefix."""
+    return f"art_{ULID()}"
diff --git a/src/lattice/core/relationships.py b/src/lattice/core/relationships.py
new file mode 100644
index 0000000..4d6073a
--- /dev/null
+++ b/src/lattice/core/relationships.py
@@ -0,0 +1 @@
+"""Relationship types and validation."""
diff --git a/src/lattice/core/tasks.py b/src/lattice/core/tasks.py
new file mode 100644
index 0000000..5504874
--- /dev/null
+++ b/src/lattice/core/tasks.py
@@ -0,0 +1 @@
+"""Task CRUD and snapshot materialization."""
diff --git a/src/lattice/dashboard/__init__.py b/src/lattice/dashboard/__init__.py
new file mode 100644
index 0000000..25e28c4
--- /dev/null
+++ b/src/lattice/dashboard/__init__.py
@@ -0,0 +1 @@
+"""Read-only local web dashboard."""
diff --git a/src/lattice/dashboard/server.py b/src/lattice/dashboard/server.py
new file mode 100644
index 0000000..9187292
--- /dev/null
+++ b/src/lattice/dashboard/server.py
@@ -0,0 +1 @@
+"""Read-only HTTP server for the Lattice dashboard."""
diff --git a/src/lattice/dashboard/static/.gitkeep b/src/lattice/dashboard/static/.gitkeep
new file mode 100644
index 0000000..e69de29
diff --git a/src/lattice/storage/__init__.py b/src/lattice/storage/__init__.py
new file mode 100644
index 0000000..0c7a4a7
--- /dev/null
+++ b/src/lattice/storage/__init__.py
@@ -0,0 +1 @@
+"""Filesystem storage operations."""
diff --git a/src/lattice/storage/fs.py b/src/lattice/storage/fs.py
new file mode 100644
index 0000000..2b2f348
--- /dev/null
+++ b/src/lattice/storage/fs.py
@@ -0,0 +1,113 @@
+"""Atomic file writes, directory management, and root discovery."""
+
+from __future__ import annotations
+
+import os
+import tempfile
+from pathlib import Path
+
+LATTICE_DIR = ".lattice"
+LATTICE_ROOT_ENV = "LATTICE_ROOT"
+
+
+def atomic_write(path: Path, content: str | bytes) -> None:
+    """Write content to path atomically via temp file + fsync + rename.
+
+    The temp file is created in the same directory as the target to ensure
+    os.rename() is an atomic operation (same filesystem).
+
+    Raises:
+        FileNotFoundError: If the parent directory does not exist.
+    """
+    parent = path.parent
+    if not parent.is_dir():
+        raise FileNotFoundError(f"Parent directory does not exist: {parent}")
+
+    data = content.encode("utf-8") if isinstance(content, str) else content
+
+    fd, tmp_path = tempfile.mkstemp(dir=parent, prefix=".tmp.")
+    closed = False
+    try:
+        os.write(fd, data)
+        os.fsync(fd)
+        os.close(fd)
+        closed = True
+        os.replace(tmp_path, path)
+    except BaseException:
+        if not closed:
+            os.close(fd)
+        try:
+            os.unlink(tmp_path)
+        except OSError:
+            pass
+        raise
+
+
+def ensure_lattice_dirs(root: Path) -> None:
+    """Create the full .lattice/ directory structure under root.
+
+    root is the project directory (the directory that will contain .lattice/).
+    """
+    lattice = root / LATTICE_DIR
+    subdirs = [
+        "tasks",
+        "events",
+        "artifacts/meta",
+        "artifacts/payload",
+        "notes",
+        "archive/tasks",
+        "archive/events",
+        "archive/notes",
+        "locks",
+    ]
+    for subdir in subdirs:
+        (lattice / subdir).mkdir(parents=True, exist_ok=True)
+
+    # Create empty _global.jsonl ready for appends
+    global_log = lattice / "events" / "_global.jsonl"
+    if not global_log.exists():
+        global_log.touch()
+
+
+def find_root(start: Path | None = None) -> Path | None:
+    """Find the project root containing .lattice/.
+
+    Checks LATTICE_ROOT env var first. If set, validates it and returns
+    the path or raises an error (no fallback to walk-up).
+
+    Otherwise, walks up from start (defaults to cwd) looking for .lattice/.
+
+    Returns:
+        Path to the directory containing .lattice/, or None if not found.
+
+    Raises:
+        LatticeRootError: If LATTICE_ROOT is set but invalid.
+    """
+    env_root = os.environ.get(LATTICE_ROOT_ENV)
+    if env_root is not None:
+        if not env_root:
+            raise LatticeRootError("LATTICE_ROOT is set but empty")
+        env_path = Path(env_root)
+        if not env_path.is_dir():
+            raise LatticeRootError(
+                f"LATTICE_ROOT points to a path that does not exist: {env_root}"
+            )
+        if not (env_path / LATTICE_DIR).is_dir():
+            raise LatticeRootError(
+                f"LATTICE_ROOT points to a directory with no {LATTICE_DIR}/ inside: {env_root}"
+            )
+        return env_path
+
+    current = (start or Path.cwd()).resolve()
+    while True:
+        if (current / LATTICE_DIR).is_dir():
+            return current
+        parent = current.parent
+        if parent == current:
+            # Reached filesystem root
+            return None
+        current = parent
+
+
+class LatticeRootError(Exception):
+    """Raised when LATTICE_ROOT env var is set but invalid."""
diff --git a/src/lattice/storage/locks.py b/src/lattice/storage/locks.py
new file mode 100644
index 0000000..ff3a941
--- /dev/null
+++ b/src/lattice/storage/locks.py
@@ -0,0 +1 @@
+"""File locking and deterministic lock ordering."""
diff --git a/tests/__init__.py b/tests/__init__.py
new file mode 100644
index 0000000..e69de29
diff --git a/tests/conftest.py b/tests/conftest.py
new file mode 100644
index 0000000..4674211
--- /dev/null
+++ b/tests/conftest.py
@@ -0,0 +1,26 @@
+"""Shared test fixtures."""
+
+from __future__ import annotations
+
+from pathlib import Path
+
+import pytest
+
+
+@pytest.fixture()
+def lattice_root(tmp_path: Path) -> Path:
+    """Return a temporary directory suitable for initializing .lattice/ in."""
+    return tmp_path
+
+
+@pytest.fixture()
+def initialized_root(lattice_root: Path) -> Path:
+    """Return a temporary directory with .lattice/ already initialized."""
+    from lattice.storage.fs import ensure_lattice_dirs, atomic_write, LATTICE_DIR
+    from lattice.core.config import default_config, serialize_config
+
+    ensure_lattice_dirs(lattice_root)
+    lattice_dir = lattice_root / LATTICE_DIR
+    atomic_write(lattice_dir / "config.json", serialize_config(default_config()))
+    (lattice_dir / "events" / "_global.jsonl").touch()
+    return lattice_root
diff --git a/tests/test_cli/__init__.py b/tests/test_cli/__init__.py
new file mode 100644
index 0000000..e69de29
diff --git a/tests/test_cli/test_init.py b/tests/test_cli/test_init.py
new file mode 100644
index 0000000..aae183d
--- /dev/null
+++ b/tests/test_cli/test_init.py
@@ -0,0 +1,147 @@
+"""Tests for the `lattice init` CLI command."""
+
+from __future__ import annotations
+
+import json
+from pathlib import Path
+
+from click.testing import CliRunner
+
+from lattice.cli.main import cli
+from lattice.core.config import default_config, serialize_config
+
+
+class TestInitDirectoryStructure:
+    """lattice init creates the full .lattice/ directory tree."""
+
+    def test_creates_all_expected_directories(self, tmp_path: Path) -> None:
+        runner = CliRunner()
+        result = runner.invoke(cli, ["init", "--path", str(tmp_path)])
+        assert result.exit_code == 0
+
+        lattice = tmp_path / ".lattice"
+        expected_dirs = [
+            "tasks",
+            "events",
+            "artifacts/meta",
+            "artifacts/payload",
+            "notes",
+            "archive/tasks",
+            "archive/events",
+            "archive/notes",
+            "locks",
+        ]
+        for d in expected_dirs:
+            assert (lattice / d).is_dir(), f"Missing directory: {d}"
+
+    def test_creates_empty_global_jsonl(self, tmp_path: Path) -> None:
+        runner = CliRunner()
+        runner.invoke(cli, ["init", "--path", str(tmp_path)])
+
+        global_log = tmp_path / ".lattice" / "events" / "_global.jsonl"
+        assert global_log.is_file()
+        assert global_log.read_text() == ""
+
+    def test_init_with_custom_path(self, tmp_path: Path) -> None:
+        target = tmp_path / "myproject"
+        target.mkdir()
+
+        runner = CliRunner()
+        result = runner.invoke(cli, ["init", "--path", str(target)])
+        assert result.exit_code == 0
+        assert (target / ".lattice" / "config.json").is_file()
+
+    def test_prints_success_message(self, tmp_path: Path) -> None:
+        runner = CliRunner()
+        result = runner.invoke(cli, ["init", "--path", str(tmp_path)])
+        assert result.exit_code == 0
+        assert "Initialized empty Lattice in .lattice/" in result.output
+
+
+class TestInitConfig:
+    """lattice init writes a valid, deterministic config.json."""
+
+    def test_writes_valid_json(self, tmp_path: Path) -> None:
+        runner = CliRunner()
+        runner.invoke(cli, ["init", "--path", str(tmp_path)])
+
+        config_path = tmp_path / ".lattice" / "config.json"
+        config = json.loads(config_path.read_text())
+        assert isinstance(config, dict)
+
+    def test_config_has_schema_version_1(self, tmp_path: Path) -> None:
+        runner = CliRunner()
+        runner.invoke(cli, ["init", "--path", str(tmp_path)])
+
+        config = json.loads((tmp_path / ".lattice" / "config.json").read_text())
+        assert config["schema_version"] == 1
+
+    def test_config_is_byte_identical_to_canonical(self, tmp_path: Path) -> None:
+        """Config on disk must be byte-identical to json.dumps(default_config(), sort_keys=True, indent=2) + '\\n'."""
+        runner = CliRunner()
+        runner.invoke(cli, ["init", "--path", str(tmp_path)])
+
+        actual = (tmp_path / ".lattice" / "config.json").read_text()
+        expected = serialize_config(default_config())
+        assert actual == expected
+
+    def test_config_has_trailing_newline(self, tmp_path: Path) -> None:
+        runner = CliRunner()
+        runner.invoke(cli, ["init", "--path", str(tmp_path)])
+
+        raw = (tmp_path / ".lattice" / "config.json").read_bytes()
+        assert raw.endswith(b"\n")
+        # Exactly one trailing newline, not two
+        assert not raw.endswith(b"\n\n")
+
+
+class TestInitIdempotency:
+    """Running init twice must not clobber existing data."""
+
+    def test_second_init_does_not_clobber_config(self, tmp_path: Path) -> None:
+        runner = CliRunner()
+        runner.invoke(cli, ["init", "--path", str(tmp_path)])
+
+        # Record config content after first init
+        config_path = tmp_path / ".lattice" / "config.json"
+        original = config_path.read_text()
+
+        # Run init again
+        result = runner.invoke(cli, ["init", "--path", str(tmp_path)])
+        assert result.exit_code == 0
+        assert "already initialized" in result.output
+
+        # Config unchanged
+        assert config_path.read_text() == original
+
+    def test_modified_config_survives_second_init(self, tmp_path: Path) -> None:
+        runner = CliRunner()
+        runner.invoke(cli, ["init", "--path", str(tmp_path)])
+
+        # Modify config between runs
+        config_path = tmp_path / ".lattice" / "config.json"
+        config = json.loads(config_path.read_text())
+        config["custom_key"] = "user_value"
+        config_path.write_text(json.dumps(config, sort_keys=True, indent=2) + "\n")
+
+        # Run init again
+        runner.invoke(cli, ["init", "--path", str(tmp_path)])
+
+        # Modified config is preserved
+        reloaded = json.loads(config_path.read_text())
+        assert reloaded["custom_key"] == "user_value"
+
+    def test_existing_tasks_survive_second_init(self, tmp_path: Path) -> None:
+        runner = CliRunner()
+        runner.invoke(cli, ["init", "--path", str(tmp_path)])
+
+        # Create a fake task file
+        task_file = tmp_path / ".lattice" / "tasks" / "task_fake.json"
+        task_file.write_text('{"id": "task_fake"}\n')
+
+        # Run init again
+        runner.invoke(cli, ["init", "--path", str(tmp_path)])
+
+        # Task file still exists
+        assert task_file.is_file()
+        assert json.loads(task_file.read_text())["id"] == "task_fake"
diff --git a/tests/test_core/__init__.py b/tests/test_core/__init__.py
new file mode 100644
index 0000000..e69de29
diff --git a/tests/test_core/test_config.py b/tests/test_core/test_config.py
new file mode 100644
index 0000000..69cd7bd
--- /dev/null
+++ b/tests/test_core/test_config.py
@@ -0,0 +1,89 @@
+"""Tests for core config module."""
+
+from __future__ import annotations
+
+import json
+
+from lattice.core.config import default_config, serialize_config
+
+
+class TestDefaultConfig:
+    """default_config() returns a well-formed configuration dict."""
+
+    def test_has_schema_version(self) -> None:
+        config = default_config()
+        assert config["schema_version"] == 1
+
+    def test_has_default_status(self) -> None:
+        config = default_config()
+        assert config["default_status"] == "backlog"
+
+    def test_has_default_priority(self) -> None:
+        config = default_config()
+        assert config["default_priority"] == "medium"
+
+    def test_has_task_types(self) -> None:
+        config = default_config()
+        assert config["task_types"] == ["task", "epic", "bug", "spike", "chore"]
+
+    def test_workflow_statuses(self) -> None:
+        config = default_config()
+        expected = ["backlog", "ready", "in_progress", "review", "done", "blocked", "cancelled"]
+        assert config["workflow"]["statuses"] == expected
+
+    def test_workflow_transitions_keys(self) -> None:
+        config = default_config()
+        transitions = config["workflow"]["transitions"]
+        expected_keys = {
+            "backlog",
+            "ready",
+            "in_progress",
+            "review",
+            "done",
+            "cancelled",
+            "blocked",
+        }
+        assert set(transitions.keys()) == expected_keys
+
+    def test_terminal_statuses_have_no_transitions(self) -> None:
+        config = default_config()
+        transitions = config["workflow"]["transitions"]
+        assert transitions["done"] == []
+        assert transitions["cancelled"] == []
+
+    def test_wip_limits(self) -> None:
+        config = default_config()
+        wip = config["workflow"]["wip_limits"]
+        assert wip == {"in_progress": 10, "review": 5}
+
+
+class TestSerializeConfig:
+    """serialize_config() produces deterministic canonical JSON."""
+
+    def test_sorted_keys(self) -> None:
+        config = default_config()
+        serialized = serialize_config(config)
+        parsed = json.loads(serialized)
+        # Re-serialize with sort_keys to verify roundtrip
+        reserialized = json.dumps(parsed, sort_keys=True, indent=2) + "\n"
+        assert serialized == reserialized
+
+    def test_trailing_newline(self) -> None:
+        config = default_config()
+        serialized = serialize_config(config)
+        assert serialized.endswith("\n")
+        assert not serialized.endswith("\n\n")
+
+    def test_two_space_indent(self) -> None:
+        config = default_config()
+        serialized = serialize_config(config)
+        # Second line should start with exactly 2 spaces (first key)
+        lines = serialized.split("\n")
+        assert lines[1].startswith("  ")
+        assert not lines[1].startswith("    ")
+
+    def test_roundtrip(self) -> None:
+        config = default_config()
+        serialized = serialize_config(config)
+        parsed = json.loads(serialized)
+        assert parsed == config
diff --git a/tests/test_storage/__init__.py b/tests/test_storage/__init__.py
new file mode 100644
index 0000000..e69de29
diff --git a/tests/test_storage/test_fs.py b/tests/test_storage/test_fs.py
new file mode 100644
index 0000000..7212156
--- /dev/null
+++ b/tests/test_storage/test_fs.py
@@ -0,0 +1,55 @@
+"""Tests for atomic write operations."""
+
+from __future__ import annotations
+
+import os
+from pathlib import Path
+
+import pytest
+
+from lattice.storage.fs import atomic_write
+
+
+class TestAtomicWrite:
+    """atomic_write() writes content safely via temp + fsync + rename."""
+
+    def test_writes_expected_content(self, tmp_path: Path) -> None:
+        target = tmp_path / "output.json"
+        atomic_write(target, '{"key": "value"}\n')
+
+        assert target.read_text() == '{"key": "value"}\n'
+
+    def test_writes_bytes_content(self, tmp_path: Path) -> None:
+        target = tmp_path / "output.bin"
+        data = b"\x00\x01\x02\x03"
+        atomic_write(target, data)
+
+        assert target.read_bytes() == data
+
+    def test_no_temp_file_left_after_success(self, tmp_path: Path) -> None:
+        target = tmp_path / "output.json"
+        atomic_write(target, "content\n")
+
+        # Only the target file should exist
+        files = list(tmp_path.iterdir())
+        assert files == [target]
+
+    def test_overwrites_existing_file(self, tmp_path: Path) -> None:
+        target = tmp_path / "output.json"
+        target.write_text("old content\n")
+
+        atomic_write(target, "new content\n")
+        assert target.read_text() == "new content\n"
+
+    def test_parent_directory_must_exist(self, tmp_path: Path) -> None:
+        target = tmp_path / "nonexistent" / "output.json"
+
+        with pytest.raises(FileNotFoundError, match="Parent directory does not exist"):
+            atomic_write(target, "content\n")
+
+    def test_file_permissions_are_readable(self, tmp_path: Path) -> None:
+        target = tmp_path / "output.json"
+        atomic_write(target, "content\n")
+
+        # File should be readable
+        assert os.access(target, os.R_OK)
diff --git a/tests/test_storage/test_root_discovery.py b/tests/test_storage/test_root_discovery.py
new file mode 100644
index 0000000..4feca6a
--- /dev/null
+++ b/tests/test_storage/test_root_discovery.py
@@ -0,0 +1,96 @@
+"""Tests for root discovery logic."""
+
+from __future__ import annotations
+
+from pathlib import Path
+
+import pytest
+
+from lattice.storage.fs import LATTICE_DIR, LatticeRootError, find_root
+
+
+class TestFindRootWalkUp:
+    """find_root() walks up from a starting path to find .lattice/."""
+
+    def test_finds_lattice_in_current_dir(self, tmp_path: Path) -> None:
+        (tmp_path / LATTICE_DIR).mkdir()
+        result = find_root(start=tmp_path)
+        assert result == tmp_path
+
+    def test_finds_lattice_in_parent_dir(self, tmp_path: Path) -> None:
+        (tmp_path / LATTICE_DIR).mkdir()
+        nested = tmp_path / "a" / "b" / "c"
+        nested.mkdir(parents=True)
+
+        result = find_root(start=nested)
+        assert result == tmp_path
+
+    def test_returns_none_when_not_found(self, tmp_path: Path) -> None:
+        # tmp_path has no .lattice/ — walk up should eventually hit root and return None
+        # Use a nested dir to avoid accidentally finding a real .lattice/ on the system
+        isolated = tmp_path / "isolated"
+        isolated.mkdir()
+        result = find_root(start=isolated)
+        assert result is None
+
+
+class TestFindRootEnvVar:
+    """LATTICE_ROOT env var overrides walk-up discovery."""
+
+    def test_env_var_overrides_walk_up(
+        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
+    ) -> None:
+        # Create .lattice/ in the env var target
+        env_target = tmp_path / "env_root"
+        env_target.mkdir()
+        (env_target / LATTICE_DIR).mkdir()
+
+        monkeypatch.setenv("LATTICE_ROOT", str(env_target))
+
+        # Even when starting from a different path, env var wins
+        other = tmp_path / "other"
+        other.mkdir()
+        result = find_root(start=other)
+        assert result == env_target
+
+    def test_env_var_nonexistent_path_raises(
+        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
+    ) -> None:
+        monkeypatch.setenv("LATTICE_ROOT", str(tmp_path / "does_not_exist"))
+
+        with pytest.raises(LatticeRootError, match="does not exist"):
+            find_root(start=tmp_path)
+
+    def test_env_var_no_lattice_dir_raises(
+        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
+    ) -> None:
+        # Directory exists but has no .lattice/ inside
+        env_target = tmp_path / "empty_root"
+        env_target.mkdir()
+
+        monkeypatch.setenv("LATTICE_ROOT", str(env_target))
+
+        with pytest.raises(LatticeRootError, match="no .lattice/"):
+            find_root(start=tmp_path)
+
+    def test_env_var_invalid_does_not_fall_back(
+        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
+    ) -> None:
+        """When LATTICE_ROOT is set but invalid, do NOT fall back to walk-up."""
+        # Create .lattice/ that walk-up would find
+        (tmp_path / LATTICE_DIR).mkdir()
+
+        # But set env var to a bad path
+        monkeypatch.setenv("LATTICE_ROOT", str(tmp_path / "bad"))
+
+        with pytest.raises(LatticeRootError):
+            find_root(start=tmp_path)
+
+    def test_env_var_empty_string_raises(
+        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
+    ) -> None:
+        """Empty LATTICE_ROOT is an error, not a silent cwd fallback."""
+        monkeypatch.setenv("LATTICE_ROOT", "")
+
+        with pytest.raises(LatticeRootError, match="empty"):
+            find_root(start=tmp_path)
diff --git a/uv.lock b/uv.lock
new file mode 100644
index 0000000..00bdb97
--- /dev/null
+++ b/uv.lock
@@ -0,0 +1,145 @@
+version = 1
+revision = 3
+requires-python = ">=3.12"
+
+[[package]]
+name = "click"
+version = "8.3.1"
+source = { registry = "https://pypi.org/simple" }
+dependencies = [
+    { name = "colorama", marker = "sys_platform == 'win32'" },
+]
+sdist = { url = "https://files.pythonhosted.org/packages/3d/fa/656b739db8587d7b5dfa22e22ed02566950fbfbcdc20311993483657a5c0/click-8.3.1.tar.gz", hash = "sha256:12ff4785d337a1bb490bb7e9c2b1ee5da3112e94a8622f26a6c77f5d2fc6842a", size = 295065, upload-time = "2025-11-15T20:45:42.706Z" }
+wheels = [
+    { url = "https://files.pythonhosted.org/packages/98/78/01c019cdb5d6498122777c1a43056ebb3ebfeef2076d9d026bfe15583b2b/click-8.3.1-py3-none-any.whl", hash = "sha256:981153a64e25f12d547d3426c367a4857371575ee7ad18df2a6183ab0545b2a6", size = 108274, upload-time = "2025-11-15T20:45:41.139Z" },
+]
+
+[[package]]
+name = "colorama"
+version = "0.4.6"
+source = { registry = "https://pypi.org/simple" }
+sdist = { url = "https://files.pythonhosted.org/packages/d8/53/6f443c9a4a8358a93a6792e2acffb9d9d5cb0a5cfd8802644b7b1c9a02e4/colorama-0.4.6.tar.gz", hash = "sha256:08695f5cb7ed6e0531a20572697297273c47b8cae5a63ffc6d6ed5c201be6e44", size = 27697, upload-time = "2022-10-25T02:36:22.414Z" }
+wheels = [
+    { url = "https://files.pythonhosted.org/packages/d1/d6/3965ed04c63042e047cb6a3e6ed1a63a35087b6a609aa3a15ed8ac56c221/colorama-0.4.6-py2.py3-none-any.whl", hash = "sha256:4f1d9991f5acc0ca119f9d443620b77f9d6b33703e51011c16baf57afb285fc6", size = 25335, upload-time = "2022-10-25T02:36:20.889Z" },
+]
+
+[[package]]
+name = "filelock"
+version = "3.24.0"
+source = { registry = "https://pypi.org/simple" }
+sdist = { url = "https://files.pythonhosted.org/packages/00/cd/fa3ab025a8f9772e8a9146d8fd8eef6d62649274d231ca84249f54a0de4a/filelock-3.24.0.tar.gz", hash = "sha256:aeeab479339ddf463a1cdd1f15a6e6894db976071e5883efc94d22ed5139044b", size = 37166, upload-time = "2026-02-14T16:05:28.723Z" }
+wheels = [
+    { url = "https://files.pythonhosted.org/packages/d9/dd/d7e7f4f49180e8591c9e1281d15ecf8e7f25eb2c829771d9682f1f9fe0c8/filelock-3.24.0-py3-none-any.whl", hash = "sha256:eebebb403d78363ef7be8e236b63cc6760b0004c7464dceaba3fd0afbd637ced", size = 23977, upload-time = "2026-02-14T16:05:27.578Z" },
+]
+
+[[package]]
+name = "iniconfig"
+version = "2.3.0"
+source = { registry = "https://pypi.org/simple" }
+sdist = { url = "https://files.pythonhosted.org/packages/72/34/14ca021ce8e5dfedc35312d08ba8bf51fdd999c576889fc2c24cb97f4f10/iniconfig-2.3.0.tar.gz", hash = "sha256:c76315c77db068650d49c5b56314774a7804df16fee4402c1f19d6d15d8c4730", size = 20503, upload-time = "2025-10-18T21:55:43.219Z" }
+wheels = [
+    { url = "https://files.pythonhosted.org/packages/cb/b1/3846dd7f199d53cb17f49cba7e651e9ce294d8497c8c150530ed11865bb8/iniconfig-2.3.0-py3-none-any.whl", hash = "sha256:f631c04d2c48c52b84d0d0549c99ff3859c98df65b3101406327ecc7d53fbf12", size = 7484, upload-time = "2025-10-18T21:55:41.639Z" },
+]
+
+[[package]]
+name = "lattice-tracker"
+version = "0.1.0"
+source = { editable = "." }
+dependencies = [
+    { name = "click" },
+    { name = "filelock" },
+    { name = "python-ulid" },
+]
+
+[package.optional-dependencies]
+dev = [
+    { name = "pytest" },
+    { name = "ruff" },
+]
+
+[package.metadata]
+requires-dist = [
+    { name = "click", specifier = ">=8.1" },
+    { name = "filelock", specifier = ">=3.13" },
+    { name = "pytest", marker = "extra == 'dev'", specifier = ">=8.0" },
+    { name = "python-ulid", specifier = ">=2.0" },
+    { name = "ruff", marker = "extra == 'dev'", specifier = ">=0.4" },
+]
+provides-extras = ["dev"]
+
+[[package]]
+name = "packaging"
+version = "26.0"
+source = { registry = "https://pypi.org/simple" }
+sdist = { url = "https://files.pythonhosted.org/packages/65/ee/299d360cdc32edc7d2cf530f3accf79c4fca01e96ffc950d8a52213bd8e4/packaging-26.0.tar.gz", hash = "sha256:00243ae351a257117b6a241061796684b084ed1c516a08c48a3f7e147a9d80b4", size = 143416, upload-time = "2026-01-21T20:50:39.064Z" }
+wheels = [
+    { url = "https://files.pythonhosted.org/packages/b7/b9/c538f279a4e237a006a2c98387d081e9eb060d203d8ed34467cc0f0b9b53/packaging-26.0-py3-none-any.whl", hash = "sha256:b36f1fef9334a5588b4166f8bcd26a14e521f2b55e6b9de3aaa80d3ff7a37529", size = 74366, upload-time = "2026-01-21T20:50:37.788Z" },
+]
+
+[[package]]
+name = "pluggy"
+version = "1.6.0"
+source = { registry = "https://pypi.org/simple" }
+sdist = { url = "https://files.pythonhosted.org/packages/f9/e2/3e91f31a7d2b083fe6ef3fa267035b518369d9511ffab804f839851d2779/pluggy-1.6.0.tar.gz", hash = "sha256:7dcc130b76258d33b90f61b658791dede3486c3e6bfb003ee5c9bfb396dd22f3", size = 69412, upload-time = "2025-05-15T12:30:07.975Z" }
+wheels = [
+    { url = "https://files.pythonhosted.org/packages/54/20/4d324d65cc6d9205fabedc306948156824eb9f0ee1633355a8f7ec5c66bf/pluggy-1.6.0-py3-none-any.whl", hash = "sha256:e920276dd6813095e9377c0bc5566d94c932c33b27a3e3945d8389c374dd4746", size = 20538, upload-time = "2025-05-15T12:30:06.134Z" },
+]
+
+[[package]]
+name = "pygments"
+version = "2.19.2"
+source = { registry = "https://pypi.org/simple" }
+sdist = { url = "https://files.pythonhosted.org/packages/b0/77/a5b8c569bf593b0140bde72ea885a803b82086995367bf2037de0159d924/pygments-2.19.2.tar.gz", hash = "sha256:636cb2477cec7f8952536970bc533bc43743542f70392ae026374600add5b887", size = 4968631, upload-time = "2025-06-21T13:39:12.283Z" }
+wheels = [
+    { url = "https://files.pythonhosted.org/packages/c7/21/705964c7812476f378728bdf590ca4b771ec72385c533964653c68e86bdc/pygments-2.19.2-py3-none-any.whl", hash = "sha256:86540386c03d588bb81d44bc3928634ff26449851e99741617ecb9037ee5ec0b", size = 1225217, upload-time = "2025-06-21T13:39:07.939Z" },
+]
+
+[[package]]
+name = "pytest"
+version = "9.0.2"
+source = { registry = "https://pypi.org/simple" }
+dependencies = [
+    { name = "colorama", marker = "sys_platform == 'win32'" },
+    { name = "iniconfig" },
+    { name = "packaging" },
+    { name = "pluggy" },
+    { name = "pygments" },
+]
+sdist = { url = "https://files.pythonhosted.org/packages/d1/db/7ef3487e0fb0049ddb5ce41d3a49c235bf9ad299b6a25d5780a89f19230f/pytest-9.0.2.tar.gz", hash = "sha256:75186651a92bd89611d1d9fc20f0b4345fd827c41ccd5c299a868a05d70edf11", size = 1568901, upload-time = "2025-12-06T21:30:51.014Z" }
+wheels = [
+    { url = "https://files.pythonhosted.org/packages/3b/ab/b3226f0bd7cdcf710fbede2b3548584366da3b19b5021e74f5bde2a8fa3f/pytest-9.0.2-py3-none-any.whl", hash = "sha256:711ffd45bf766d5264d487b917733b453d917afd2b0ad65223959f59089f875b", size = 374801, upload-time = "2025-12-06T21:30:49.154Z" },
+]
+
+[[package]]
+name = "python-ulid"
+version = "3.1.0"
+source = { registry = "https://pypi.org/simple" }
+sdist = { url = "https://files.pythonhosted.org/packages/40/7e/0d6c82b5ccc71e7c833aed43d9e8468e1f2ff0be1b3f657a6fcafbb8433d/python_ulid-3.1.0.tar.gz", hash = "sha256:ff0410a598bc5f6b01b602851a3296ede6f91389f913a5d5f8c496003836f636", size = 93175, upload-time = "2025-08-18T16:09:26.305Z" }
+wheels = [
+    { url = "https://files.pythonhosted.org/packages/6c/a0/4ed6632b70a52de845df056654162acdebaf97c20e3212c559ac43e7216e/python_ulid-3.1.0-py3-none-any.whl", hash = "sha256:e2cdc979c8c877029b4b7a38a6fba3bc4578e4f109a308419ff4d3ccf0a46619", size = 11577, upload-time = "2025-08-18T16:09:25.047Z" },
+]
+
+[[package]]
+name = "ruff"
+version = "0.15.1"
+source = { registry = "https://pypi.org/simple" }
+sdist = { url = "https://files.pythonhosted.org/packages/04/dc/4e6ac71b511b141cf626357a3946679abeba4cf67bc7cc5a17920f31e10d/ruff-0.15.1.tar.gz", hash = "sha256:c590fe13fb57c97141ae975c03a1aedb3d3156030cabd740d6ff0b0d601e203f", size = 4540855, upload-time = "2026-02-12T23:09:09.998Z" }
+wheels = [
+    { url = "https://files.pythonhosted.org/packages/23/bf/e6e4324238c17f9d9120a9d60aa99a7daaa21204c07fcd84e2ef03bb5fd1/ruff-0.15.1-py3-none-linux_armv6l.whl", hash = "sha256:b101ed7cf4615bda6ffe65bdb59f964e9f4a0d3f85cbf0e54f0ab76d7b90228a", size = 10367819, upload-time = "2026-02-12T23:09:03.598Z" },
+    { url = "https://files.pythonhosted.org/packages/b3/ea/c8f89d32e7912269d38c58f3649e453ac32c528f93bb7f4219258be2e7ed/ruff-0.15.1-py3-none-macosx_10_12_x86_64.whl", hash = "sha256:939c995e9277e63ea632cc8d3fae17aa758526f49a9a850d2e7e758bfef46602", size = 10798618, upload-time = "2026-02-12T23:09:22.928Z" },
+    { url = "https://files.pythonhosted.org/packages/5e/0f/1d0d88bc862624247d82c20c10d4c0f6bb2f346559d8af281674cf327f15/ruff-0.15.1-py3-none-macosx_11_0_arm64.whl", hash = "sha256:1d83466455fdefe60b8d9c8df81d3c1bbb2115cede53549d3b522ce2bc703899", size = 10148518, upload-time = "2026-02-12T23:08:58.339Z" },
+    { url = "https://files.pythonhosted.org/packages/f5/c8/291c49cefaa4a9248e986256df2ade7add79388fe179e0691be06fae6f37/ruff-0.15.1-py3-none-manylinux_2_17_aarch64.manylinux2014_aarch64.whl", hash = "sha256:a9457e3c3291024866222b96108ab2d8265b477e5b1534c7ddb1810904858d16", size = 10518811, upload-time = "2026-02-12T23:09:31.865Z" },
+    { url = "https://files.pythonhosted.org/packages/c3/1a/f5707440e5ae43ffa5365cac8bbb91e9665f4a883f560893829cf16a606b/ruff-0.15.1-py3-none-manylinux_2_17_armv7l.manylinux2014_armv7l.whl", hash = "sha256:92c92b003e9d4f7fbd33b1867bb15a1b785b1735069108dfc23821ba045b29bc", size = 10196169, upload-time = "2026-02-12T23:09:17.306Z" },
+    { url = "https://files.pythonhosted.org/packages/2a/ff/26ddc8c4da04c8fd3ee65a89c9fb99eaa5c30394269d424461467be2271f/ruff-0.15.1-py3-none-manylinux_2_17_i686.manylinux2014_i686.whl", hash = "sha256:1fe5c41ab43e3a06778844c586251eb5a510f67125427625f9eb2b9526535779", size = 10990491, upload-time = "2026-02-12T23:09:25.503Z" },
+    { url = "https://files.pythonhosted.org/packages/fc/00/50920cb385b89413f7cdb4bb9bc8fc59c1b0f30028d8bccc294189a54955/ruff-0.15.1-py3-none-manylinux_2_17_ppc64le.manylinux2014_ppc64le.whl", hash = "sha256:66a6dd6df4d80dc382c6484f8ce1bcceb55c32e9f27a8b94c32f6c7331bf14fb", size = 11843280, upload-time = "2026-02-12T23:09:19.88Z" },
+    { url = "https://files.pythonhosted.org/packages/5d/6d/2f5cad8380caf5632a15460c323ae326f1e1a2b5b90a6ee7519017a017ca/ruff-0.15.1-py3-none-manylinux_2_17_s390x.manylinux2014_s390x.whl", hash = "sha256:6a4a42cbb8af0bda9bcd7606b064d7c0bc311a88d141d02f78920be6acb5aa83", size = 11274336, upload-time = "2026-02-12T23:09:14.907Z" },
+    { url = "https://files.pythonhosted.org/packages/a3/1d/5f56cae1d6c40b8a318513599b35ea4b075d7dc1cd1d04449578c29d1d75/ruff-0.15.1-py3-none-manylinux_2_17_x86_64.manylinux2014_x86_64.whl", hash = "sha256:4ab064052c31dddada35079901592dfba2e05f5b1e43af3954aafcbc1096a5b2", size = 11137288, upload-time = "2026-02-12T23:09:07.475Z" },
+    { url = "https://files.pythonhosted.org/packages/cd/20/6f8d7d8f768c93b0382b33b9306b3b999918816da46537d5a61635514635/ruff-0.15.1-py3-none-manylinux_2_31_riscv64.whl", hash = "sha256:5631c940fe9fe91f817a4c2ea4e81f47bee3ca4aa646134a24374f3c19ad9454", size = 11070681, upload-time = "2026-02-12T23:08:55.43Z" },
+    { url = "https://files.pythonhosted.org/packages/9a/67/d640ac76069f64cdea59dba02af2e00b1fa30e2103c7f8d049c0cff4cafd/ruff-0.15.1-py3-none-musllinux_1_2_aarch64.whl", hash = "sha256:68138a4ba184b4691ccdc39f7795c66b3c68160c586519e7e8444cf5a53e1b4c", size = 10486401, upload-time = "2026-02-12T23:09:27.927Z" },
+    { url = "https://files.pythonhosted.org/packages/65/3d/e1429f64a3ff89297497916b88c32a5cc88eeca7e9c787072d0e7f1d3e1e/ruff-0.15.1-py3-none-musllinux_1_2_armv7l.whl", hash = "sha256:518f9af03bfc33c03bdb4cb63fabc935341bb7f54af500f92ac309ecfbba6330", size = 10197452, upload-time = "2026-02-12T23:09:12.147Z" },
+    { url = "https://files.pythonhosted.org/packages/78/83/e2c3bade17dad63bf1e1c2ffaf11490603b760be149e1419b07049b36ef2/ruff-0.15.1-py3-none-musllinux_1_2_i686.whl", hash = "sha256:da79f4d6a826caaea95de0237a67e33b81e6ec2e25fc7e1993a4015dffca7c61", size = 10693900, upload-time = "2026-02-12T23:09:34.418Z" },
+    { url = "https://files.pythonhosted.org/packages/a1/27/fdc0e11a813e6338e0706e8b39bb7a1d61ea5b36873b351acee7e524a72a/ruff-0.15.1-py3-none-musllinux_1_2_x86_64.whl", hash = "sha256:3dd86dccb83cd7d4dcfac303ffc277e6048600dfc22e38158afa208e8bf94a1f", size = 11227302, upload-time = "2026-02-12T23:09:36.536Z" },
+    { url = "https://files.pythonhosted.org/packages/f6/58/ac864a75067dcbd3b95be5ab4eb2b601d7fbc3d3d736a27e391a4f92a5c1/ruff-0.15.1-py3-none-win32.whl", hash = "sha256:660975d9cb49b5d5278b12b03bb9951d554543a90b74ed5d366b20e2c57c2098", size = 10462555, upload-time = "2026-02-12T23:09:29.899Z" },
+    { url = "https://files.pythonhosted.org/packages/e0/5e/d4ccc8a27ecdb78116feac4935dfc39d1304536f4296168f91ed3ec00cd2/ruff-0.15.1-py3-none-win_amd64.whl", hash = "sha256:c820fef9dd5d4172a6570e5721704a96c6679b80cf7be41659ed439653f62336", size = 11599956, upload-time = "2026-02-12T23:09:01.157Z" },
+    { url = "https://files.pythonhosted.org/packages/2a/07/5bda6a85b220c64c65686bc85bd0bbb23b29c62b3a9f9433fa55f17cda93/ruff-0.15.1-py3-none-win_arm64.whl", hash = "sha256:5ff7d5f0f88567850f45081fac8f4ec212be8d0b963e385c3f7d0d2eb4899416", size = 10874604, upload-time = "2026-02-12T23:09:05.515Z" },
+]
