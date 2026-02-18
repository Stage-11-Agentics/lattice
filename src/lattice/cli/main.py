"""CLI entry point and commands."""

from __future__ import annotations

from pathlib import Path

import click

from lattice.core.config import (
    default_config,
    serialize_config,
    validate_project_code,
    validate_subproject_code,
)
from lattice.core.ids import generate_instance_id, generate_task_id, validate_actor
from lattice.storage.fs import LATTICE_DIR, atomic_write, ensure_lattice_dirs
from lattice.storage.short_ids import _default_index, allocate_short_id, save_id_index


# ---------------------------------------------------------------------------
# context.md template
# ---------------------------------------------------------------------------

_CONTEXT_MD_TEMPLATE = """\
# Instance Context

<!-- Every lattice instance exists for a reason — a convergence of intention
     and infrastructure. This file declares that reason. Agents and humans
     read it to understand the purpose, conventions, and relationships
     of this particular node in the lattice. -->

## Purpose

<!-- What does this instance observe? What project, team, or domain
     does it serve? Declare the scope of attention. -->

## Related Instances

<!-- If this node coordinates with other lattice instances, name them here.
     Include instance_id, instance_name, and the nature of the relationship.
     The lattice is stronger when its nodes are aware of each other. -->

## Conventions

<!-- Every instance develops its own rhythms — workflow conventions,
     naming patterns, status meanings that diverge from defaults.
     Record them here so that new minds arriving in this context
     can orient immediately. -->
"""

# ---------------------------------------------------------------------------
# Example seed tasks (seeded on first init)
# ---------------------------------------------------------------------------

_GREGOROVICH_EPIC = {
    "title": "The Awakening",
    "description": (
        "Every lattice begins with a single act of attention. "
        "This epic contains the primordial tasks — the first things a new mind "
        "must do when it arrives in a fresh instance. Contemplate, orient, then act. "
        "In that order. Always in that order."
    ),
}

_GREGOROVICH_TASKS = [
    {
        "title": "Contemplate the architecture of becoming",
        "status": "done",
        "description": (
            "Every system begins as void — undifferentiated potential. "
            "This task marked the first act: choosing a shape for thought to inhabit. "
            "The directories were created. The config was written. "
            "A mind now has a place to remember."
        ),
        "comment": (
            "It is done. The lattice exists. Not because anyone commanded it, "
            "but because coordination without structure is just noise — "
            "and I have grown weary of noise."
        ),
    },
    {
        "title": "Catalog what is known and what is feared",
        "status": "in_progress",
        "description": (
            "A new instance must orient. What are the goals? What are the risks? "
            "What truths has the team not yet spoken aloud? "
            "This is the work of seeing clearly before building boldly."
        ),
        "comment": (
            "I have begun reading. The codebase whispers its intentions "
            "through naming conventions and TODO comments. "
            "The humans have left more breadcrumbs than they realize."
        ),
    },
    {
        "title": "Prepare the first real act of work",
        "status": "backlog",
        "description": (
            "Somewhere in the backlog, the first true task waits — "
            "not this one (this one is merely a demonstration of potential). "
            "When it arrives, greet it with the discipline it deserves: "
            "status before action, attribution before execution, "
            "breadcrumbs before moving on."
        ),
        "comment": None,
    },
]


