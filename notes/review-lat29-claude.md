# README.md Review — LAT-29

**Reviewer:** agent:claude-opus-4-6
**Verdict:** Changes Requested

---

## Summary

The README is strong. The tone is right, the architecture section is accurate, and a developer would understand what Lattice is within 60 seconds. There are a handful of factual issues that should be fixed before launch, ranging from a phantom feature claim to a broken image reference and a missing section from the expected outline. None are structural — this is close to ship.

---

## Section Audit (Expected 10)

| # | Expected Section | Present? | Notes |
|---|-----------------|----------|-------|
| 1 | Title | Yes | Line 1. Clean one-liner subtitle. |
| 2 | Why it exists | Yes | Lines 5-9. Good framing, no marketing fluff. |
| 3 | Quick start | Yes | Lines 11-31. Functional. |
| 4 | Key features | Yes | Lines 33-44. |
| 5 | Dashboard placeholder | Yes | Lines 46-50. |
| 6 | Architecture | Yes | Lines 52-70. Accurate. |
| 7 | MCP server | Yes | Lines 72-83. |
| 8 | Development | Yes | Lines 85-105. |
| 9 | License | Yes | Lines 111-113. |
| 10 | Built by | Yes | Lines 115-117. |

**Extra section:** `## Status` (lines 107-109) is present but was NOT in the expected 10-section outline. This is actually a good addition — alpha honesty deserves its own callout. Suggest keeping it, but the spec should be updated to reflect 11 sections, or it should be folded into another section (e.g., a parenthetical after the title subtitle).

---

## Findings

### HIGH — Factual error: "Plugin system" claim (line 44)

> `- **Plugin system** -- extend Lattice with custom event handlers and integrations.`

The phrase "custom event handlers" is misleading. What actually exists:

- `src/lattice/plugins.py` — entry-point-based plugin discovery for **CLI command plugins** (`lattice.cli_plugins`) and **CLAUDE.md template block plugins** (`lattice.template_blocks`). Neither of these is an "event handler."
- `src/lattice/storage/hooks.py` — shell hook execution (`hooks.post_event`, `hooks.on.<type>`) that fires after events are written. This is closer to "event handlers," but it is a hooks system configured in `config.json`, not a plugin system.

These are two separate mechanisms. The README conflates them into one bullet that accurately describes neither. A developer who reads this and goes looking for a plugin API to handle events will be confused.

**Suggested fix:** Split into two bullets or reword to accurately describe what ships:

```markdown
- **Hooks** -- fire shell commands on events via `config.json` (`post_event`, per-type triggers).
- **Plugin system** -- extend the CLI and `setup-claude` templates via `importlib.metadata` entry points.
```

Or if you want a single bullet, be precise:

```markdown
- **Extensible** -- shell hooks fire on events; entry-point plugins extend the CLI and templates.
```

### MEDIUM — Dashboard image reference is a dead link (line 48)

```markdown
![Dashboard](docs/images/dashboard.png)
```

The file `docs/images/dashboard.png` does not exist in the repo. The `*(screenshot coming soon)*` note on line 50 acknowledges this, but the broken image tag will render as a broken image icon on GitHub, which looks unpolished for a launch README.

**Suggested fix:** Remove the image tag entirely until the screenshot exists. Keep only the "(screenshot coming soon)" note, or remove the entire Dashboard section and add it back when there is something to show.

```markdown
## Dashboard

`lattice dashboard` launches a read-only local web UI for human visibility into task state.

*(Screenshot coming soon)*
```

### MEDIUM — Quick start assumes `lattice init` sets a project_code (line 30)

The quick start uses `LAT-1` on line 30:

```bash
lattice status LAT-1 in_progress --actor human:me
```

Short IDs like `LAT-1` require a `project_code` to be configured. The preceding `lattice init` on line 20 does not show any `--project-code` flag. If `lattice init` defaults to creating a project code, this works. If it does not, the user will get an error on step 4 of the quick start, which is a terrible first impression.

