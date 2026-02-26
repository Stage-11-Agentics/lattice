"""Import GitHub Project items into Lattice: lattice import-github-project."""

from __future__ import annotations

import json as json_mod
import shutil
import subprocess
from pathlib import Path

import click

from lattice.cli.helpers import (
    common_options,
    load_project_config,
    output_error,
    output_result,
    require_actor,
    require_root,
)
from lattice.cli.main import cli
from lattice.core.config import validate_status
from lattice.core.events import create_event
from lattice.core.ids import generate_task_id
from lattice.core.tasks import apply_event_to_snapshot
from lattice.storage.operations import scaffold_plan, write_task_event
from lattice.storage.short_ids import allocate_short_id

DEFAULT_STATUS_MAP: dict[str, str] = {
    "Backlog": "backlog",
    "Todo": "backlog",
    "In Progress": "in_progress",
    "In Review": "review",
    "Needs review": "review",
    "Review": "review",
    "Done": "done",
    "Cancelled": "cancelled",
    "Blocked": "blocked",
}


def _find_existing_gh_item_ids(lattice_dir: Path) -> set[str]:
    """Scan existing tasks for github_project_item_id custom field."""
    existing: set[str] = set()
    tasks_dir = lattice_dir / "tasks"
    if not tasks_dir.is_dir():
        return existing
    for snap_path in tasks_dir.glob("*.json"):
        try:
            snap = json_mod.loads(snap_path.read_text())
            item_id = (snap.get("custom_fields") or {}).get("github_project_item_id")
            if item_id:
                existing.add(item_id)
        except (json_mod.JSONDecodeError, OSError):
            continue
    return existing