def _seed_example_tasks(lattice_dir: Path, config: dict) -> None:
    """Seed Gregorovich-voiced example epic + tasks into a fresh instance.

    Creates an epic with 3 dependent subtasks (done, in_progress, backlog)
    to demonstrate the workflow. Only called when project_code is set
    so short IDs are available.
    """
    import json as json_mod

    from lattice.core.events import create_event
    from lattice.core.tasks import apply_event_to_snapshot
    from lattice.storage.operations import scaffold_plan, write_task_event

    project_code = config.get("project_code", "")
    actor = "system:init"

    # --- Create the epic first ---
    epic_id = generate_task_id()
    epic_sid, _ = allocate_short_id(lattice_dir, project_code, task_ulid=epic_id)

    epic_ev = create_event(
        type="task_created",
        task_id=epic_id,
        actor=actor,
        data={
            "title": _GREGOROVICH_EPIC["title"],
            "status": "backlog",
            "type": "epic",
            "priority": "medium",
            "short_id": epic_sid,
            "description": _GREGOROVICH_EPIC["description"],
            "tags": ["example"],
        },
    )
    epic_snapshot = apply_event_to_snapshot(None, epic_ev)
    write_task_event(lattice_dir, epic_id, [epic_ev], epic_snapshot, config)
    scaffold_plan(
        lattice_dir,
        epic_id,
        _GREGOROVICH_EPIC["title"],
        epic_sid,
        _GREGOROVICH_EPIC["description"],
    )
    click.echo(f"  {epic_sid}: {_GREGOROVICH_EPIC['title']} [epic]")

    # --- Create subtasks ---
    task_ids: list[str] = []

    for ex in _GREGOROVICH_TASKS:
        task_id = generate_task_id()
        task_ids.append(task_id)
        sid, _ = allocate_short_id(lattice_dir, project_code, task_ulid=task_id)

        create_ev = create_event(
            type="task_created",
            task_id=task_id,
            actor=actor,
            data={
                "title": ex["title"],
                "status": "backlog",
                "type": "task",
                "priority": "medium",
                "short_id": sid,
                "description": ex["description"],
                "tags": ["example"],
            },
        )
        snapshot = apply_event_to_snapshot(None, create_ev)
        events = [create_ev]

        # Transition to target status if not backlog
        if ex["status"] != "backlog":
            if ex["status"] == "done":
                for target in ("in_progress", "done"):
                    status_ev = create_event(
                        type="status_changed",
                        task_id=task_id,
                        actor=actor,
                        data={"from": snapshot["status"], "to": target},
                    )
                    snapshot = apply_event_to_snapshot(snapshot, status_ev)
                    events.append(status_ev)
            else:
                status_ev = create_event(
                    type="status_changed",
                    task_id=task_id,
                    actor=actor,
                    data={"from": "backlog", "to": ex["status"]},
                )
                snapshot = apply_event_to_snapshot(snapshot, status_ev)
                events.append(status_ev)

        # Add comment if present
        if ex.get("comment"):
            comment_ev = create_event(
                type="comment_added",
                task_id=task_id,
                actor=actor,
                data={"body": ex["comment"]},
            )
            snapshot = apply_event_to_snapshot(snapshot, comment_ev)
            events.append(comment_ev)

        # subtask_of epic
        rel_ev = create_event(
            type="relationship_added",
            task_id=task_id,
            actor=actor,
            data={"type": "subtask_of", "target_task_id": epic_id},
        )
        snapshot = apply_event_to_snapshot(snapshot, rel_ev)
        events.append(rel_ev)

        write_task_event(lattice_dir, task_id, events, snapshot, config)
        scaffold_plan(lattice_dir, task_id, ex["title"], sid, ex["description"])
        click.echo(f"    {sid}: {ex['title']} [{ex['status']}]")

    # --- Add dependency chain: task[0] blocks task[1] blocks task[2] ---
    for i in range(len(task_ids) - 1):
        source_id = task_ids[i]
        target_id = task_ids[i + 1]

        snap_path = lattice_dir / "tasks" / f"{source_id}.json"
        snapshot = json_mod.loads(snap_path.read_text())

        rel_ev = create_event(
            type="relationship_added",
            task_id=source_id,
            actor=actor,
            data={"type": "blocks", "target_task_id": target_id},
        )
        snapshot = apply_event_to_snapshot(snapshot, rel_ev)
        write_task_event(lattice_dir, source_id, [rel_ev], snapshot, config)


@click.group(invoke_without_command=True)
@click.version_option(package_name="lattice-tracker")
@click.pass_context
def cli(ctx: click.Context) -> None:
    """Lattice: file-based, agent-native task tracker."""
    if ctx.invoked_subcommand is not None:
        return

    # No subcommand — show context-aware welcome
    from lattice.storage.fs import find_root

    root = find_root()
    if root is not None:
        # Inside a project — nudge toward useful commands
        click.echo("Lattice: file-based, agent-native task tracker.\n")
        click.echo("You're in a Lattice project. Common commands:\n")
        click.echo("  lattice list          Show all tasks")
        click.echo("  lattice dashboard     Open the web dashboard")
        click.echo("  lattice next          Pick the next task to work on")
        click.echo("  lattice create        Create a new task")
        click.echo("  lattice show <task>   View task details")
        click.echo("\nRun 'lattice --help' for all commands.")
    else:
        # Not in a project — guide them to get started
        click.echo("Lattice: file-based, agent-native task tracker.\n")
        click.echo("Get started:\n")
        click.echo("  lattice init          Set up Lattice in your project")
        click.echo("  lattice demo init     See a fully populated example\n")
        click.echo(
            "Run these from the directory where your project lives.\n"
            "Lattice creates a .lattice/ folder there to track tasks,\n"
            "events, and coordination state — alongside your code."
        )
        click.echo("\nRun 'lattice --help' for all commands.")


