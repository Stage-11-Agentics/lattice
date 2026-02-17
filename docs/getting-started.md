# Getting Started

## Install

```bash
uv tool install lattice-tracker
```

This installs `lattice` as a global command — available from any directory, any project. No venv activation, no `uv run`. Just `lattice`.

If you see a warning that `lattice` is not on your PATH, run `uv tool update-shell` and restart your terminal.

If you prefer pipx: `pipx install lattice-tracker`.

## Try the demo (optional)

Before setting up your own project, you can explore a fully populated example:

```bash
lattice demo init
```

This seeds 30 tasks across 4 epics and opens the dashboard in your browser. Press Ctrl+C when you're done looking.

## Initialize

```bash
cd your-project/
lattice init
```

You'll set your identity (`human:yourname`) and a project code (like `APP` for IDs like `APP-1`). This creates a `.lattice/` directory — your task state, living alongside your code.

Commit it to your repo.

## Connect your agent

**Claude Code:**

```bash
lattice setup-claude
```

This teaches your agent to create tasks before coding, update status at transitions, and leave context for the next session. [Full guide →](integration-claude-code.md)

**OpenClaw:**

```bash
lattice setup-openclaw
```

Installs the Lattice skill so your agent uses `lattice` commands naturally. [Full guide →](integration-openclaw.md)

**Any MCP client:**

```json
{
  "mcpServers": {
    "lattice": {
      "command": "lattice-mcp"
    }
  }
}
```

## Open the dashboard

```bash
lattice dashboard
```

This is where you live. The dashboard opens at [http://127.0.0.1:8799](http://127.0.0.1:8799) — a local web UI where you create tasks, set priorities, review agent work, and make decisions. Everything your agents do shows up here in real time.

You don't need to use the CLI after setup. The dashboard handles creating tasks, dragging them between status columns, adding comments, and reviewing the activity feed. Your agents handle the CLI side — you handle the dashboard side.

## Fill the backlog

Your backlog already has a couple of example tasks to help you get the feel. You can create more directly in the dashboard, or just tell your agent what you want built — "break down the auth system into tasks" or "create tasks for the MVP features we discussed." The agent will populate the backlog with structured tasks, priorities, and descriptions. You review and adjust from the dashboard.

Once there's work in the backlog, tell your agent to advance — it claims the top task, does the work, and reports back. You come back to a sorted inbox: work in review, decisions waiting, blockers identified.

That's the loop. [Read the guide →](/guide) to understand it deeply.
