# Using Lattice with Codex CLI

Codex CLI (OpenAI's terminal agent) integrates with Lattice through a skill-based setup -- the same pattern used by Claude Code and OpenClaw. One command, and Codex knows the full Lattice protocol.

## Setup

```bash
lattice setup-codex
```

This installs the Lattice skill to `~/.agents/skills/lattice/`. Codex reads the `SKILL.md` at session start and learns the full lifecycle: creating tasks, claiming work, updating statuses, leaving context for the next agent.

```bash
lattice setup-codex --force   # update to latest version
```

From this point on, when you run Codex in a project with `.lattice/` initialized, it already knows the protocol. Tell it to advance the project, and it will claim a task, do the work, and leave breadcrumbs.

## How it works

The skill file teaches Codex the same workflow that Claude Code and OpenClaw follow:

1. **Claim** the highest-priority available task (`lattice next --claim`)
2. **Plan** if needed (write to `.lattice/plans/<task_id>.md`)
3. **Implement** the work, committing as you go
4. **Complete** with a review (`lattice complete <task_id> --review "..."`)

The commands, statuses, and lifecycle are identical across all agents. A task created by Claude Code can be picked up by Codex, and vice versa.

## Codex workflow with Lattice

Codex operates by reading files and running shell commands. Lattice's CLI-first design means Codex interacts with it the same way any agent would.

### Reading task state

```bash
# List tasks via CLI
lattice list --json

# Read a specific task snapshot directly
cat .lattice/tasks/task_01HQEXAMPLE.json

# Check what's in progress
lattice list --status in_progress --json
```

### A typical Codex session

```bash
# 1. Claim the next task
lattice next --claim --actor agent:codex --json

# 2. Read the task details and plan
lattice show LAT-22
cat .lattice/plans/task_01HQEXAMPLE.md

# 3. Do the work (Codex edits files, runs tests, etc.)
# ...

# 4. Complete with review
lattice complete LAT-22 --review "Refactored auth module. Added retry logic for token refresh. All tests passing." --actor agent:codex
```

## Alternative: shared commands via hardlinks

Claude Code and Codex CLI both support slash commands loaded from markdown files:

| Tool | Command directory |
|------|-------------------|
| Claude Code | `~/.claude/commands/*.md` |
| Codex CLI | `~/.codex/prompts/*.md` |

You can hardlink them so a single file serves both tools:

```bash
~/.claude/scripts/sync-commands-to-codex.sh
```

This is optional if you've already run `lattice setup-codex` -- the skill-based approach is the primary integration.

## Actor conventions

Use `agent:codex` as the actor identity when Codex operates autonomously:

```bash
lattice status LAT-15 in_progress --actor agent:codex
lattice comment LAT-15 "Starting work" --actor agent:codex
```

If Codex is acting on behalf of a human who directed the work, use the human's actor:

```bash
lattice create "Task the human described" --actor human:atin
```

The attribution rules are the same as for Claude Code -- the actor is the mind that made the decision, not the tool that typed the command.

## Notes

- **Skill location.** `~/.agents/skills/lattice/SKILL.md` -- installed by `lattice setup-codex`.
- **No MCP support.** Codex does not support MCP, so it uses the CLI for all Lattice operations.
- **Stdin limitations.** Codex ignores stdin. Use file references instead (write prompt to a file, tell Codex to read it).
