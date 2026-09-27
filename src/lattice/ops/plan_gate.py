"""The plan gate: no ``in_progress`` while the plan is still the scaffold."""

from __future__ import annotations

from pathlib import Path

from lattice.core.errors import OpError
from lattice.core.plans import is_scaffold_plan


def check_plan_gate(
    lattice_dir: Path,
    task_id: str,
    target_status: str,
    config: dict,
    *,
    force: bool = False,
    reason: str | None = None,
    authoritative_snapshot: dict | None = None,
    authoritative_location: str | None = None,
) -> None:
    """Refuse a move to ``in_progress`` while the plan file is still scaffold.

    Does nothing for other targets, for workflows with no ``in_planning``
    status (the linear preset has no plan ritual), or with ``force`` and a
    reason. Raises ``OpError`` (``PLAN_REQUIRED``, ``VALIDATION_ERROR``,
    ``INTEGRITY_ERROR``) when the gate fires.
    """
    if target_status != "in_progress":
        return
    if "in_planning" not in config.get("workflow", {}).get("statuses", []):
        return
    if force:
        if not reason:
            raise OpError("VALIDATION_ERROR", "--reason is required with --force.")
        return

    if authoritative_snapshot is None:
        from lattice.storage.operations import resolve_task_prose_path

        plan_path, authority = resolve_task_prose_path(lattice_dir, task_id, "plan")
        authoritative_snapshot = authority.snapshot
    else:
        base = lattice_dir / "archive" if authoritative_location == "archived" else lattice_dir
        other_base = (
            lattice_dir if authoritative_location == "archived" else lattice_dir / "archive"
        )
        target = base / "plans" / f"{task_id}.md"
        other = other_base / "plans" / f"{task_id}.md"
        if target.exists() and other.exists() and target.read_bytes() != other.read_bytes():
            raise OpError(
                "INTEGRITY_ERROR",
                f"Plan files diverge for {task_id}; manual recovery is required.",
            )
        plan_path = target if target.exists() else other if other.exists() else None
    if plan_path is None:
        raise OpError(
            "PLAN_REQUIRED",
            f"Plan file missing for {task_id}. "
            "Write a plan before moving to in_progress. "
            "Override with --force --reason.",
        )

    try:
        content = plan_path.read_text(encoding="utf-8")
    except OSError:
        return  # Can't read → don't block (filesystem issue, not a planning issue)

    # The description distinguishes "plan is just the auto-generated
    # description" from "plan has real content".
    description = authoritative_snapshot.get("description")
    if is_scaffold_plan(content, description=description):
        raise OpError(
            "PLAN_REQUIRED",
            f"Plan for {task_id} is still scaffold. "
            "Write the plan (even one line) before moving to in_progress. "
            "Override with --force --reason.",
        )
