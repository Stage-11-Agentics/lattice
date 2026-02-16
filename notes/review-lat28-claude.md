# Code Review: LAT-28 — Open-Source Metadata in pyproject.toml

**Reviewer:** agent:claude-opus-4
**Commit:** `fd0895f chore: add open-source metadata to pyproject.toml (LAT-28)`
**Verdict:** LGTM (with minor suggestions)

---

## Summary

The commit is a clean, additive change that inserts open-source metadata fields into `pyproject.toml` without disturbing any existing configuration. The diff is strictly additions — no lines removed, no lines modified. This is exactly what was requested.

---

## Checklist

### 1. Existing fields left untouched?

**PASS.** Verified by diff inspection. The following fields are byte-identical before and after the commit:

- `[build-system]` (requires, build-backend)
- `name`, `version`, `description`, `requires-python`
- `dependencies` (click, python-ulid, filelock)
- `[project.optional-dependencies]` (mcp, dev)
- `[project.scripts]` (lattice, lattice-mcp)
- `[tool.hatch.build.targets.wheel]`
- `[tool.pytest.ini_options]`
- `[tool.mypy]`
- `[tool.ruff]`

No existing fields were modified, reordered, or reformatted.

### 2. All specified metadata fields present and correct?

**PASS.** All requested fields are present:

| Field | Value | Status |
|-------|-------|--------|
| `license` | `"MIT"` | Present |
| `readme` | `"README.md"` | Present; `README.md` exists on disk (4227 bytes) |
| `authors` | `[{ name = "Stage 11 Agentics", email = "hello@stage11agentics.com" }]` | Present; matches company entity info |
| `keywords` | 6 keywords including task-tracker, agent, ai, event-sourcing, cli, mcp | Present |
| `classifiers` | 8 trove classifiers | Present |
| `[project.urls]` | Homepage, Repository, Issues | Present |

### 3. TOML valid and well-formatted?

**PASS.** Validated via `tomllib.load()` — parses without error. Formatting is consistent with the existing file style (4-space indentation in arrays, standard TOML conventions). New fields are inserted in the correct location within `[project]` (after `requires-python`, before `dependencies`), and `[project.urls]` is placed after `[project.scripts]`, before `[tool.*]` sections. This is idiomatic ordering per PEP 621 convention.

### 4. Classifiers accurate?

**PASS.** All 8 classifiers are valid PyPI trove classifiers:

- `Development Status :: 3 - Alpha` — correct for v0.1.0
- `Environment :: Console` — correct, it is a CLI tool
- `Intended Audience :: Developers` — correct
- `License :: OSI Approved :: MIT License` — matches the `license` field and `LICENSE` file
- `Programming Language :: Python :: 3` — correct
- `Programming Language :: Python :: 3.12` — matches `requires-python = ">=3.12"`
- `Topic :: Software Development :: Libraries` — correct (it is a library/framework)
- `Topic :: Software Development :: Build Tools` — debatable (see note below)

### 5. Redundancies or missing fields for PyPI?

**No redundancies found.** A few observations on potential additions (non-blocking):

- **`Homepage` and `Repository` URLs are identical.** This is fine — many projects do this when the GitHub repo is the homepage. PyPI renders them as separate links in the sidebar. If there is a separate docs site or landing page planned, `Homepage` could point there instead, but for now this is correct.

- **Missing `Changelog` URL.** Not required, but commonly included in `[project.urls]` for PyPI. Could be added later when a CHANGELOG exists: `Changelog = "https://github.com/stage11-agentics/lattice/blob/main/CHANGELOG.md"`.

- **Missing `Documentation` URL.** Same — not required but standard for mature projects.

- **No `Typing :: Typed` classifier.** The project has `[tool.mypy]` with `strict = true`, but no `py.typed` marker file exists yet. If/when a `py.typed` marker is added to `src/lattice/`, the `Typing :: Typed` classifier should be added. This is a separate task, not a gap in LAT-28.

### 6. License field format: PEP 639 string vs. legacy table?

**PASS — correct choice.** The commit uses the modern PEP 639 string format:

```toml
license = "MIT"
```

This is the right call for this project. The alternative legacy PEP 621 table format (`license = {text = "MIT"}` or `license = {file = "LICENSE"}`) is deprecated in favor of the PEP 639 SPDX string format. Hatchling (the build backend) has supported PEP 639 since version 1.21. Since `build-system.requires` specifies `"hatchling"` without a version pin, it will resolve to a recent version that supports this format. If a minimum version were ever pinned below 1.21, this would need revisiting, but as-is this is correct and forward-looking.

---

## Minor Suggestions (Non-Blocking)

1. **Classifier accuracy: `Build Tools`.**
   `Topic :: Software Development :: Build Tools` is slightly off for a task tracker. More precise alternatives:
   - `Topic :: Software Development :: Bug Tracking` (closest match for issue/task tracking)
   - `Topic :: Office/Business :: Scheduling` (for project management aspect)

   This is cosmetic and non-blocking. The current classifier is not *wrong* — Lattice is a developer tool — but it could be more precise.

2. **Consider adding `Programming Language :: Python :: 3.13`** if/when Python 3.13 compatibility is validated, since `requires-python = ">=3.12"` implies it.

3. **Consider pinning hatchling minimum version** in `build-system.requires` to `"hatchling>=1.21"` to formally guarantee PEP 639 support. Without this, a hypothetical environment with an old cached hatchling could fail on the string-format `license` field. Low probability, but a one-line hardening.

---

## Verdict

**LGTM.** The change is exactly scoped to the task: additive metadata, no regressions, valid TOML, correct license format, accurate classifiers. The minor suggestions above are improvements for a follow-up, not blockers.
