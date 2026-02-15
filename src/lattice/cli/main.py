"""CLI entry point and commands."""

from __future__ import annotations

from pathlib import Path

import click

from lattice.core.config import default_config, serialize_config, validate_project_code
from lattice.core.ids import validate_actor
from lattice.storage.fs import LATTICE_DIR, atomic_write, ensure_lattice_dirs
from lattice.storage.short_ids import save_id_index


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
def init(target_path: str, actor: str | None, project_code: str | None) -> None:
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

    try:
        # Create directory structure
        ensure_lattice_dirs(root)

        # Write default config atomically
        config: dict = dict(default_config())
        if actor:
            config["default_actor"] = actor
        if project_code:
            config["project_code"] = project_code
        config_content = serialize_config(config)
        atomic_write(lattice_dir / "config.json", config_content)

        # Initialize ids.json if project code is set
        if project_code:
            save_id_index(lattice_dir, {"schema_version": 1, "next_seq": 1, "map": {}})
    except PermissionError:
        raise click.ClickException(f"Permission denied: cannot create {LATTICE_DIR}/ in {root}")
    except OSError as e:
        raise click.ClickException(f"Failed to initialize Lattice: {e}")

    click.echo(f"Initialized empty Lattice in {LATTICE_DIR}/")
    if actor:
        click.echo(f"Default actor: {actor}")
    if project_code:
        click.echo(f"Project code: {project_code}")


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

if __name__ == "__main__":
    cli()
