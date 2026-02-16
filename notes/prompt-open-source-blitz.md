# Open Source Phase 1 — Parallel Blitz

You are working on the Lattice project at `/Users/atin/Projects/Stage 11 Agentics/PROJECTS/Lattice`. Lattice is a file-based, agent-native task tracker with an event-sourced core, built by Stage 11 Agentics.

## Your Mission

Execute four tasks in parallel — spawn 4 agents simultaneously. These are all subtasks of epic LAT-26 (Open Source Launch & Distribution). After completing all four, update each task's status to `done` via `uv run lattice status <task> done --actor agent:claude-opus-4`.

Auto-commit each piece of work as it completes. Push when everything is done.

---

## Task 1: LAT-27 — Add MIT LICENSE file

Create a `LICENSE` file at the repo root with the standard MIT License text.

- Copyright holder: **Stage 11 Agentics Corporation**
- Year: **2026**
- That's it. Standard MIT, no modifications.

---

## Task 2: LAT-28 — Add project metadata to pyproject.toml

The current `pyproject.toml` has basic fields but is missing open-source metadata. Add these fields under `[project]`:

```toml
license = "MIT"
readme = "README.md"
authors = [
    { name = "Stage 11 Agentics", email = "hello@stage11agentics.com" },
]
keywords = ["task-tracker", "agent", "ai", "event-sourcing", "cli", "mcp"]
classifiers = [
    "Development Status :: 3 - Alpha",
    "Environment :: Console",
    "Intended Audience :: Developers",
    "License :: OSI Approved :: MIT License",
    "Programming Language :: Python :: 3",
    "Programming Language :: Python :: 3.12",
    "Topic :: Software Development :: Libraries",
    "Topic :: Software Development :: Build Tools",
]
```

Also add:
```toml
[project.urls]
Homepage = "https://github.com/stage11-agentics/lattice"
Repository = "https://github.com/stage11-agentics/lattice"
Issues = "https://github.com/stage11-agentics/lattice/issues"
```

Do NOT change any existing fields (name, version, description, dependencies, scripts, etc.). Only add what's missing.

After editing, verify the project still installs cleanly: `uv pip install -e ".[dev]"`.

---

## Task 3: LAT-29 — Write README.md

Write a README.md at the repo root. This is the first thing people see on GitHub. It needs to be excellent but honest — Lattice is v0.1.0, alpha quality, actively developed.

**Structure:**
1. **Title + one-line description** — `# Lattice` + "File-based, agent-native task tracker with an event-sourced core."
2. **Why Lattice exists** — 2-3 sentences. AI agents forget. Each session starts fresh. Lattice gives agents shared persistent state through files they can already read. No database, no server, no setup beyond `lattice init`.
3. **Quick start** — `pip install lattice-tracker` (or `uv pip install lattice-tracker`), `lattice init`, `lattice create "My first task" --actor human:me`, `lattice list`, `lattice status LAT-1 in_progress --actor human:me`.
4. **Key features** — bullet list: event-sourced (append-only event log, rebuildable snapshots), file-based (`.lattice/` directory, git-friendly), agent-native (any tool that reads files can participate), CLI-first, MCP server included, local dashboard, short IDs (LAT-42 style), relationships/dependencies, provenance tracking, plugin system.
5. **Dashboard screenshot placeholder** — `![Dashboard](docs/images/dashboard.png)` with a note "(screenshot coming soon)" — don't worry about the actual image.
6. **Architecture in brief** — event log is source of truth, task JSON files are materialized snapshots, `lattice rebuild` replays events. Link to CLAUDE.md or a future architecture doc for details.
7. **MCP Server** — brief mention that `lattice-mcp` ships as a separate entry point, install with `pip install lattice-tracker[mcp]`.
8. **Development** — clone, `uv venv && uv pip install -e ".[dev]"`, `uv run pytest`, `uv run ruff check src/ tests/`.
9. **License** — MIT. Link to LICENSE file.
10. **Built by** — "Built by [Stage 11 Agentics](https://stage11agentics.com) — autonomous agent teams."

**Tone:** Clear, technical, no marketing fluff. Written for developers and AI practitioners who might integrate Lattice into their agent workflows.

---

## Task 4: LAT-30 — Set up GitHub Actions CI

Create `.github/workflows/ci.yml`:

```yaml
name: CI

on:
  push:
    branches: [main]
  pull_request:
    branches: [main]

jobs:
  test:
    runs-on: ubuntu-latest
    strategy:
      matrix:
        python-version: ["3.12", "3.13"]
    steps:
      - uses: actions/checkout@v4
      - name: Install uv
        uses: astral-sh/setup-uv@v4
      - name: Set up Python ${{ matrix.python-version }}
        run: uv python install ${{ matrix.python-version }}
      - name: Install dependencies
        run: uv pip install -e ".[dev]" --system
      - name: Lint
        run: uv run ruff check src/ tests/
      - name: Format check
        run: uv run ruff format --check src/ tests/
      - name: Test
        run: uv run pytest -x -q

  type-check:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - name: Install uv
        uses: astral-sh/setup-uv@v4
      - name: Set up Python
        run: uv python install 3.12
      - name: Install dependencies
        run: uv pip install -e ".[dev]" --system
      - name: Type check
        run: uv run mypy src/lattice/
```

Make sure the `.github/workflows/` directory is created. Verify the YAML is valid.

---

## After All Four Complete

1. Update Lattice statuses:
   ```
   uv run lattice status LAT-27 done --actor agent:claude-opus-4
   uv run lattice status LAT-28 done --actor agent:claude-opus-4
   uv run lattice status LAT-29 done --actor agent:claude-opus-4
   uv run lattice status LAT-30 done --actor agent:claude-opus-4
   ```
2. Commit all changes with a clear message.
3. Push to remote.
