# Getting Started

## What you're setting up

Lattice is a shared coordination language that your agentic coding tools speak. It gives agents and humans a common vocabulary — tasks, statuses, events, actors — so that multiple minds can work on the same project without talking past each other.

**You install Lattice on your machine, then use it from inside your existing coding agent** — Claude Code, Codex CLI, OpenClaw, Cursor, Windsurf, or any tool that gives an AI agent filesystem access. Lattice isn't a replacement for those tools. It's the coordination layer that makes them work together.

**What you need:**
- **An agentic coding tool** — if you don't have one, start with [Claude Code](https://docs.anthropic.com/en/docs/claude-code) or [Codex CLI](https://github.com/openai/codex)
- **Python 3.12+** on your machine

## Install

```bash
uv tool install lattice-tracker
```

This installs `lattice` as a global command — available from any directory, any project. No venv activation, no `uv run`. Just `lattice`.

If you see a warning that `lattice` is not on your PATH, run `uv tool update-shell` and restart your terminal.

If you prefer pipx: `pipx install lattice-tracker`. Or plain pip: `pip install lattice-tracker`.

## Upgrading

```bash
uv tool upgrade lattice-tracker
```

If you installed with pipx: `pipx upgrade lattice-tracker`. If you installed with pip: `pip install --upgrade lattice-tracker`.

To check your current version:

```bash
lattice --version
```

## Try the demo (optional)

Before setting up your own project, you can explore a fully populated example:

```bash
lattice demo init
```

This seeds 30 tasks across 4 parent tasks and opens the dashboard in your browser. Press Ctrl+C when you're done looking.

## Initialize in your project

```bash
cd your-project/
lattice init
```

You'll set your identity (`human:yourname`) and a project code (like `APP` for IDs like `APP-1`). This creates a `.lattice/` directory — your task state, living alongside your code.

Commit it to your repo.

## Connect your coding agent

This is the key step. You're teaching your agent the Lattice protocol so it knows how to create tasks, claim work, update status, and leave context — automatically, every session.

**Claude Code** (most common):

```bash
lattice setup-claude
```

This writes a block into your project's `CLAUDE.md` — the file Claude Code reads at startup. From now on, every time you open Claude Code in this project, it already knows Lattice. You don't need to explain anything. It will create tasks before working, update statuses at transitions, and leave breadcrumbs for the next session.

[Full Claude Code integration guide →](integration-claude-code.md)

**Codex CLI:**

```bash
lattice setup-codex
```

Installs the Lattice skill to `~/.agents/skills/lattice/`. Codex reads it at session start and knows the full protocol. [Full guide →](integration-codex.md)

**OpenClaw:**

```bash
lattice setup-openclaw
```

Installs the Lattice skill so your agent uses `lattice` commands naturally. [Full guide →](integration-openclaw.md)

**Any MCP-compatible tool** (Cursor, Windsurf, custom agents):

```bash
pip install lattice-tracker[mcp]
```

```json
{
  "mcpServers": {
    "lattice": {
      "command": "lattice-mcp"
    }
  }
}
```

This exposes Lattice as native tool calls — no CLI parsing needed. [Full MCP guide →](integration-mcp.md)

**Any agent with shell access:**

If your agent can run commands and read files, it can use Lattice. Use `lattice setup-prompt` to print the full instructions to stdout, then paste them into your agent's config:

```bash
lattice setup-prompt              # SKILL.md content (default)
lattice setup-prompt --claude-md  # CLAUDE.md block instead
```

## Open the dashboard

```bash
lattice dashboard
```

This is where you live. The dashboard opens at [http://127.0.0.1:8799](http://127.0.0.1:8799) — a local web UI where you create tasks, set priorities, review agent work, and make decisions. Everything your agents do shows up here in real time.

You don't need to use the CLI after setup. The dashboard handles creating tasks, dragging them between status columns, adding comments, and reviewing the activity feed. Your agents handle the CLI side — you handle the dashboard side.

## Fill the backlog and start the loop

Your backlog already has a couple of example tasks to help you get the feel. You can create more directly in the dashboard, or just tell your agent what you want built — "break down the auth system into tasks" or "create tasks for the MVP features we discussed." The agent will populate the backlog with structured tasks, priorities, and descriptions. You review and adjust from the dashboard.

Once there's work in the backlog, tell your agent to advance — it claims the top task, does the work, and reports back. You come back to a sorted inbox: work in review, decisions waiting, blockers identified.

That's the loop. [Read the full guide →](user-guide.md) to understand it deeply.