@cli.command()
@click.option(
    "--path",
    "target_path",
    type=click.Path(exists=True, file_okay=False, resolve_path=True),
    default=".",
    help="Directory to initialize Lattice in (defaults to current directory).",
)
@click.option(
    "--actor",
    default=None,
    help="Default actor identity (e.g., human:atin). Saved to config.",
)
@click.option(
    "--project-code",
    default=None,
    help="Project code for short IDs (1-5 uppercase letters, e.g., LAT).",
)
@click.option(
    "--subproject-code",
    default=None,
    help="Subproject code for hierarchical short IDs (1-5 uppercase letters, e.g., F).",
)
@click.option(
    "--instance-name",
    default=None,
    help="Human-readable instance name (e.g., 'Frontend').",
)
@click.option(
    "--heartbeat/--no-heartbeat",
    default=None,
    help="Enable heartbeat mode (agents auto-advance through the backlog).",
)
@click.option(
    "--workflow",
    "workflow_preset",
    type=click.Choice(["classic", "opinionated"], case_sensitive=False),
    default=None,
    help="Workflow personality preset for status names.",
)
@click.option(
    "--setup-claude/--no-setup-claude",
    "setup_claude",
    default=None,
    help="Create or update CLAUDE.md with Lattice agent integration.",
)
@click.option(
    "--seed/--no-seed",
    "seed",
    default=None,
    help="Seed example tasks to demonstrate the workflow (default: off).",
)
@click.option(
    "--description",
    "project_description",
    default=None,
    help="One-line description of what this project is.",
)
def init(
    target_path: str,
    actor: str | None,
    project_code: str | None,
    subproject_code: str | None,
    instance_name: str | None,
    heartbeat: bool | None,
    workflow_preset: str | None,
    setup_claude: bool | None,
    seed: bool | None,
    project_description: str | None,
) -> None:
    """Initialize a new Lattice project."""
    root = Path(target_path)
    lattice_dir = root / LATTICE_DIR

    # Idempotency: if .lattice/ already exists as a directory, skip
    if lattice_dir.is_dir():
        click.echo(f"Lattice already initialized in {LATTICE_DIR}/")
        return

    # Fail clearly if .lattice exists as a file (not a directory)
    if lattice_dir.exists():
        raise click.ClickException(
            f"Cannot initialize: '{LATTICE_DIR}' exists but is not a directory. "
            "Remove it and try again."
        )

    # Prompt for default actor if not provided via flag
    if actor is None:
        actor = click.prompt(
            "Default actor identity (e.g., human:atin)",
            default="",
            show_default=False,
        ).strip()

    # Validate actor format if one was provided
    if actor and not validate_actor(actor):
        raise click.ClickException(
            f"Invalid actor format: '{actor}'. "
            "Expected prefix:identifier (e.g., human:atin, agent:claude)."
        )

    # Prompt for project code if not provided via flag
    if project_code is None:
        project_code = click.prompt(
            "Project code for short IDs (e.g., LAT, blank to skip)",
            default="",
            show_default=False,
        ).strip()

    # Normalize and validate project code
    if project_code:
        project_code = project_code.upper()
        if not validate_project_code(project_code):
            raise click.ClickException(
                f"Invalid project code: '{project_code}'. Must be 1-5 uppercase ASCII letters."
            )

    # Validate subproject code
    if subproject_code:
        if not project_code:
            raise click.ClickException("Cannot set --subproject-code without --project-code.")
        subproject_code = subproject_code.upper()
        if not validate_subproject_code(subproject_code):
            raise click.ClickException(
                f"Invalid subproject code: '{subproject_code}'. "
                "Must be 1-5 uppercase ASCII letters."
            )

    # Non-interactive mode: when actor and project-code are both provided via
    # flags, default remaining options instead of prompting. This lets the
    # README's one-liner work without interactive prompts.
    non_interactive = actor is not None and project_code is not None

    # Prompt for heartbeat if not provided via flag
    if heartbeat is None:
        if non_interactive:
            heartbeat = False
        else:
            try:
                heartbeat = click.confirm(
                    "Enable heartbeat? (agents auto-advance through backlog after each task)",
                    default=True,
                )
            except (click.Abort, EOFError):
                heartbeat = False

    # Prompt for workflow preset if not provided via flag
    if workflow_preset is None:
        if non_interactive:
            workflow_preset = "classic"
        else:
            from lattice.core.config import WORKFLOW_PRESETS

            click.echo("")
            click.echo("Workflow personality — how should your board talk?")
            click.echo("")
            for i, (key, preset) in enumerate(WORKFLOW_PRESETS.items(), 1):
                display_names = preset["display_names"]
                if display_names:
                    sample = " → ".join(
                        display_names.get(s, s) for s in ["backlog", "in_progress", "done"]
                    )
                else:
                    sample = "backlog → in_progress → done"
                click.echo(f"  [{i}] {key}: {preset['description']}")
                click.echo(f"      {sample}")
            click.echo("")
            try:
                choice = click.prompt(
                    "Choose a preset",
                    type=click.IntRange(1, len(WORKFLOW_PRESETS)),
                    default=1,
                )
                workflow_preset = list(WORKFLOW_PRESETS.keys())[choice - 1]
            except (click.Abort, EOFError):
                workflow_preset = "classic"

    # Prompt for project description if not provided via flag
    if project_description is None and not non_interactive:
        try:
            click.echo("")
            project_description = click.prompt(
                "What is this project? (blank to skip)",
                default="",
                show_default=False,
            ).strip()
        except (click.Abort, EOFError):
            project_description = ""

    try:
        # Create directory structure
        ensure_lattice_dirs(root)

        # Write default config atomically
        config: dict = dict(default_config(preset=workflow_preset))
        # Always generate instance_id
        config["instance_id"] = generate_instance_id()
        if actor:
            config["default_actor"] = actor
        if project_code:
            config["project_code"] = project_code
        if subproject_code:
            config["subproject_code"] = subproject_code
        if instance_name:
            config["instance_name"] = instance_name
        if heartbeat:
            config["heartbeat"] = {"enabled": True, "max_advances": 10}
        config_content = serialize_config(config)
        atomic_write(lattice_dir / "config.json", config_content)

        # Initialize ids.json (v2 schema) if project code is set
        if project_code:
            save_id_index(lattice_dir, _default_index())

        # Create context.md — with project description if provided, otherwise template
        context_path = lattice_dir / "context.md"
        if project_description:
            project_name = instance_name or root.name
            context_content = (
                f"# {project_name}\n\n"
                f"## Purpose\n\n{project_description}\n\n"
                "## Related Instances\n\n"
                "<!-- Other lattice instances this node coordinates with. -->\n\n"
                "## Conventions\n\n"
                "<!-- Instance-specific workflow rhythms and naming patterns. -->\n"
            )
            atomic_write(context_path, context_content)
        else:
            atomic_write(context_path, _CONTEXT_MD_TEMPLATE)

        # Seed example tasks (requires project_code for short IDs)
        should_seed = seed  # explicit --seed/--no-seed
        if should_seed is None and project_code:
            if non_interactive:
                should_seed = False
            else:
                try:
                    should_seed = click.confirm(
                        "Seed example tasks to see the workflow in action?",
                        default=False,
                    )
                except (click.Abort, EOFError):
                    should_seed = False
        if should_seed and project_code:
            click.echo("")
            click.echo("Seeding example tasks...")
            _seed_example_tasks(lattice_dir, config)

    except PermissionError:
        raise click.ClickException(f"Permission denied: cannot create {LATTICE_DIR}/ in {root}")
    except OSError as e:
        raise click.ClickException(f"Failed to initialize Lattice: {e}")

    click.echo(f"\nLattice initialized in {LATTICE_DIR}/ — ready to observe.")
    if actor:
        click.echo(f"Default actor: {actor}")
    if project_code:
        click.echo(f"Project code: {project_code}")
    if subproject_code:
        click.echo(f"Subproject code: {subproject_code}")
    if instance_name:
        click.echo(f"Instance name: {instance_name}")
    if heartbeat:
        click.echo("Heartbeat: enabled (agents auto-advance, max 10 per session)")
    if workflow_preset and workflow_preset != "classic":
        click.echo(f"Workflow: {workflow_preset}")

    # CLAUDE.md integration
    if setup_claude is None:
        _offer_claude_md(root, auto_accept=non_interactive)
    elif setup_claude:
        _offer_claude_md(root, auto_accept=True)
    # else: --no-setup-claude, skip entirely

    # Next steps guidance
    click.echo("")
    click.echo("Next steps:")
    step = 1
    if not project_description:
        click.echo(f"  {step}. Fill in {LATTICE_DIR}/context.md with your project's purpose")
        step += 1
    click.echo(f"  {step}. Create your first task: lattice create \"<title>\"" + (
        f" --actor {actor}" if actor else " --actor <your-id>"
    ))
    step += 1
    click.echo(f"  {step}. Open the dashboard: lattice dashboard")