def _fetch_github_items(
    gh_bin: str, org: str, project_number: int, *, use_cache: bool, is_json: bool
) -> list[dict]:
    """Fetch items from a GitHub Project via the gh CLI."""
    cache_path = Path("/tmp/lattice-gh-import-cache.json")

    if use_cache and cache_path.exists():
        raw = json_mod.loads(cache_path.read_text())
        return raw.get("items", raw if isinstance(raw, list) else [])

    result = subprocess.run(
        [
            gh_bin,
            "project",
            "item-list",
            str(project_number),
            "--owner",
            org,
            "--format",
            "json",
            "--limit",
            "500",
        ],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        output_error(
            f"gh project item-list failed: {result.stderr.strip()}",
            "GH_ERROR",
            is_json,
        )

    try:
        raw = json_mod.loads(result.stdout)
    except json_mod.JSONDecodeError:
        output_error(
            f"Failed to parse gh output as JSON: {result.stdout[:200]}",
            "GH_PARSE_ERROR",
            is_json,
        )

    if use_cache:
        cache_path.write_text(json_mod.dumps(raw, indent=2))

    return raw.get("items", raw if isinstance(raw, list) else [])


def _build_description(item: dict, org: str) -> str:
    """Build a Lattice task description from a GitHub Project item."""
    content = item.get("content") or {}
    url = content.get("url", "")
    number = content.get("number")
    body = (content.get("body") or "").strip()
    repo = content.get("repository", "")

    parts = []
    if url and number:
        label = f"{repo or org}#{number}" if repo else f"{org}#{number}"
        parts.append(f"GitHub Issue: [{label}]({url})")
    elif url:
        parts.append(f"GitHub: {url}")

    if body:
        parts.append("")
        parts.append(body)

    return "\n".join(parts)


@cli.command("import-github-project")
@click.argument("org")
@click.argument("project_number", type=int)
@click.option(
    "--status-map",
    default=None,
    help='JSON overrides for GitHub→Lattice status mapping, e.g. \'{"In Progress":"in_progress"}\'.',
)
@click.option(
    "--default-status",
    default=None,
    help="Fallback Lattice status for unmapped GitHub statuses. Defaults to project default_status.",
)
@click.option("--dry-run", is_flag=True, help="Print what would be imported without writing.")
@click.option("--cache", is_flag=True, help="Cache gh output to /tmp to avoid repeated API calls.")
@common_options
def import_github_project(
    org: str,
    project_number: int,
    status_map: str | None,
    default_status: str | None,
    dry_run: bool,
    cache: bool,
    model: str | None,
    session: str | None,
    output_json: bool,
    quiet: bool,
    triggered_by: str | None,
    on_behalf_of: str | None,
    provenance_reason: str | None,
) -> None:
    """Import items from a GitHub Project into Lattice tasks.

    Fetches all items from the specified GitHub Project (using the gh CLI)
    and creates corresponding Lattice tasks with mapped statuses and
    custom fields for traceability.

    Idempotent: items already imported (by github_project_item_id) are skipped.

    \b
    Examples:
        lattice import-github-project myorg 1 --actor human:ron
        lattice import-github-project myorg 1 --dry-run --actor human:ron
        lattice import-github-project myorg 1 --status-map '{"Todo":"backlog"}' --actor human:ron
    """
    is_json = output_json

    # 1. Check gh CLI
    gh_bin = shutil.which("gh")
    if gh_bin is None:
        output_error(
            "The 'gh' CLI is required. Install from https://cli.github.com",
            "GH_NOT_FOUND",
            is_json,
        )

    # 2. Resolve Lattice root and config
    lattice_dir = require_root(is_json)
    config = load_project_config(lattice_dir)

    # 3. Resolve actor
    actor = require_actor(is_json)

    # 4. Parse status mapping
    parsed_map = dict(DEFAULT_STATUS_MAP)
    if status_map:
        try:
            parsed_map.update(json_mod.loads(status_map))
        except json_mod.JSONDecodeError as e:
            output_error(f"Invalid --status-map JSON: {e}", "VALIDATION_ERROR", is_json)

    fallback_status = default_status or config.get("default_status", "backlog")

    # 5. Fetch GitHub Project items
    if not quiet:
        click.echo(f"Fetching items from {org} project #{project_number}...")

    items = _fetch_github_items(gh_bin, org, project_number, use_cache=cache, is_json=is_json)

    if not quiet:
        click.echo(f"Found {len(items)} item(s).")

    # 6. Idempotency: find already-imported items
    existing_item_ids = _find_existing_gh_item_ids(lattice_dir) if not dry_run else set()

    if not quiet and existing_item_ids:
        click.echo(f"Already imported: {len(existing_item_ids)} item(s).")

    # 7. Import loop
    imported = 0
    skipped = 0
    errors: list[dict] = []

    project_code = config.get("project_code")
    subproject_code = config.get("subproject_code")

    for item in items:
        item_id = item.get("id", "")
        title = (item.get("title") or item.get("content", {}).get("title") or "(untitled)").strip()
        gh_status = item.get("status") or ""
        content = item.get("content") or {}
        issue_number = content.get("number")
        issue_url = content.get("url", "")
        assignees = item.get("assignees") or []
        labels = item.get("labels") or []

        # Idempotency check
        if item_id and item_id in existing_item_ids:
            skipped += 1
            if not quiet and not is_json:
                click.echo(f"  [skip] #{issue_number or '?'}: {title}")
            continue

        # Map status
        lattice_status = parsed_map.get(gh_status, fallback_status)
        if not validate_status(config, lattice_status):
            lattice_status = fallback_status

        if dry_run:
            imported += 1
            if not quiet and not is_json:
                click.echo(f"  [dry-run] #{issue_number or '?'}: {title} → {lattice_status}")
            continue

        # Generate IDs
        task_id = generate_task_id()
        short_id = None
        if project_code:
            prefix = f"{project_code}-{subproject_code}" if subproject_code else project_code
            short_id, _ = allocate_short_id(lattice_dir, prefix, task_ulid=task_id)

        # Build custom fields
        custom_fields: dict = {}
        if item_id:
            custom_fields["github_project_item_id"] = item_id
        if issue_number is not None:
            custom_fields["github_issue_number"] = str(issue_number)
        if issue_url:
            custom_fields["github_url"] = issue_url

        # Build description
        description = _build_description(item, org)

        # Build tags from labels
        tags = [label.strip().lower().replace(" ", "-") for label in labels if label.strip()]

        # Build assigned_to from first assignee
        assigned_to = f"human:{assignees[0]}" if assignees else None

        # Build event data
        event_data: dict = {
            "title": title,
            "status": lattice_status,
            "type": "task",
            "priority": config.get("default_priority", "medium"),
            "custom_fields": custom_fields,
        }
        if short_id:
            event_data["short_id"] = short_id
        if description:
            event_data["description"] = description
        if tags:
            event_data["tags"] = tags
        if assigned_to:
            event_data["assigned_to"] = assigned_to

        try:
            event = create_event(
                type="task_created",
                task_id=task_id,
                actor=actor,
                data=event_data,
                model=model,
                session=session,
                triggered_by=triggered_by,
                on_behalf_of=on_behalf_of,
                reason=provenance_reason,
            )
            snapshot = apply_event_to_snapshot(None, event)
            write_task_event(lattice_dir, task_id, [event], snapshot, config)
            scaffold_plan(lattice_dir, task_id, title, short_id, description or None)
            imported += 1

            if not quiet and not is_json:
                click.echo(f"  [ok] #{issue_number or '?'}: {title} → {short_id or task_id[:12]}")
        except Exception as exc:
            errors.append({"title": title, "issue_number": issue_number, "error": str(exc)})
            if not quiet and not is_json:
                click.echo(f"  [error] #{issue_number or '?'}: {title} — {exc}")

    # 8. Output summary
    summary = {
        "imported": imported,
        "skipped": skipped,
        "errors": len(errors),
        "total": len(items),
        "dry_run": dry_run,
    }
    if errors:
        summary["error_details"] = errors

    if dry_run:
        human_msg = f"Dry run: would import {imported} item(s), {skipped} already synced."
    else:
        human_msg = f"Imported {imported} task(s), skipped {skipped}, {len(errors)} error(s)."

    output_result(
        data=summary,
        human_message=human_msg,
        quiet_value=str(imported),
        is_json=is_json,
        is_quiet=quiet,
    )

    if errors and not is_json:
        raise SystemExit(1)
