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
from lattice.core.ids import generate_instance_id, validate_actor
from lattice.storage.fs import LATTICE_DIR, atomic_write, ensure_lattice_dirs
from lattice.storage.short_ids import _default_index, save_id_index


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


@click.group()
def cli() -> None:
    """Lattice: file-based, agent-native task tracker."""


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
def init(
    target_path: str,
    actor: str | None,
    project_code: str | None,
    subproject_code: str | None,
    instance_name: str | None,
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

    try:
        # Create directory structure
        ensure_lattice_dirs(root)

        # Write default config atomically
        config: dict = dict(default_config())
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
        config_content = serialize_config(config)
        atomic_write(lattice_dir / "config.json", config_content)

        # Initialize ids.json (v2 schema) if project code is set
        if project_code:
            save_id_index(lattice_dir, _default_index())

        # Create context.md template
        context_path = lattice_dir / "context.md"
        atomic_write(context_path, _CONTEXT_MD_TEMPLATE)

    except PermissionError:
        raise click.ClickException(f"Permission denied: cannot create {LATTICE_DIR}/ in {root}")
    except OSError as e:
        raise click.ClickException(f"Failed to initialize Lattice: {e}")

    click.echo(f"Lattice initialized in {LATTICE_DIR}/ — ready to observe.")
    if actor:
        click.echo(f"Default actor: {actor}")
    if project_code:
        click.echo(f"Project code: {project_code}")
    if subproject_code:
        click.echo(f"Subproject code: {subproject_code}")
    if instance_name:
        click.echo(f"Instance name: {instance_name}")

    # CLAUDE.md integration
    _offer_claude_md(root)


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


def _offer_claude_md(root: Path) -> None:
    """Detect CLAUDE.md and offer to add Lattice integration block."""
    marker, composed_block = _compose_claude_md_blocks()

    claude_md = root / "CLAUDE.md"

    try:
        if claude_md.exists():
            content = claude_md.read_text()
            if marker in content:
                click.echo("CLAUDE.md already has Lattice integration.")
                return
            if click.confirm(
                "Found CLAUDE.md — add Lattice agent integration?",
                default=True,
            ):
                with open(claude_md, "a") as f:
                    f.write(composed_block)
                click.echo("Added Lattice integration to CLAUDE.md.")
        else:
            if click.confirm(
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
    marker, composed_block = _compose_claude_md_blocks()

    root = Path(target_path)
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

# ---------------------------------------------------------------------------
# Load CLI plugins (must be after all built-in commands are registered)
# ---------------------------------------------------------------------------
from lattice.plugins import load_cli_plugins as _load_cli_plugins  # noqa: E402

_load_cli_plugins(cli)

if __name__ == "__main__":
    cli()
