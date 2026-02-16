# Code Review: LAT-30 — GitHub Actions CI

**Verdict: Changes Requested**

## Findings

1. **[High] Required `type-check` job is missing**  
   File: `.github/workflows/ci.yml:33`  
   LAT-30 expects two jobs: `test` and `type-check` (Python 3.12, `mypy src/lattice/`). The workflow defines `test` and `publish`, and there is no mypy step in CI.

2. **[High] Trigger scope does not match LAT-30 requirements**  
   File: `.github/workflows/ci.yml:6`  
   LAT-30 expects triggers for push to `main` and PRs to `main`. Current workflow additionally triggers on tags (`"v*"`), which expands behavior beyond scope.

3. **[Medium] Test command differs from required fail-fast/quiet mode**  
   File: `.github/workflows/ci.yml:31`  
   Expected command is `pytest -x -q`; current command is `pytest -v`.

4. **[Low] Install flow differs from requested `--system` usage**  
   Files: `.github/workflows/ci.yml:22`, `.github/workflows/ci.yml:25`  
   LAT-30 specifies `uv pip install -e ".[dev]" --system`. Current workflow creates a venv and installs without `--system`.

## Checks Against Requested Criteria

- YAML validity/structure: **Valid and well-structured**.
- Action versions: **Match expected** (`actions/checkout@v4`, `astral-sh/setup-uv@v4`).
- Python test matrix: **Correct** (`3.12`, `3.13`).
- Lint/format/test order: **Correct order** (`ruff check` -> `ruff format --check` -> `pytest`).
- Mypy configuration in project: **Present** in `pyproject.toml:63` (`[tool.mypy]` configured, strict enabled).

## Best-Practice Gaps (Non-blocking but recommended)

- Add `timeout-minutes` per job to avoid hung runners.
- Add dependency/tool caching (e.g., via `setup-uv` cache options) to reduce CI time.
- Add `concurrency` cancel-in-progress for PR workflows to avoid redundant runs.
- Consider separating publish logic into a dedicated release workflow if release automation is intentional.
