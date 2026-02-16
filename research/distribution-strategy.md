# Lattice Distribution Strategy: Zero-Friction Adoption

**Date:** 2026-02-15
**Purpose:** Technical plan for making Lattice trivially easy to install across the agent ecosystem

---

## Current State

Lattice already has the two hardest pieces built:

| Asset | Status | Entry Point |
|-------|--------|-------------|
| CLI | Built | `lattice` (Click-based) |
| MCP Server | Built | `lattice-mcp` (FastMCP, stdio transport) |
| Package config | Ready | `lattice-tracker` in pyproject.toml |
| GitHub repo | Private | BenevolentFutures/lattice |
| README | Missing | --- |
| LICENSE | Missing | --- |
| PyPI | Not published | --- |
| OpenClaw skill | Not built | --- |

**The MCP server is the killer asset.** It's already a complete tool suite with 15+ operations. This means Lattice can integrate with ANY MCP client (Claude Code, Cursor, Windsurf, OpenClaw, Cline, Continue, Zed, etc.) with zero custom code per client.

---

## The Adoption Funnel

Every potential user goes through this funnel. The goal is to minimize friction at each step.

```
DISCOVER  →  INSTALL  →  INIT  →  CONNECT  →  USE
```

Each persona hits the funnel differently:

| Persona | Discover | Install | Init | Connect | Use |
|---------|----------|---------|------|---------|-----|
| **Claude Code user** | README / word of mouth | `pip install lattice-tracker` | `lattice init` | Add MCP config to `claude_desktop_config.json` | MCP tools auto-available |
| **OpenClaw user** | ClawHub / community | `pip install lattice-tracker` | `lattice init` | Install skill or add MCP config | Skill/MCP tools available |
| **Cursor user** | MCP registry / README | `pip install lattice-tracker` | `lattice init` | Add MCP config to Cursor settings | MCP tools auto-available |
| **CLI power user** | GitHub / PyPI | `pipx install lattice-tracker` | `lattice init` | N/A (CLI direct) | `lattice create`, `lattice status`, etc. |
| **Python developer** | PyPI / GitHub | `pip install lattice-tracker` | `lattice init` | Import as library | `from lattice.core import ...` |

---

## Distribution Channels (Priority Order)

### 1. PyPI --- The Foundation

**Impact:** Every other channel depends on this.
**Effort:** Low (pyproject.toml is ready)

```bash
# User experience
pip install lattice-tracker    # or
pipx install lattice-tracker   # CLI-only install, or
uv tool install lattice-tracker  # modern Python tooling
```

**What's needed:**
- [ ] Publish to PyPI (`uv build && uv publish` or `hatch build && hatch publish`)
- [ ] Add `[project.urls]` to pyproject.toml (homepage, repo, docs)
- [ ] Add `[project.classifiers]` for PyPI discoverability
- [ ] Add `[project.readme]` pointing to README.md

**pyproject.toml additions:**
```toml
[project]
readme = "README.md"
license = {text = "MIT"}
classifiers = [
    "Development Status :: 3 - Alpha",
    "Environment :: Console",
    "Intended Audience :: Developers",
    "License :: OSI Approved :: MIT License",
    "Programming Language :: Python :: 3.12",
    "Topic :: Software Development :: Bug Tracking",
]

[project.urls]
Homepage = "https://github.com/Stage11Agentics/lattice"
Repository = "https://github.com/Stage11Agentics/lattice"
Documentation = "https://github.com/Stage11Agentics/lattice#readme"
```

### 2. GitHub Public Repo --- Visibility and Trust

**Impact:** Required for open-source credibility. Stars = social proof.
**Effort:** Low-medium

**What's needed:**
- [ ] Decide on GitHub org (Stage11Agentics? LatticeTracker? InferenceConsulting?)
- [ ] Add MIT LICENSE file
- [ ] Write README.md (see template below)
- [ ] Add CONTRIBUTING.md (lightweight)
- [ ] Set up GitHub Actions CI (pytest + ruff on push)
- [ ] Add GitHub topics: `task-tracking`, `agent-native`, `event-sourcing`, `mcp`, `cli`, `ai-agents`, `openclaw`
- [ ] Transfer or mirror from BenevolentFutures/lattice

### 3. MCP Server Registry --- The Multiplier

**Impact:** One registration reaches Claude Code + Cursor + Windsurf + Cline + Continue + Zed + any MCP client.
**Effort:** Low