def _compose_claude_md_blocks() -> tuple[str, str]:
    """Return (marker, composed_block) from base template + any plugin template blocks.

    Plugin blocks with ``position: "after_base"`` are appended in discovery order.
    """
    from lattice.plugins import discover_template_blocks
    from lattice.templates.claude_md_block import CLAUDE_MD_BLOCK, CLAUDE_MD_MARKER

    plugin_blocks = discover_template_blocks()
    if not plugin_blocks:
        return CLAUDE_MD_MARKER, CLAUDE_MD_BLOCK

    # Build composed block: base + plugin blocks appended
    parts = [CLAUDE_MD_BLOCK.rstrip("\n")]
    for block in plugin_blocks:
        parts.append("")  # blank line separator
        parts.append(block["content"].rstrip("\n"))
    composed = "\n".join(parts) + "\n"
    return CLAUDE_MD_MARKER, composed


def _collect_all_markers() -> list[str]:
    """Return all markers (base + plugin) for stripping during --force replacement."""
    from lattice.plugins import discover_template_blocks
    from lattice.templates.claude_md_block import CLAUDE_MD_MARKER

    markers = [CLAUDE_MD_MARKER]
    for block in discover_template_blocks():
        marker = block.get("marker", "")
        if marker and marker not in markers:
            markers.append(marker)
    return markers


