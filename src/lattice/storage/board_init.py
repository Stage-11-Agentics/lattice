"""Writing a new board: the files ``lattice init`` creates.

``lattice init`` (after its interview) and ``lattice server project create``
both call :func:`create_board`, so a server project is exactly the board
``init`` would write with the same options.
"""

from __future__ import annotations

from pathlib import Path

from lattice.core.config import WORKFLOW_PRESETS, default_config, serialize_config
from lattice.core.ids import generate_instance_id
from lattice.storage.fs import LATTICE_DIR, atomic_write, ensure_lattice_dirs
from lattice.storage.short_ids import _default_index, save_id_index

CONTEXT_MD_TEMPLATE = """\
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


def create_board(
    root: Path,
    *,
    workflow_preset: str = "classic",
    status_preset: str = "stage11",
    custom_workflow: dict | None = None,
    actor: str | None = None,
    project_code: str | None = None,
    subproject_code: str | None = None,
    instance_name: str | None = None,
    project_name: str | None = None,
    model: str | None = None,
    heartbeat: bool | None = None,
    review_mode: str | None = None,
    plan_review_mode: str | None = None,
    plan_approval: str | None = None,
    done_display: str | None = None,
    project_type: str | None = None,
    project_description: str | None = None,
) -> dict:
    """Create ``root/.lattice/`` with its directories, ``config.json``, ``ids.json``
    (when a project code is set), and ``context.md``; return the config written.

    Codes arrive validated and uppercased; the caller owns the interview.
    """
    lattice_dir = Path(root) / LATTICE_DIR
    # Create directory structure
    ensure_lattice_dirs(root)

    # Write default config atomically
    config: dict = dict(default_config(preset=workflow_preset, status_preset=status_preset))
    if custom_workflow is not None:
        # Personality display names apply only to statuses that exist.
        display_names = {
            slug: name
            for slug, name in WORKFLOW_PRESETS[workflow_preset]["display_names"].items()
            if slug in custom_workflow["statuses"]
        }
        if display_names:
            custom_workflow["display_names"] = display_names
        config["workflow"] = custom_workflow
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
    if project_name:
        config["project_name"] = project_name
    if model:
        config["model"] = model
    if heartbeat:
        config["heartbeat"] = {"enabled": True, "max_advances": 10}
    if review_mode:
        config["review_mode"] = review_mode
    if plan_review_mode:
        config["plan_review_mode"] = plan_review_mode
    if plan_approval:
        config["plan_approval"] = plan_approval
    if done_display:
        config["done_display"] = done_display
    # Only persist project_type when explicitly non-default so existing
    # standard projects serialize identically to before this field existed.
    if project_type and project_type.lower() != "standard":
        config["project_type"] = project_type.lower()
    config_content = serialize_config(config)
    atomic_write(lattice_dir / "config.json", config_content)

    # Initialize ids.json (v2 schema) if project code is set
    if project_code:
        save_id_index(lattice_dir, _default_index())

    # Create context.md — with project description if provided, otherwise template
    context_path = lattice_dir / "context.md"
    display_name = project_name or instance_name or root.name
    if project_description:
        context_content = (
            f"# {display_name}\n\n"
            f"## Purpose\n\n{project_description}\n\n"
            "## Related Instances\n\n"
            "<!-- Other lattice instances this node coordinates with. -->\n\n"
            "## Conventions\n\n"
            "<!-- Instance-specific workflow rhythms and naming patterns. -->\n"
        )
        atomic_write(context_path, context_content)
    elif project_name:
        # Use project name for heading even without a description
        named_template = CONTEXT_MD_TEMPLATE.replace(
            "# Instance Context",
            f"# {project_name}",
        )
        atomic_write(context_path, named_template)
    else:
        atomic_write(context_path, CONTEXT_MD_TEMPLATE)
    return config
