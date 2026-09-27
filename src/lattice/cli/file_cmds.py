"""File-link and explain commands: file-link, file-unlink, explain."""

from __future__ import annotations

import re
from pathlib import Path

import click

from lattice.cli.helpers import (
    common_options,
    json_envelope,
    output_error,
    output_result,
    require_root,
)
from lattice.cli.main import cli
from lattice.cli.ops_bridge import provenance_params, run_operation
from lattice.core.stats import load_all_snapshots
from lattice.ops.task_file_link import resolve_to_relative
from lattice.storage.readers import read_task_events


# ---------------------------------------------------------------------------
# lattice file-link
# ---------------------------------------------------------------------------


@cli.command("file-link")
@click.argument("task_id")
@click.argument("filepaths", nargs=-1, required=True)
@common_options
def file_link(
    task_id: str,
    filepaths: tuple[str, ...],
    model: str | None,
    session: str | None,
    output_json: bool,
    quiet: bool,
    triggered_by: str | None,
    on_behalf_of: str | None,
    provenance_reason: str | None,
) -> None:
    """Link file(s) to a task to record decision provenance."""
    is_json = output_json
    result = run_operation(
        "task.file_link",
        {
            "task": task_id,
            "filepaths": list(filepaths),
            **provenance_params(model, session, triggered_by, on_behalf_of, provenance_reason),
        },
        is_json,
    )
    task_id = result.value["id"]
    paths = result.events[-1]["data"]["paths"]
    output_result(
        data=result.value,
        human_message=f"Linked {len(paths)} file(s) to {task_id}: {', '.join(paths)}",
        quiet_value=task_id,
        is_json=is_json,
        is_quiet=quiet,
    )


# ---------------------------------------------------------------------------
# lattice file-unlink
# ---------------------------------------------------------------------------


@cli.command("file-unlink")
@click.argument("task_id")
@click.argument("filepaths", nargs=-1, required=True)
@common_options
def file_unlink(
    task_id: str,
    filepaths: tuple[str, ...],
    model: str | None,
    session: str | None,
    output_json: bool,
    quiet: bool,
    triggered_by: str | None,
    on_behalf_of: str | None,
    provenance_reason: str | None,
) -> None:
    """Unlink file(s) from a task."""
    is_json = output_json
    result = run_operation(
        "task.file_unlink",
        {
            "task": task_id,
            "filepaths": list(filepaths),
            **provenance_params(model, session, triggered_by, on_behalf_of, provenance_reason),
        },
        is_json,
    )
    task_id = result.value["id"]
    paths = result.events[-1]["data"]["paths"]
    output_result(
        data=result.value,
        human_message=f"Unlinked {len(paths)} file(s) from {task_id}: {', '.join(paths)}",
        quiet_value=task_id,
        is_json=is_json,
        is_quiet=quiet,
    )


# ---------------------------------------------------------------------------
# lattice explain
# ---------------------------------------------------------------------------


def _get_decision_comments(
    lattice_dir: Path,
    task_id: str,
    *,
    is_archived: bool = False,
) -> list[str]:
    """Return body texts of comments with role 'decision' for a task."""
    events = read_task_events(lattice_dir, task_id, is_archived=is_archived)
    bodies: list[str] = []
    for ev in events:
        if ev.get("type") == "comment_added" and ev.get("data", {}).get("role") == "decision":
            body = ev.get("data", {}).get("body", "").strip()
            if body:
                bodies.append(body)
    return bodies


def _parse_decisions_md(lattice_dir: Path, filepath: str) -> list[dict]:
    """Search for matching entries in Decisions.md.

    Looks for entries that reference the filepath in a Files: line.
    Returns a list of decision dicts with title, decision, rationale,
    consequence, and files.
    """
    project_root = lattice_dir.parent
    decisions_path = project_root / "Decisions.md"
    if not decisions_path.exists():
        return []

    try:
        content = decisions_path.read_text()
    except OSError:
        return []

    # Split into entries by ## headings
    entries: list[dict] = []
    current_entry: dict | None = None

    for line in content.splitlines():
        if line.startswith("## "):
            if current_entry is not None:
                entries.append(current_entry)
            current_entry = {"heading": line[3:].strip(), "lines": []}
        elif current_entry is not None:
            current_entry["lines"].append(line)

    if current_entry is not None:
        entries.append(current_entry)

    # Filter entries that reference our filepath (exact match)
    files_line_re = re.compile(r"(?i)^-?\s*Files?:\s*(.*)$", re.MULTILINE)
    matches: list[dict] = []
    for entry in entries:
        body = "\n".join(entry["lines"])
        # Parse Files: lines and compare each path exactly
        for m in files_line_re.finditer(body):
            paths = [p.strip() for p in m.group(1).split(",")]
            if filepath in paths:
                matches.append(
                    {
                        "source": "Decisions.md",
                        "heading": entry["heading"],
                        "body": body.strip(),
                    }
                )
                break

    return matches