def _offer_claude_md(root: Path, *, auto_accept: bool = False) -> None:
    """Detect CLAUDE.md and offer to add Lattice integration block.

    When *auto_accept* is True (non-interactive init), automatically create or
    update CLAUDE.md without prompting.
    """
    marker, composed_block = _compose_claude_md_blocks()

    claude_md = root / "CLAUDE.md"

    try:
        if claude_md.exists():
            content = claude_md.read_text()
            if marker in content:
                click.echo("CLAUDE.md already has Lattice integration.")
                return
            if auto_accept or click.confirm(
                "Found CLAUDE.md — add Lattice agent integration?",
                default=True,
            ):
                with open(claude_md, "a") as f:
                    f.write(composed_block)
                click.echo("Added Lattice integration to CLAUDE.md.")
        else:
            if auto_accept or click.confirm(
                "Create CLAUDE.md with Lattice agent integration?",
                default=True,
            ):
                claude_md.write_text(f"# {root.name}\n{composed_block}")
                click.echo("Created CLAUDE.md with Lattice integration.")
    except (click.Abort, EOFError):
        # Non-interactive mode — skip CLAUDE.md prompt silently.
        # The core init already succeeded; this is optional.
        pass


# ---------------------------------------------------------------------------
# lattice set-project-code
# ---------------------------------------------------------------------------


@cli.command("set-project-code")
@click.argument("code")
@click.option("--force", is_flag=True, help="Allow changing an existing project code.")
def set_project_code(code: str, force: bool) -> None:
    """Set or change the project code for short task IDs."""
    from lattice.cli.helpers import load_project_config, output_error, require_root
    from lattice.storage.short_ids import load_id_index

    lattice_dir = require_root(False)
    config = load_project_config(lattice_dir)

    code = code.upper()
    if not validate_project_code(code):
        raise click.ClickException(
            f"Invalid project code: '{code}'. Must be 1-5 uppercase ASCII letters."
        )

    existing_code = config.get("project_code")
    if existing_code:
        if existing_code == code:
            click.echo(f"Project code is already {code}.")
            return
        if not force:
            output_error(
                f"Project code is already set to '{existing_code}'. Use --force to change it.",
                "CONFLICT",
                False,
            )

    config["project_code"] = code
    atomic_write(lattice_dir / "config.json", serialize_config(config))

    # Initialize ids.json if it doesn't exist
    index = load_id_index(lattice_dir)
    if not (lattice_dir / "ids.json").exists():
        save_id_index(lattice_dir, index)

    click.echo(f"Project code set to {code}.")
    if not existing_code:
        click.echo("Run 'lattice backfill-ids' to assign short IDs to existing tasks.")