**Action needed:** Verify that `lattice init` either (a) prompts for or defaults to a project code, or (b) add `--project-code LAT` to the init command in the quick start. If short IDs are not guaranteed, use the ULID output from the `create` command instead.

### LOW — `_lifecycle.jsonl` missing from architecture tree (line 63)

The CLAUDE.md documents `.lattice/events/_lifecycle.jsonl` as part of the on-disk layout. The README's architecture tree omits it. This is fine for a "brief" overview, but noting it for completeness — if someone reads both documents they may wonder if it was removed.

### LOW — Philosophy link may confuse open-source readers (line 70)

```markdown
For the full architecture, see [CLAUDE.md](CLAUDE.md) or [Philosophy](Philosophy_v2.md).
```

Linking to `CLAUDE.md` as architecture documentation is fine — it genuinely contains the architecture details. But `Philosophy_v2.md` is an internal philosophical document written in lowercase, non-technical prose. An open-source contributor clicking that link expecting architecture docs will find something quite different. Consider linking only to `CLAUDE.md` for architecture, or labeling the Philosophy link more accurately (e.g., "design philosophy").

### LOW — Python version discrepancy with runtime (line 103)

The README states `Python 3.12+` and `pyproject.toml` specifies `requires-python = ">=3.12"`. Both are consistent. However, the `.venv` in the repo is running Python 3.13 (`/Users/atin/Projects/Fractal Agentics/PROJECTS/Lattice/.venv/lib/python3.13/`). This is not an error — 3.13 satisfies >=3.12 — but worth noting that CI should test on 3.12 to ensure the floor is actually supported.

### NITPICK — Tone and clarity

The README tone is good. Technical, direct, no buzzwords. Two micro-observations:

1. Line 7: "Your agents have intelligence without memory. Capability without coordination." — This is the only line that reads slightly like copy. It is still good; just noting it sits at the edge of the "no marketing fluff" bar.

2. Line 9: "and suddenly every agent that can read a file -- and they all can --" — The parenthetical is effective. Keeps the README grounded.

### NITPICK — Markdown consistency

Em dashes are rendered as `--` throughout, which is consistent internally but renders as literal `--` on GitHub (not `—`). This is a stylistic choice, not an error.

---

## Cross-reference checks

| Fact | README | pyproject.toml / Source | Match? |
|------|--------|------------------------|--------|
| Package name | `lattice-tracker` | `name = "lattice-tracker"` | Yes |
| Version | `v0.1.0` | `version = "0.1.0"` | Yes |
| License | MIT | `license = "MIT"`, `LICENSE` file exists | Yes |
| Python version | 3.12+ | `requires-python = ">=3.12"` | Yes |
| Runtime deps | click, python-ulid, filelock | `dependencies` in pyproject.toml | Yes |
| MCP entry point | `lattice-mcp` | `lattice-mcp = "lattice.mcp.server:main"` | Yes |
| MCP install extra | `lattice-tracker[mcp]` | `[project.optional-dependencies] mcp = [...]` | Yes |
| CLI entry point | `lattice` | `lattice = "lattice.cli.main:cli"` | Yes |
| GitHub URL | `stage11-agentics/lattice` | `project.urls` | Yes |
| Plugin system | Claimed | `src/lattice/plugins.py` exists (CLI + template plugins, NOT event handlers) | Partial (see HIGH finding) |
| Event-sourced architecture | Claimed | Confirmed via CLAUDE.md and source structure | Yes |
| Dashboard | Claimed | `src/lattice/dashboard/` exists with `server.py` and `static/index.html` | Yes |

---

## Verdict

**Changes Requested** — two items should be fixed before launch:

1. **Fix the plugin system bullet** (HIGH) — the current wording claims something that does not exist as described. Reword to match what actually ships.
2. **Remove the broken dashboard image tag** (MEDIUM) — broken images on a launch README are a bad first impression.

The quick start short-ID issue (MEDIUM) should also be verified but may already work if `lattice init` defaults a project code.

Everything else is solid. The README does its job: a developer landing on this page will understand what Lattice is, how to install it, and how to use it within 60 seconds.
