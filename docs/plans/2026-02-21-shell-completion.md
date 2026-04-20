# Shell Autocompletion

**Date:** 2026-02-21
**Status:** Shipped

Tab-completion for the `lattice` CLI using Click 8.1's native `shell_complete`. Supports bash, zsh, fish.

## Scope

- **Static completion** — commands, subcommands, flags, and `click.Choice` values come for free from Click.
- **Dynamic callbacks** in `src/lattice/completion/__init__.py`:
  - `complete_task_id` — reads `.lattice/ids.json`
  - `complete_status` — reads `workflow.statuses` from `.lattice/config.json`, falls back to defaults
  - `complete_actor` — scans `.lattice/tasks/*.json` for distinct `assigned_to` values
  - `complete_resource_name` / `complete_session_name` — lists files under `.lattice/resources/` and `.lattice/sessions/`
  - `complete_relationship_type` — imports `RELATIONSHIP_TYPES` from `core.relationships` (single source of truth)
- All callbacks swallow exceptions and return `[]` on error — completion must never break the shell.

## `lattice completion` command

One job: print an activation script to stdout. Does **not** modify the user's shell config — that's the user's decision and the user's file.

```bash
lattice completion [--shell bash|zsh|fish] [--json]
```

Auto-detects `$SHELL` when `--shell` is omitted. `--json` wraps the script in `{"ok": true, "data": {"shell": ..., "script": ...}}` for programmatic consumers.

Users wire it into their own rc file with a one-liner (see `docs/getting-started.md`).

## Design decisions

- **No install/uninstall subcommand.** Writing to `~/.bashrc` etc. is a blast radius a task tracker shouldn't own. One line of README copy-paste replaces ~180 lines of shell-specific install logic, atomic writes, backup handling, and fish-vs-bash parity bugs.
- **Import, don't duplicate.** Completion data for relationship types and statuses pulls from the same modules/config that validation uses. Prevents silent drift.
- **Silent failure.** Every callback is try/except-wrapped. A broken `.lattice/` must not break `TAB` in the user's shell.