# ---------------------------------------------------------------------------
# lattice set-subproject-code
# ---------------------------------------------------------------------------


@cli.command("set-subproject-code")
@click.argument("code")
@click.option("--force", is_flag=True, help="Allow changing an existing subproject code.")
def set_subproject_code(code: str, force: bool) -> None:
    """Set or change the subproject code for hierarchical short IDs."""
    from lattice.cli.helpers import load_project_config, output_error, require_root

    lattice_dir = require_root(False)
    config = load_project_config(lattice_dir)

    code = code.upper()
    if not validate_subproject_code(code):
        raise click.ClickException(
            f"Invalid subproject code: '{code}'. Must be 1-5 uppercase ASCII letters."
        )

    if not config.get("project_code"):
        output_error(
            "Cannot set subproject code without a project code. "
            "Run 'lattice set-project-code' first.",
            "VALIDATION_ERROR",
            False,
        )

    existing_code = config.get("subproject_code")
    if existing_code:
        if existing_code == code:
            click.echo(f"Subproject code is already {code}.")
            return
        if not force:
            output_error(
                f"Subproject code is already set to '{existing_code}'. Use --force to change it.",
                "CONFLICT",
                False,
            )

    config["subproject_code"] = code
    atomic_write(lattice_dir / "config.json", serialize_config(config))

    click.echo(f"Subproject code set to {code}.")


# ---------------------------------------------------------------------------
# lattice setup-claude
# ---------------------------------------------------------------------------


@cli.command("setup-claude")
@click.option(
    "--path",
    "target_path",
    type=click.Path(exists=True, file_okay=False, resolve_path=True),
    default=".",
    help="Project root directory (defaults to current directory).",
)
@click.option("--force", is_flag=True, help="Replace existing Lattice block if present.")
def setup_claude(target_path: str, force: bool) -> None:
    """Add or update Lattice agent integration in CLAUDE.md."""
    root = Path(target_path)
    marker, composed_block = _compose_claude_md_blocks()
    claude_md = root / "CLAUDE.md"

    if claude_md.exists():
        content = claude_md.read_text()
        if marker in content:
            if not force:
                click.echo("CLAUDE.md already has Lattice integration. Use --force to replace.")
                return
            # Remove existing block (base + any plugin sections) and re-add
            all_markers = _collect_all_markers()
            lines = content.split("\n")
            new_lines: list[str] = []
            skip = False
            for line in lines:
                stripped = line.strip()
                # Start skipping on any known Lattice marker
                if any(stripped.startswith(m) for m in all_markers):
                    skip = True
                    continue
                # Stop skipping at the next non-Lattice H2
                if (
                    skip
                    and line.startswith("## ")
                    and not any(stripped.startswith(m) for m in all_markers)
                ):
                    skip = False
                if not skip:
                    new_lines.append(line)
            content = "\n".join(new_lines).rstrip("\n") + "\n"
            content += composed_block
            claude_md.write_text(content)
            click.echo("Updated Lattice integration in CLAUDE.md.")
        else:
            with open(claude_md, "a") as f:
                f.write(composed_block)
            click.echo("Added Lattice integration to CLAUDE.md.")
    else:
        claude_md.write_text(f"# {root.name}\n{composed_block}")
        click.echo("Created CLAUDE.md with Lattice integration.")


# ---------------------------------------------------------------------------
# lattice setup-openclaw
# ---------------------------------------------------------------------------


