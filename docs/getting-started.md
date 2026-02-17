# Getting Started

## Install

```bash
pip install lattice-tracker
```

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

## Create tasks, advance, review

```bash
lattice create "Build the login page" --actor human:yourname --priority high
lattice create "Add rate limiting" --actor human:yourname --priority medium
lattice dashboard
```

Then tell your agent to advance — it claims the top task, does the work, and reports back. You come back to a sorted inbox: work in review, decisions waiting, blockers identified.

That's the loop. [Read the guide →](/guide) to understand it deeply.

## Quick reference

| Action | Command |
|--------|---------|
| Install | `pip install lattice-tracker` |
| Initialize | `lattice init` |
| Create task | `lattice create "Title" --actor human:you` |
| Change status | `lattice status ID STATUS --actor human:you` |
| List tasks | `lattice list` |
| Show task | `lattice show ID` |
| Open dashboard | `lattice dashboard` |
| Daily digest | `lattice weather` |
| Claude Code setup | `lattice setup-claude` |
| OpenClaw setup | `lattice setup-openclaw` |
