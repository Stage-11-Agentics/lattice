# Task: Scaffold Lattice project structure and implement `lattice init`

## Context

Lattice is a file-based, agent-native task tracker with an event-sourced core. You are starting from a repo that has project documents and no code yet.

**Key architectural facts relevant to this task:**
- The CLI is the only write interface for authoritative state (invariant 2.2)
- All writes use atomic operations: write temp file, fsync, rename (section 5.2)
- All JSON output is deterministic: sorted keys, 2-space indent, trailing newline (section 5.2)
- The CLI finds `.lattice/` by walking up from cwd; `LATTICE_ROOT` env var overrides (section 5.3)
- `_global.jsonl` is a **derived** convenience log, not a second source of truth (section 9.1)

## Before writing any code, read these files in order:

1. `CLAUDE.md` — Project guide: stack, architecture, project structure, layer boundaries, coding conventions
2. `ProjectRequirements_v1.md` — Full spec. For this task, focus on:
   - Section 2 (System Invariants — understand all six, especially 2.2 and 2.6)
   - Section 5 (File Layout, Format Rules, Root Discovery)
   - Section 7.1 (Workflow config — the default config shape)
   - Section 13.1 (`lattice init` command)
3. `Decisions.md` — Architectural decisions with rationale (skim for context)

## What to build

### 1. Project scaffold

Create the full Python package structure as defined in CLAUDE.md:

- `pyproject.toml` — Python 3.12+, src layout, Click entry point, runtime deps (click, python-ulid, filelock), dev deps (pytest, ruff). Package name: `lattice-tracker` (to avoid PyPI conflicts). CLI entry point: `lattice = "lattice.cli.main:cli"`
- `src/lattice/` — All subpackages with `__init__.py` files: `cli/`, `core/`, `storage/`, `dashboard/`
- `src/lattice/cli/main.py` — Click group entry point (just the root group, no subcommands beyond `init` yet)
- `src/lattice/core/config.py` — Default config generation and validation
- `src/lattice/storage/fs.py` — Atomic file writes, directory creation
- `src/lattice/core/ids.py` — Stub for ULID generation (can be minimal for now)
- `tests/conftest.py` — Shared fixtures: `tmp_path`-based `.lattice/` directory fixture
- `.gitignore` — Python standard ignores. Include `.lattice/` **only to prevent test artifacts from being committed to the Lattice source repo itself**. Note: users of Lattice (the product) are expected to commit their `.lattice/` directory — deterministic JSON formatting exists specifically for clean git diffs.

Leave other modules as empty files with just docstrings — they'll be implemented in subsequent tasks.

### 2. Root discovery utility

Implement the root discovery logic (section 5.3) as a utility in `storage/` or a shared location:

- Check `LATTICE_ROOT` env var first. If set, it points to the directory **containing** `.lattice/` (not `.lattice/` itself). If the env var is set but the path is invalid (doesn't exist, or exists but has no `.lattice/` inside), **fail immediately with a clear error** — do not fall back to walk-up. An explicit override that's wrong is a bug, not a hint.
- If no env var, walk up from cwd (or a given starting path) looking for a `.lattice/` directory, stopping at the filesystem root.
- Return the path to the directory containing `.lattice/`, or `None` if not found.
- `lattice init` does **not** use walk-up discovery (it creates `.lattice/` in a target directory). All other future commands will use it.

### 3. `lattice init` command

Implement the `lattice init` command that:

- Creates the `.lattice/` directory structure:
  ```
  .lattice/
  ├── config.json
  ├── tasks/
  ├── events/
  │   └── _global.jsonl  (empty file, ready for appends)
  ├── artifacts/
  │   ├── meta/
  │   └── payload/
  ├── notes/
  ├── archive/
  │   ├── tasks/
  │   ├── events/
  │   └── notes/
  └── locks/
  ```
- Writes the default `config.json` using **atomic write** (write to temp file in same directory, fsync, rename).

  **Important:** The JSON below is the exact byte-for-byte expected output of `json.dumps(config, sort_keys=True, indent=2) + "\n"`. Your `core/config.py` should produce a dict that, when serialized this way, matches exactly. Note that `sort_keys=True` puts `default_priority` before `schema_version`, and `indent=2` expands arrays to one item per line.

  ```json
  {
    "default_priority": "medium",
    "default_status": "backlog",
    "schema_version": 1,
    "task_types": [
      "task",
      "epic",
      "bug",
      "spike",
      "chore"
    ],
    "workflow": {
      "statuses": [
        "backlog",
        "ready",
        "in_progress",
        "review",
        "done",
        "blocked",
        "cancelled"
      ],
      "transitions": {
        "backlog": [
          "ready",
          "cancelled"
        ],
        "blocked": [
          "ready",
          "in_progress",
          "cancelled"
        ],
        "cancelled": [],
        "done": [],
        "in_progress": [
          "review",
          "blocked",
          "cancelled"
        ],
        "ready": [
          "in_progress",
          "blocked",
          "cancelled"
        ],
        "review": [
          "done",
          "in_progress",
          "cancelled"
        ]
      },
      "wip_limits": {
        "in_progress": 10,
        "review": 5
      }
    }
  }
  ```

- Is **idempotent**: running `lattice init` in a directory that already has `.lattice/` should succeed without overwriting existing config or data. Print a message like "Lattice already initialized in .lattice/" and exit 0.
- Prints confirmation on success: "Initialized empty Lattice in .lattice/"
- Supports `--path <dir>` to initialize in a specific directory (defaults to cwd)

**Why init does not require locking:** `lattice init` either creates a new `.lattice/` directory (no existing data to protect) or detects an existing one and exits without writing (idempotent no-op). There is no concurrent-write scenario. All other commands that mutate existing files will require locks — that machinery will be implemented in later tasks.

### 4. Tests

Write tests for:

**Init — directory structure:**
- `lattice init` creates all expected directories (every subdirectory in the tree above)
- `lattice init` creates an empty `_global.jsonl` file
- `lattice init --path <dir>` works with a custom path

**Init — config:**
- `lattice init` writes valid, parseable `config.json`
- Config has `schema_version: 1`
- Config JSON is byte-identical to `json.dumps(default_config(), sort_keys=True, indent=2) + "\n"` (this validates sorted keys, 2-space indent, trailing newline, and array expansion all at once)
- Config is written via atomic write pattern (verify by checking that the file exists and is valid JSON — the atomic write *implementation* is in `storage/fs.py` and its correctness is a unit test there: write to temp, fsync, rename)

**Init — idempotency:**
- Second `lattice init` run doesn't clobber existing config or data
- Modified config survives a second `lattice init` (edit config between runs, verify edit is preserved)

**Root discovery:**
- Walks up from a nested subdirectory to find `.lattice/`
- `LATTICE_ROOT` env var overrides walk-up
- `LATTICE_ROOT` set to invalid path raises an error (does not fall back to walk-up)
- Returns `None` when no `.lattice/` exists and no env var set

**Atomic write (unit test for `storage/fs.py`):**
- Writes to temp file and renames (verify no partial file at the target path during write)
- Target file contains expected content after write
- Target file's parent directory must exist (or error clearly)

Use Click's `CliRunner` for CLI integration tests. Use `tmp_path` and `monkeypatch` for filesystem and env var isolation.

## Conventions to follow

- All JSON: sorted keys, 2-space indent, trailing newline (`json.dumps(data, sort_keys=True, indent=2) + "\n"`)
- Atomic writes for config.json: write to temp file in same directory, fsync, then `os.rename`
- Layer boundaries: `core/` has no filesystem calls, `storage/` handles I/O, `cli/` wires them together
- Root discovery lives in `storage/` (it's filesystem I/O)
- Config generation (the default data structure) lives in `core/config.py` (it's pure data, no I/O)
- Keep it simple — this is the foundation, not the place for cleverness

## What NOT to do

- Don't implement any other CLI commands (create, update, status, etc.)
- Don't implement event logging yet
- Don't build the dashboard
- Don't implement file locking yet — init doesn't need it (see rationale above), and the locking module will be built in a later task when commands that mutate existing files are implemented
- Don't add any dependencies beyond what's listed in CLAUDE.md

## Validation

After implementation, run:
```bash
uv venv && uv pip install -e ".[dev]"
uv run pytest -v
uv run ruff check src/ tests/
uv run ruff format --check src/ tests/
uv run lattice --help
uv run lattice init
ls -la .lattice/
cat .lattice/config.json
uv run lattice init  # second run should be idempotent
```

All tests should pass. Ruff should be clean (both check and format). Both init runs should succeed. The `cat` output should be byte-identical to the expected JSON above (sorted keys, expanded arrays, trailing newline).

Clean up after validation:
```bash
rm -rf .lattice/  # remove the test .lattice/ from the repo root
```
