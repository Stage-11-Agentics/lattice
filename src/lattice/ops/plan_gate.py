"""The plan gate: no ``in_progress`` while the plan is still the scaffold."""

from __future__ import annotations

import json
import shlex
from pathlib import Path

from lattice.core.errors import OpError
from lattice.core.plans import is_scaffold_plan
from lattice.storage.operations import AuthoritativeLogError


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
    _check_plan_path(
        lattice_dir,
        task_id,
        authoritative_snapshot,
        plan_path,
        claim=False,
    )


def read_plan_path_for_mutation(
    lattice_dir: Path,
    task_id: str,
    location: str | None,
) -> Path | None:
    """Read the authoritative plan location without reacquiring its task lock.

    Call only inside a task mutation callback, which already holds the task
    lock. A byte-divergent active/archive pair keeps the exact integrity error
    used by ``resolve_task_prose_path``.
    """
    active = lattice_dir / "plans" / f"{task_id}.md"
    archived = lattice_dir / "archive" / "plans" / f"{task_id}.md"
    target, other = (archived, active) if location == "archived" else (active, archived)
    if target.exists() and other.exists() and target.read_bytes() != other.read_bytes():
        raise AuthoritativeLogError(
            "active and archived plan files diverge; manual recovery required",
            path=other,
        )
    if target.exists():
        return target
    if other.exists():
        return other
    return None


def check_claim_plan_gate(
    lattice_dir: Path,
    task_id: str,
    snapshot: dict,
    plan_path: Path | None,
    config: dict,
) -> None:
    """Apply the existing plan gate using the plan path read inside a claim."""
    if "in_planning" not in config.get("workflow", {}).get("statuses", []):
        return
    _check_plan_path(lattice_dir, task_id, snapshot, plan_path, claim=True)


def _check_plan_path(
    lattice_dir: Path,
    task_id: str,
    snapshot: dict,
    plan_path: Path | None,
    *,
    claim: bool,
) -> None:
    if plan_path is None:
        raise OpError.task_state(
            "PLAN_REQUIRED",
            _plan_required_message(task_id, lattice_dir, snapshot, scaffold=False, claim=claim),
            snapshot,
        )

    try:
        content = plan_path.read_text(encoding="utf-8")
    except OSError:
        return  # Can't read → don't block (filesystem issue, not a planning issue)

    # The description distinguishes "plan is just the auto-generated
    # description" from "plan has real content".
    description = snapshot.get("description")
    if is_scaffold_plan(content, description=description):
        raise OpError.task_state(
            "PLAN_REQUIRED",
            _plan_required_message(task_id, lattice_dir, snapshot, scaffold=True, claim=claim),
            snapshot,
        )


def _plan_required_message(
    task_id: str,
    lattice_dir: Path,
    snapshot: dict,
    *,
    scaffold: bool,
    claim: bool,
) -> str:
    if scaffold:
        message = (
            f"Plan for {task_id} is still scaffold. "
            "Write the plan (even one line) before moving to in_progress. "
            "Override with --force --reason."
        )
    else:
        message = (
            f"Plan file missing for {task_id}. "
            "Write a plan before moving to in_progress. "
            "Override with --force --reason."
        )
    message += _hosted_hint(lattice_dir, snapshot)
    if claim:
        message += " No assignment or status change was made."
    return message


def _hosted_hint(
    lattice_dir: Path,
    snapshot: dict | None,
    *,
    config: dict | None = None,
    task_type: object = None,
) -> str:
    """Return an actionable hint only for a server-owned board.

    The default is the plan-write hint (SPEC §3.9). When ``config`` is given,
    format the task-type admin command; the hosted project slug is the name of
    the server-owned project directory. Empty for a local board.
    """
    if not (lattice_dir / "hosted" / "owner.json").exists():
        return ""
    if config is not None:
        configured = list(config.get("task_types", []))
        replacement = list(configured)
        if isinstance(task_type, str) and task_type not in replacement:
            replacement.append(task_type)
        assignment = shlex.quote("task_types=" + json.dumps(replacement, separators=(",", ":")))
        slug = lattice_dir.parent.name
        return (
            "On a hosted board, ask an admin on the server host to run "
            f"`lattice server project config {slug} --set {assignment}`; "
            "this replaces the list, so include the existing values when adding a type."
        )
    task = (snapshot or {}).get("short_id") or (snapshot or {}).get("id") or "<task>"
    return f" Write the plan with `lattice plan write {task} --file <path>`."