The MCP ecosystem has several registries/directories:

| Registry | URL | How to list |
|----------|-----|-------------|
| **Smithery** | smithery.ai | Submit via their PR process |
| **mcp.run** | mcp.run | Submit listing |
| **PulseMCP** | pulsemcp.com | Community directory |
| **Awesome MCP Servers** | github.com/punkpeye/awesome-mcp-servers | PR to add |
| **Glama** | glama.ai/mcp | Submit listing |

**MCP config that users would add** (this is what goes in Claude Desktop, Cursor, etc.):

```json
{
  "mcpServers": {
    "lattice": {
      "command": "lattice-mcp",
      "args": []
    }
  }
}
```

That's it. One line in their config, and every Lattice MCP tool is available. No API keys, no network, no accounts.

**Alternative for users who haven't installed globally:**
```json
{
  "mcpServers": {
    "lattice": {
      "command": "uvx",
      "args": ["--from", "lattice-tracker[mcp]", "lattice-mcp"]
    }
  }
}
```

This is the **zero-install** path --- `uvx` downloads and runs in a temporary venv. The user doesn't even need to `pip install` anything.

### 4. OpenClaw Skill --- The Ecosystem Play

**Impact:** Reaches 195K+ star OpenClaw community directly.
**Effort:** Medium

Two approaches, and we should do both:

#### 4a. MCP-Based Integration (Recommended Primary)

OpenClaw is an MCP client. Users can add Lattice as an MCP server in their OpenClaw config:

```json
// ~/.openclaw/mcp.json (or equivalent config)
{
  "servers": {
    "lattice": {
      "command": "lattice-mcp",
      "args": []
    }
  }
}
```

**Advantage:** Uses the MCP server we already built. No OpenClaw-specific code needed.

#### 4b. Native OpenClaw Skill (Complementary)

A `SKILL.md`-based skill that teaches the agent to use the `lattice` CLI directly. This is better for users who prefer CLI over MCP, and it integrates with OpenClaw's skill discovery/injection system.

**Skill directory structure:**
```
lattice/
├── SKILL.md
└── scripts/
    └── lattice-check.sh   # Quick "is lattice installed and initialized?" check
```

**SKILL.md content (draft):**
```yaml
---
name: lattice
description: >-
  Event-sourced task tracking for agents and humans. Use when managing tasks,
  tracking work status, coordinating multi-agent workflows, or maintaining
  an audit trail of who did what and when.
version: 0.1.0
requirements:
  binaries:
    - lattice
  install:
    all: pip install lattice-tracker
---

# Lattice — Agent-Native Task Tracker

Lattice is a file-based, event-sourced task tracker designed for AI agents.
It stores everything in a `.lattice/` directory in your project root (like
`.git/` for version control).

## When to Use Lattice

- User asks to track tasks, create tickets, manage work
- Multi-agent workflows need coordination
- You need an audit trail of task changes
- Planning and organizing development work

## Quick Start

If `.lattice/` doesn't exist in the project, initialize it:
```bash
lattice init --project-code PROJ
```

## Core Commands

### Create a task
```bash
lattice create "Fix the login bug" --actor agent:openclaw --priority high
```

### List tasks
```bash
lattice list                    # All active tasks
lattice list --status in_progress  # Filtered
lattice list --assigned agent:openclaw
```

### Update status
```bash
lattice status LAT-42 in_progress --actor agent:openclaw
lattice status LAT-42 done --actor agent:openclaw
```

### Assign a task
```bash
lattice assign LAT-42 agent:openclaw --actor agent:openclaw
```

### Add a comment
```bash
lattice comment LAT-42 "Found the root cause: race condition in auth middleware" --actor agent:openclaw
```

### Show task details
```bash
lattice show LAT-42              # Summary
lattice show LAT-42 --events     # With full event history
```

### Link tasks
```bash
lattice link LAT-42 blocks LAT-43 --actor agent:openclaw
lattice link LAT-44 subtask_of LAT-42 --actor agent:openclaw
```

### Archive completed work
```bash
lattice archive LAT-42 --actor agent:openclaw
```

## Actor IDs

Always identify yourself when making changes. Format: `prefix:identifier`
- `agent:openclaw` — for your own actions
- `agent:openclaw-worker-1` — for multi-agent setups
- `human:username` — when acting on behalf of a human

## Task IDs

Tasks have both a ULID (`task_01HQ...`) and a short ID (`LAT-42`).
Use short IDs in conversation — they're easier for everyone.

## Output

All commands support `--json` for structured output:
```bash
lattice list --json
```

Returns: `{"ok": true, "data": [...]}` or `{"ok": false, "error": {...}}`

## Notes Files

Every task has a notes file at `.lattice/notes/<task_id>.md`. Use it to
document plans, decisions, and context that outlives a single session.

## Status Workflow

Default: backlog → planned → in_progress → in_review → done

Transitions are enforced. Use `--force --reason "..."` to override.
```

