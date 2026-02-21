# Lattice

Stage 11 Agentics' file-based, agent-native task tracker with an event-sourced core.

## Lattice Coordination — READ THIS FIRST

This project tracks all work through Lattice. **Before you do anything else, read `/lattice`** — it is the complete guide to Lattice workflows, commands, lifecycle, architecture, and discipline.

**The non-negotiable rule:** Every unit of work gets a Lattice task before the work begins. Not after. Not during. Before. A feature, a bug fix, a refactor — if it will produce commits, it starts with:

```
lattice create "<title>" --actor agent:<your-id>
```

The `/lattice` skill covers everything: CLI commands, lifecycle discipline (claim, work, complete), status transitions, planning gates, review gates, sub-agent execution model, rework loops, actor attribution, branch linking, on-disk layout, identifiers, and coordination patterns.


### Project-Specific Conventions

- **Recurring observations become tasks.** Same issue in 2+ sessions? Create a task (`needs_human` if it needs scoping, `backlog` if well-understood).
- **Where learnings go:** Do not save to auto-memory. Add to this `CLAUDE.md` or propose updating `src/lattice/templates/claude_md_block.py` so every future installation benefits.
- **Actor format:** `agent:<model-name>` or `human:<name>`
- **Auto-commit and auto-push** on feature branches.

---

## Disambiguation: "Lattice" the Codebase vs. "Lattice" the Instance

This project **dogfoods itself**. Two distinct things called "Lattice":

1. **The Lattice source code** — the Python project under `src/lattice/`. This is what `git` tracks.
2. **The `.lattice/` data directory** — a live Lattice instance for tracking dev tasks. Gitignored in this repo (heavy test/dev churn would pollute diffs).

**Rule:** Never confuse changes to `src/lattice/` (source code) with changes to `.lattice/` (instance data). They are independent. Editing source code does not affect the running instance until you reinstall (`uv pip install -e ".[dev]"`).

## Global Tool vs. Dev Install — Known Failure Mode

Lattice is installed two ways on this machine. Mixing them up causes silent bugs where your code changes have no effect.

| Install | Binary | Python path | When to use |
|---------|--------|-------------|-------------|
| **Global tool** | `lattice` (bare) | `~/.local/share/uv/tools/lattice-tracker/` | End-user usage, running the dashboard day-to-day |
| **Dev install** | `uv run lattice` | Project venv, reads `src/` directly | Development, testing code changes |

**The failure mode:** You edit source files in `src/lattice/`, then run `lattice dashboard` (bare). The global tool serves its own installed copy of the code, not your source tree. Your changes are invisible. Everything looks wrong and you waste time debugging CSS/JS/Python that is actually correct.

**Critical: `uv tool install` uses a build cache.** Running `uv tool install . --force` does NOT guarantee a fresh build. uv caches the built wheel and will reuse it if the package version hasn't changed. To force a true rebuild:

```bash
# WRONG: may serve stale cached wheel
uv tool install . --force

# RIGHT: clean cache first, then install
uv cache clean lattice-tracker && uv tool install . --force
```

Look for `Building lattice-tracker` in the output. If you only see `Prepared 1 package` without `Building`, you got the cached wheel.

**How to avoid it:**
- **During development**, always use `uv run lattice` (not bare `lattice`)
- **After finishing work**, use the publish script: `./scripts/publish-global.sh`
- **When the dashboard looks stale**, check which binary is running: `ps aux | grep lattice` -- if it shows `~/.local/share/uv/tools/`, that's the global install

**Quick reference:**
```bash
# Dev: run from source (changes take effect immediately)
uv run lattice dashboard

# Global: update after committing changes (always use the script)
./scripts/publish-global.sh
lattice dashboard
```

## Quick Reference

| Item | Value |
|------|-------|
| Language | Python 3.12+ |
| CLI framework | Click |
| Testing | pytest |
| Linting | ruff |
| Package manager | uv |
| Entry point | `lattice` (via `[project.scripts]`) |

## Key Documents

| Document | Purpose |
|----------|---------|
| `ProjectRequirements_v1.md` | Full specification — object model, schemas, CLI commands, invariants |
| `Decisions.md` | Architectural decisions with rationale (append-only log) |
| `docs/architecture/README.md` | Index for all architecture deep dives |

**Read `ProjectRequirements_v1.md` before making any architectural change.**

## Layer Boundaries

- **`core/`** — pure business logic. No filesystem calls.
- **`storage/`** — all filesystem I/O. Atomic writes, locking, directory traversal.
- **`cli/`** — wires core + storage via Click commands. Output formatting.
- **`dashboard/`** — read-only. Reads `.lattice/` files, serves JSON + static HTML.

## Development Setup

```bash
cd lattice
uv venv
uv pip install -e ".[dev]"
uv run pytest
uv run ruff check src/ tests/
uv run ruff format src/ tests/
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

Minimize dependencies. The dashboard uses only stdlib. Do not add dependencies without justification.

## Coding Conventions

### JSON Output

```python
# Snapshots: sorted keys, 2-space indent, trailing newline
json.dumps(data, sort_keys=True, indent=2) + "\n"

# Events (JSONL): compact separators, one line
json.dumps(event, sort_keys=True, separators=(",", ":")) + "\n"
```

### Error Handling

- Human-readable errors to stderr, non-zero exit codes.
- `--json` mode: `{"ok": true, "data": ...}` or `{"ok": false, "error": {"code": "...", "message": "..."}}`.
- Never silently swallow errors.

### Testing

- CLI commands → integration tests (invoke Click, check `.lattice/` state).
- Core modules → unit tests (pure logic, no filesystem).
- Storage → tests with real temp directories (`tmp_path` fixture).

## Where Things Live

- **Task plans** → `.lattice/plans/<task_id>.md`
- **Task notes** → `.lattice/notes/<task_id>.md`
- **Repo `notes/`** — code reviews, retrospectives, working documents NOT tied to a specific task.
- **Repo `docs/`** — user-facing documentation, guides, architecture deep dives.
- **Repo `prompts/`** — prompt templates and implementation checklists.
- **Repo `research/`** — external research, competitive analysis, reference material.
- **Don't duplicate** — a document should live in one place.

## Branching Model

**Two-branch model.**

- **`main`** — development branch. All feature branches merge here.
- **`prod`** — stable release branch. Merges from `main` when a release is ready.
- Feature work: short-lived branches off `main` (`feat/`, `fix/`, `refactor/`, `test/`, `chore/`).
- Conventional commit messages (`feat:`, `fix:`, etc.).
- Before merging to `main`: all tests pass, ruff clean.
- Before merging to `prod`: same gates + manual confirmation.
- Schema changes: bump `schema_version`, maintain forward compatibility.
- New decisions: append to `Decisions.md`.

## What Not to Build (v0)

Refer to `ProjectRequirements_v1.md` for full non-goals. Key reminders:
- No agent registry (actor IDs are free-form strings)
- No `lattice note` command (notes are direct file edits)
- No database or index (filesystem scanning is sufficient at v0 scale)
- No real-time dashboard updates
- No authentication or multi-user access control
- No CI/CD integration, alerting, or process management
