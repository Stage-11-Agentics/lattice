# CI Workflow Review — LAT-30

**File:** `.github/workflows/ci.yml`
**Reviewer:** agent:claude-opus-4-6
**Date:** 2026-02-16
**Verdict:** Changes Requested

---

## Summary

The workflow structure is sound — two jobs (`test` and `type-check`), correct triggers, sensible matrix. But the pipeline **will not pass against the current codebase** due to two independently blocking issues: 9 files fail `ruff format --check`, and mypy reports 221 errors across 24 files. There are also several CI best-practice gaps worth addressing.

---

## Findings

### BLOCKING: Pipeline will fail on current main

#### B1. `ruff format --check` fails (9 files)

The format check step will exit non-zero. These files need reformatting:

- `src/lattice/cli/main.py`
- `src/lattice/cli/stats_cmds.py`
- `src/lattice/cli/task_cmds.py`
- `src/lattice/cli/weather_cmds.py`
- `src/lattice/core/stats.py`
- `src/lattice/dashboard/server.py`
- `tests/test_cli/test_stats_cmds.py`
- `tests/test_cli/test_weather_cmds.py`
- `tests/test_core/test_plugins.py`

**Fix:** Run `uv run ruff format src/ tests/` and commit before merging the CI workflow, or the very first run on main will be red.

#### B2. `mypy` fails (221 errors in 24 files)

The `type-check` job will fail immediately. The bulk of errors are in `src/lattice/mcp/tools.py` and `src/lattice/mcp/resources.py` (bare `dict` without type parameters under `strict = true`), plus issues in `artifact_cmds.py`, `archive_cmds.py`, and `integrity_cmds.py`.

**Fix options (pick one):**
1. Fix all 221 mypy errors before enabling the job.
2. Add the type-check job with `continue-on-error: true` and a comment marking it advisory until the codebase is clean.
3. Scope mypy to only the clean modules for now (exclude `mcp/` and problem CLI files) and expand coverage incrementally.

Option 3 is the most pragmatic — you get real type-checking protection on the core immediately without blocking on the MCP layer.

### NON-BLOCKING: CI best practices

#### N1. `--system` flag is wrong for `uv run`

Lines 22 and 39 install with `uv pip install -e ".[dev]" --system`, then all subsequent steps use `uv run`. This is incoherent: `--system` installs into the system Python's site-packages, but `uv run` creates/uses an ephemeral virtual environment and won't see system-installed packages. On GitHub Actions runners this may accidentally work because uv falls back to discovering the project, but it is fragile and conceptually wrong.

**Fix:** Drop `--system` and let uv manage the environment:
```yaml
- name: Install dependencies
  run: uv sync --extra dev
```

`uv sync` reads `uv.lock` (which exists in the repo), creates a `.venv`, and installs deterministically. This is the canonical uv-in-CI pattern.

#### N2. No job timeout

Neither job specifies `timeout-minutes`. The default is 360 minutes (6 hours). A hung test or infinite loop will burn through Actions minutes.

**Fix:**
```yaml
test:
  runs-on: ubuntu-latest
  timeout-minutes: 10
```

10 minutes is generous for a 70-second test suite. Type-check can be 5 minutes.

#### N3. No concurrency group

Pushing twice in quick succession to main (or updating a PR) runs duplicate workflows. The first run is wasted.

**Fix:**
```yaml
concurrency:
  group: ci-${{ github.ref }}
  cancel-in-progress: true
```

#### N4. No permissions block

The workflow gets default `contents: read` + `write` permissions. Explicitly restricting to `contents: read` is a security best practice — especially once the repo is public.

**Fix:**
```yaml
permissions:
  contents: read
```

#### N5. Lint and format run redundantly in the matrix

Ruff lint and format checks are Python-version-independent (they parse but don't execute code). Running them in both 3.12 and 3.13 doubles the work for no benefit.

**Fix:** Either:
- Extract lint/format into a separate job that runs once (cleaner).
- Or add `if: matrix.python-version == '3.12'` to the lint and format steps (simpler).

#### N6. No `fail-fast: false` on the matrix

The default `fail-fast: true` means if 3.12 fails, the 3.13 run is cancelled. For CI where you want to know the full picture (does it fail on both? just one?), `fail-fast: false` is usually better.

```yaml
strategy:
  fail-fast: false
  matrix:
    python-version: ["3.12", "3.13"]
```

#### N7. Action versions are current (no issue)

`actions/checkout@v4` and `astral-sh/setup-uv@v4` are the current major versions. No issue here.

#### N8. Python matrix is correct (no issue)

`requires-python = ">=3.12"` in pyproject.toml, matrix tests 3.12 and 3.13. Correct and sufficient.

---

## Suggested Rewrite

Incorporating all findings above:

```yaml
name: CI

on:
  push:
    branches: [main]
  pull_request:
    branches: [main]

permissions:
  contents: read

concurrency:
  group: ci-${{ github.ref }}
  cancel-in-progress: true

jobs:
  lint:
    runs-on: ubuntu-latest
    timeout-minutes: 5
    steps:
      - uses: actions/checkout@v4
      - uses: astral-sh/setup-uv@v4
      - name: Set up Python
        run: uv python install 3.12
      - name: Install dependencies
        run: uv sync --extra dev
      - name: Lint
        run: uv run ruff check src/ tests/
      - name: Format check
        run: uv run ruff format --check src/ tests/

  test:
    runs-on: ubuntu-latest
    timeout-minutes: 10
    strategy:
      fail-fast: false
      matrix:
        python-version: ["3.12", "3.13"]
    steps:
      - uses: actions/checkout@v4
      - uses: astral-sh/setup-uv@v4
      - name: Set up Python ${{ matrix.python-version }}
        run: uv python install ${{ matrix.python-version }}
      - name: Install dependencies
        run: uv sync --extra dev
      - name: Test
        run: uv run pytest -x -q

  type-check:
    runs-on: ubuntu-latest
    timeout-minutes: 5
    steps:
      - uses: actions/checkout@v4
      - uses: astral-sh/setup-uv@v4
      - name: Set up Python
        run: uv python install 3.12
      - name: Install dependencies
        run: uv sync --extra dev
      - name: Type check
        run: uv run mypy src/lattice/
```

---

## Pre-merge Checklist

- [ ] Run `uv run ruff format src/ tests/` and commit (fixes B1)
- [ ] Resolve mypy errors or scope the type-check job (fixes B2)
- [ ] Switch from `uv pip install --system` to `uv sync --extra dev` (fixes N1)
- [ ] Add `timeout-minutes` to both jobs (fixes N2)
- [ ] Add concurrency group (fixes N3)
- [ ] Add permissions block (fixes N4)
- [ ] Extract lint into its own job or gate it to one matrix entry (fixes N5)
- [ ] Consider `fail-fast: false` (fixes N6)