**Publishing to ClawHub:**
```bash
# From the skill directory
npx clawhub publish lattice/
```

### 5. Claude Code Integration --- Our Own Ecosystem

**Impact:** Directly relevant since Lattice already dogfoods with Claude Code.
**Effort:** Low

Claude Code has two integration surfaces:

#### 5a. MCP Server (same as above)

Add to `~/.claude/claude_desktop_config.json` or project-level `.mcp.json`:
```json
{
  "mcpServers": {
    "lattice": {
      "command": "lattice-mcp"
    }
  }
}
```

#### 5b. Claude Code Slash Command / Skill

A `/lattice` skill in `~/.claude/commands/lattice.md` that gives Claude Code agents instructions for using Lattice. This is already partially implemented via the existing `lattice` skill in the project.

### 6. Homebrew Tap --- macOS Convenience

**Impact:** macOS developers (large overlap with agent users)
**Effort:** Medium

```bash
brew tap Stage11Agentics/tap
brew install lattice-tracker
```

**What's needed:**
- [ ] Create `homebrew-tap` repo with formula
- [ ] Formula wraps `pip install` in a virtualenv (or uses `pipx`)

Lower priority than PyPI/MCP/OpenClaw skill. Do this after initial traction.

### 7. Docker Image --- Enterprise and Server Deployments

**Impact:** CI/CD, server-side agents, enterprise environments
**Effort:** Low

```dockerfile
FROM python:3.12-slim
RUN pip install lattice-tracker[mcp]
ENTRYPOINT ["lattice"]
```

For MCP server mode:
```bash
docker run -v $(pwd):/workspace -w /workspace lattice-tracker lattice-mcp
```

Lower priority. Build when there's demand.

---

## The Zero-Friction Install Paths

Ranked by friction (lowest first):

### Path 1: uvx (Zero Install)

```bash
# Works immediately, no install needed
uvx --from "lattice-tracker[mcp]" lattice-mcp
```

Or for CLI:
```bash
uvx --from lattice-tracker lattice init
uvx --from lattice-tracker lattice create "My first task" --actor human:me
```

**Friction:** Near-zero if the user has `uv` installed (increasingly standard in the Python ecosystem).

### Path 2: pipx (Isolated Install)

```bash
pipx install lattice-tracker
lattice init
```

**Friction:** One command. Lattice gets its own isolated environment.

### Path 3: pip (Direct Install)

```bash
pip install lattice-tracker
lattice init
```

**Friction:** One command, but pollutes the global Python environment.

### Path 4: MCP Config Only (For Agent Users)

```json
{
  "mcpServers": {
    "lattice": {
      "command": "uvx",
      "args": ["--from", "lattice-tracker[mcp]", "lattice-mcp"]
    }
  }
}
```

**Friction:** Copy-paste one JSON block. No pip install at all. `uvx` handles everything.

This is the **golden path** for agent users. They paste a config block and Lattice just works.

---

## What Needs to Happen (Ordered)

### Phase 1: Open Source Foundation (Do First)

These are prerequisites for everything else:

1. **Add LICENSE (MIT)**
   - Single file, instant credibility
   - MIT is the standard for developer tools in this ecosystem (OpenClaw is MIT)

2. **Write README.md**
   - Must answer in <30 seconds: "What is this? How do I install it? How do I use it?"
   - Three quick-start paths: CLI, MCP, OpenClaw
   - Badge: PyPI version, license, tests passing

3. **Add project metadata to pyproject.toml**
   - readme, license, classifiers, urls

4. **Set up GitHub Actions CI**
   - pytest + ruff on push/PR
   - Builds confidence for external contributors

5. **Publish to PyPI**
   - `uv build && uv publish` (or `hatch build && hatch publish`)
   - After this, `pip install lattice-tracker` works worldwide