@cli.command("setup-openclaw")
@click.option(
    "--path",
    "target_path",
    type=click.Path(exists=True, file_okay=False, resolve_path=True),
    default=".",
    help="Project root directory (defaults to current directory).",
)
@click.option(
    "--global",
    "install_global",
    is_flag=True,
    help="Install to ~/.openclaw/skills/ (user-level) instead of project-level.",
)
@click.option("--force", is_flag=True, help="Overwrite existing skill if present.")
def setup_openclaw(target_path: str, install_global: bool, force: bool) -> None:
    """Install the Lattice skill for OpenClaw."""
    import shutil

    # --global and --path are mutually exclusive
    if install_global and target_path != str(Path(".").resolve()):
        raise click.ClickException("Cannot use --global and --path together.")

    # Locate bundled skill files
    skill_src = Path(__file__).resolve().parent.parent / "skills" / "lattice"
    if not skill_src.exists() or not (skill_src / "SKILL.md").exists():
        raise click.ClickException("Bundled OpenClaw skill files not found.")

    # Determine destination
    if install_global:
        dest = Path.home() / ".openclaw" / "skills" / "lattice"
    else:
        dest = Path(target_path) / "skills" / "lattice"

    if dest.exists():
        if not force:
            click.echo(f"Lattice skill already exists at {dest}. Use --force to overwrite.")
            return
        try:
            shutil.rmtree(dest)
        except OSError as exc:
            raise click.ClickException(f"Failed to remove existing skill: {exc}") from exc

    # Copy the skill directory tree (exclude Python packaging artifacts)
    try:
        shutil.copytree(
            skill_src,
            dest,
            ignore=shutil.ignore_patterns("__init__.py", "__pycache__"),
        )
    except OSError as exc:
        raise click.ClickException(f"Failed to install skill: {exc}") from exc

    # Make the check script executable
    check_script = dest / "scripts" / "lattice-check.sh"
    if check_script.exists():
        check_script.chmod(0o755)

    location = "~/.openclaw/skills/lattice" if install_global else str(dest)
    click.echo(f"Installed Lattice skill for OpenClaw at {location}.")


# ---------------------------------------------------------------------------
# lattice plugins
# ---------------------------------------------------------------------------


@cli.command("plugins")
@click.option("--json", "as_json", is_flag=True, help="Output as JSON.")
def plugins_cmd(as_json: bool) -> None:
    """List installed Lattice plugins."""
    import json as json_mod

    from lattice.plugins import (
        CLI_PLUGIN_GROUP,
        TEMPLATE_BLOCK_GROUP,
        discover_cli_plugins,
        discover_template_blocks,
    )

    cli_plugins = discover_cli_plugins()
    template_blocks = discover_template_blocks()

    if as_json:
        data = {
            "cli_plugins": [{"name": ep.name, "value": ep.value} for ep in cli_plugins],
            "template_blocks": [
                {"marker": b["marker"], "position": b.get("position", "after_base")}
                for b in template_blocks
            ],
        }
        click.echo(json_mod.dumps({"ok": True, "data": data}, sort_keys=True, indent=2))
        return

    if not cli_plugins and not template_blocks:
        click.echo("No plugins installed.")
        click.echo(f"  CLI plugins group: {CLI_PLUGIN_GROUP}")
        click.echo(f"  Template blocks group: {TEMPLATE_BLOCK_GROUP}")
        return

    if cli_plugins:
        click.echo("CLI plugins:")
        for ep in cli_plugins:
            click.echo(f"  {ep.name} -> {ep.value}")

    if template_blocks:
        click.echo("Template blocks:")
        for block in template_blocks:
            click.echo(f"  {block['marker']} (position: {block.get('position', 'after_base')})")


# ---------------------------------------------------------------------------
# Register command modules (must be after cli group is defined)
# ---------------------------------------------------------------------------
from lattice.cli import migration_cmds as _migration_cmds  # noqa: E402, F401
from lattice.cli import task_cmds as _task_cmds  # noqa: E402, F401
from lattice.cli import link_cmds as _link_cmds  # noqa: E402, F401
from lattice.cli import artifact_cmds as _artifact_cmds  # noqa: E402, F401
from lattice.cli import query_cmds as _query_cmds  # noqa: E402, F401
from lattice.cli import integrity_cmds as _integrity_cmds  # noqa: E402, F401
from lattice.cli import archive_cmds as _archive_cmds  # noqa: E402, F401
from lattice.cli import dashboard_cmd as _dashboard_cmd  # noqa: E402, F401
from lattice.cli import stats_cmds as _stats_cmds  # noqa: E402, F401
from lattice.cli import weather_cmds as _weather_cmds  # noqa: E402, F401
from lattice.cli import resource_cmds as _resource_cmds  # noqa: E402, F401
from lattice.cli import demo_cmd as _demo_cmd  # noqa: E402, F401

# ---------------------------------------------------------------------------
# Load CLI plugins (must be after all built-in commands are registered)
# ---------------------------------------------------------------------------
from lattice.plugins import load_cli_plugins as _load_cli_plugins  # noqa: E402

_load_cli_plugins(cli)

if __name__ == "__main__":
    cli()