@cli.command("explain")
@click.argument("filepath")
@click.option("--json", "output_json", is_flag=True, help="Output structured JSON.")
@click.option("--verbose", is_flag=True, help="Include full comments and plan content.")
def explain_cmd(
    filepath: str,
    output_json: bool,
    verbose: bool,
) -> None:
    """Show decisions and context behind a file.

    Scans all tasks for file links matching FILEPATH, and also checks
    Decisions.md for entries referencing this file.
    """
    is_json = output_json

    lattice_dir = require_root(is_json)

    # Resolve to project-relative path
    try:
        rel_path = resolve_to_relative(lattice_dir, filepath)
    except ValueError as exc:
        output_error(str(exc), "VALIDATION_ERROR", is_json)

    # Scan all task snapshots for linked_files containing this path
    active, archived = load_all_snapshots(lattice_dir)
    archived_ids = {s.get("id") for s in archived}
    all_snapshots = active + archived

    matching_tasks: list[dict] = []
    for snap in all_snapshots:
        linked = snap.get("linked_files", [])
        if rel_path in linked:
            task_id = snap.get("id")
            is_archived = task_id in archived_ids
            entry: dict = {
                "task_id": task_id,
                "short_id": snap.get("short_id"),
                "title": snap.get("title"),
                "status": snap.get("status"),
                "description": snap.get("description"),
                "created_at": snap.get("created_at"),
            }

            # Collect decision-role comments from event log
            decision_comments = _get_decision_comments(
                lattice_dir,
                task_id,
                is_archived=is_archived,
            )
            if decision_comments:
                entry["decision_comments"] = decision_comments

            # Include decision-role comments if verbose or JSON
            if verbose or is_json:
                evidence_refs = snap.get("evidence_refs", [])
                decision_refs = [r for r in evidence_refs if r.get("role") == "decision"]
                entry["decision_evidence"] = decision_refs

                # Check for plan file
                plan_path = lattice_dir / "plans" / f"{task_id}.md"
                if not plan_path.exists():
                    plan_path = lattice_dir / "archive" / "plans" / f"{task_id}.md"
                if plan_path.exists():
                    try:
                        plan_content = plan_path.read_text()
                        if verbose:
                            entry["plan"] = plan_content
                        else:
                            # First 5 non-empty lines
                            lines = [ln for ln in plan_content.splitlines() if ln.strip()][:5]
                            entry["plan_excerpt"] = "\n".join(lines)
                    except OSError:
                        pass

            matching_tasks.append(entry)

    # Sort by creation date (oldest first)
    matching_tasks.sort(key=lambda t: t.get("created_at", ""))

    # Check Decisions.md
    decisions_md_matches = _parse_decisions_md(lattice_dir, rel_path)

    if is_json:
        data = {
            "filepath": rel_path,
            "tasks": matching_tasks,
            "decisions_md": decisions_md_matches,
        }
        click.echo(json_envelope(True, data=data))
    else:
        if not matching_tasks and not decisions_md_matches:
            click.echo(f"No decisions found for: {rel_path}")
            return

        click.echo(f"Decisions for: {rel_path}")
        click.echo("")

        for task in matching_tasks:
            short_id = task.get("short_id") or task.get("task_id", "?")
            title = task.get("title", "?")
            status = task.get("status", "?")
            click.echo(f'  {short_id}  "{title}"  [{status}]')

            desc = task.get("description")
            if desc:
                # Show first 3 lines of description
                desc_lines = desc.strip().splitlines()[:3]
                for line in desc_lines:
                    click.echo(f"    {line}")

            # Show decision-role comments (always, not just verbose)
            for comment_body in task.get("decision_comments", []):
                click.echo(f"    Decision: {comment_body}")

            if verbose:
                plan = task.get("plan")
                if plan:
                    click.echo("    Plan:")
                    for line in plan.strip().splitlines()[:10]:
                        click.echo(f"      {line}")

            click.echo("")

        if decisions_md_matches:
            click.echo("  From Decisions.md:")
            for match in decisions_md_matches:
                click.echo(f"    {match['heading']}")
                if verbose:
                    for line in match["body"].splitlines()[:5]:
                        click.echo(f"      {line}")
                click.echo("")
