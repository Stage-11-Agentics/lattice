# Lattice + OpenClaw Integration Guide

Two ways to use Lattice with OpenClaw: the **Agent Skill** (CLI-based) and the **MCP server** (structured tool calls). Both paths give your OpenClaw agent full task tracking capabilities.

## Option 1: OpenClaw Skill (Recommended)

The Lattice skill teaches your OpenClaw agent to use the `lattice` CLI directly. This is the most natural integration — OpenClaw's skill system was designed for exactly this pattern.

### Install from ClawHub

```bash
npx clawhub install lattice
```

### Or install locally

Copy the `skills/lattice/` directory into your workspace or managed skills:

```bash
# Workspace-level (this project only)
cp -r skills/lattice/ ./skills/lattice/

# User-level (all projects)
cp -r skills/lattice/ ~/.openclaw/skills/lattice/
```

### Prerequisites

The skill requires the `lattice` CLI binary. Install it:

```bash
pip install lattice-tracker    # or
pipx install lattice-tracker   # isolated install, or
uv tool install lattice-tracker  # modern Python tooling
```

### What happens

When your OpenClaw agent encounters task-related conversations (tracking work, managing tasks, coordinating agents), the skill injects Lattice CLI instructions into the agent's context. The agent then uses `lattice` commands like any other CLI tool.

## Option 2: MCP Server

Lattice includes a built-in MCP server that exposes all operations as structured tools. This gives agents typed inputs/outputs instead of parsing CLI text.

### Configure MCP

Add to your OpenClaw MCP configuration:

```json
{
  "servers": {
    "lattice": {
      "command": "lattice-mcp",
      "args": []
    }
  }
}
```

### Zero-install MCP (via uvx)

If you don't want to install Lattice globally, use `uvx` to run it on demand:

```json
{
  "servers": {
    "lattice": {
      "command": "uvx",
      "args": ["--from", "lattice-tracker[mcp]", "lattice-mcp"]
    }
  }
}
```

This downloads and runs Lattice in a temporary environment. No `pip install` needed — just `uv`.

### Available MCP Tools

The MCP server exposes these tools:

| Tool | Description |
|------|-------------|
| `lattice_create` | Create a new task |
| `lattice_update` | Update task fields (title, description, priority, type) |
| `lattice_status` | Change task status |
| `lattice_assign` | Assign a task to an actor |
| `lattice_comment` | Add a comment to a task |
| `lattice_link` | Create a relationship between tasks |
| `lattice_unlink` | Remove a relationship |
| `lattice_attach` | Attach a file or URL to a task |
| `lattice_archive` | Archive a completed task |
| `lattice_unarchive` | Restore an archived task |
| `lattice_event` | Record a custom event |
| `lattice_list` | List tasks with filters |
| `lattice_show` | Show detailed task information |
| `lattice_config` | Read project configuration |
| `lattice_doctor` | Check project data integrity |

All tools accept an optional `lattice_root` parameter to specify the project directory. If omitted, Lattice finds `.lattice/` by walking up from the current directory.

## Which to Choose?

| Consideration | Skill | MCP |
|---------------|-------|-----|
| Setup complexity | Lower (copy directory) | Lower (paste JSON config) |
| Output format | Text (parsed by agent) | Structured JSON (native) |
| Works offline | Yes | Yes |
| Multi-client | OpenClaw only | Any MCP client |
| Dependency | `lattice` binary | `lattice-mcp` binary (or `uvx`) |

**Use the skill** if you primarily use OpenClaw and want the most natural integration.
**Use MCP** if you use multiple AI tools (Claude Code, Cursor, etc.) and want one config for all of them.
**Use both** for maximum flexibility — they don't conflict.

## Quick Start

After installing via either method:

```bash
# Initialize Lattice in your project
lattice init --project-code MYAPP

# Your OpenClaw agent can now:
# - Create tasks: "Create a task to fix the auth bug"
# - Track status: "What tasks are in progress?"
# - Update work: "Mark MYAPP-3 as done"
# - Coordinate: "What should I work on next?"
```

## Actor ID Convention

Configure your OpenClaw agent to use `agent:openclaw` as its actor ID:

```bash
lattice create "My task" --actor agent:openclaw
```

For multi-agent setups, use unique IDs per agent instance:

```bash
# Main agent
--actor agent:openclaw

# Worker agents
--actor agent:openclaw-worker-1
--actor agent:openclaw-worker-2
```

## Further Reading

- [Getting Started Guide](getting-started.md) — Full Lattice setup walkthrough
- [User Guide](user-guide.md) — Complete command reference
- [MCP Integration](integration-mcp.md) — Detailed MCP configuration for all clients
- [Multi-Agent Guide](../skills/lattice/references/multi-agent-guide.md) — Coordinating multiple agents