6. **Make repo public**
   - Transfer to the right org first if needed
   - Add GitHub topics for discoverability

### Phase 2: Integration Surface (Week After)

7. **Register on MCP directories**
   - Smithery, Awesome MCP Servers, mcp.run
   - Include the `uvx` config snippet so users can try without installing

8. **Build and publish OpenClaw skill**
   - SKILL.md + helper script
   - Publish to ClawHub
   - Post in OpenClaw community (GitHub Discussions, Discord)

9. **Write integration guides**
   - "Lattice + Claude Code" (CLAUDE.md instructions + MCP config)
   - "Lattice + OpenClaw" (skill install + MCP config)
   - "Lattice + Cursor/Windsurf" (MCP config)
   - "Multi-Agent Task Coordination with Lattice" (the killer use case)

### Phase 3: Community and Growth (Ongoing)

10. **Announce**
    - Hacker News "Show HN"
    - OpenClaw GitHub Discussions
    - AI agent communities (Discord servers, X/Twitter)

11. **Dogfood publicly**
    - Use Lattice in public repos
    - Share screenshots/workflows of agents using Lattice

12. **Iterate on feedback**
    - What's confusing? What's missing?
    - What do OpenClaw users specifically need?

---

## README.md Template

```markdown
# Lattice

File-based, event-sourced task tracking for AI agents and humans.

Lattice stores tasks in a `.lattice/` directory in your project (like `.git/`).
Every change is an event. Events are the source of truth. Snapshots are fast
reads. If they disagree, events win.

## Install

```bash
pip install lattice-tracker
```

## Quick Start

```bash
lattice init --project-code PROJ
lattice create "Fix the login bug" --actor human:me --priority high
lattice list
lattice status PROJ-1 in_progress --actor human:me
lattice status PROJ-1 done --actor human:me
```

## Use with AI Agents

### MCP Server (Claude Code, Cursor, Windsurf, OpenClaw, etc.)

Add to your MCP config:

```json
{
  "mcpServers": {
    "lattice": {
      "command": "uvx",
      "args": ["--from", "lattice-tracker[mcp]", "lattice-mcp"]
    }
  }
}
```

That's it. Your agent now has full task tracking capabilities.

### OpenClaw

Install the Lattice skill:
```bash
npx clawhub install lattice
```

Or add as MCP server in your OpenClaw config.

## Why Lattice?

- **Local-first.** No accounts, no API keys, no network.
- **Event-sourced.** Full audit trail of every change.
- **Agent-native.** CLI + MCP + short IDs (PROJ-42) designed for agents.
- **Concurrent-safe.** File locks prevent corruption from parallel agents.
- **Crash-recoverable.** `lattice rebuild` replays events to fix state.
```

---

## Key Technical Decisions

### Package Name: `lattice-tracker`

`lattice` is taken on PyPI. `lattice-tracker` is clear and available. The CLI command is still just `lattice`.

### License: MIT

Standard for developer tools. Same as OpenClaw. Maximizes adoption.

### GitHub Org

Options:
- `Stage11Agentics/lattice` — company-branded, signals this is a real product
- `lattice-tracker/lattice` — project-branded, feels more community-owned
- Keep `BenevolentFutures/lattice` and redirect later

**Recommendation:** Start with `Stage11Agentics/lattice` for authenticity, and the name Stage 11 Agentics literally describes what this does (agent coordination at fractal scale).

### MCP vs CLI for Agent Integration

Both. MCP is the universal adapter (works with any MCP client). CLI is the universal fallback (works anywhere there's a shell). The OpenClaw skill should document both paths.

The MCP server has one advantage the CLI doesn't: **it returns structured data natively** (JSON dicts) without needing `--json` flags. Agents prefer this.

### The `uvx` Golden Path

`uvx --from "lattice-tracker[mcp]" lattice-mcp` is the lowest-friction path because:
1. No global install needed
2. No venv management needed
3. One line in MCP config
4. Works on macOS, Linux, Windows
5. Always gets the latest version

The only prereq is `uv`, which is rapidly becoming the standard Python tool (installed via `curl -LsSf https://astral.sh/uv/install.sh | sh`).

---

## Metrics to Track

- PyPI downloads (weekly/monthly)
- GitHub stars
- ClawHub skill installs
- MCP registry page views
- GitHub issues/PRs from external contributors
- Mentions in OpenClaw community channels
