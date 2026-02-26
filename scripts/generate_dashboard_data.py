#!/usr/bin/env python3
"""Generate static dashboard JSON from a GitHub Project for GitHub Pages deployment.

Fetches all items from a GitHub Project via the gh CLI and produces
static JSON files matching the Lattice dashboard's API contract:
  - data/snapshot.json  (tasks list, wraps as {"ok": true, "data": [...]})
  - data/config.json    (dashboard config with workflow, statuses, project info)

Environment variables:
  GH_ORG              GitHub org or user owning the project (required)
  GH_PROJECT_NUMBER   Project number to fetch (required)
  GH_REPO             Default repo slug for issue links (optional, e.g. "org/repo")
  OAUTH_CLIENT_ID     GitHub OAuth App client ID (optional, written to config)
  OAUTH_PROXY_URL     OAuth proxy URL for token exchange (optional, written to config)
  OUTPUT_DIR          Output directory (default: data/)

Usage:
  python scripts/generate_dashboard_data.py
  GH_ORG=myorg GH_PROJECT_NUMBER=1 python scripts/generate_dashboard_data.py
"""

import json
import os
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

# ---------------------------------------------------------------------------
# Configuration from environment
# ---------------------------------------------------------------------------

GH_ORG = os.environ.get("GH_ORG", "")
GH_PROJECT_NUMBER = os.environ.get("GH_PROJECT_NUMBER", "")
GH_REPO = os.environ.get("GH_REPO", "")
OAUTH_CLIENT_ID = os.environ.get("OAUTH_CLIENT_ID", "")
OAUTH_PROXY_URL = os.environ.get("OAUTH_PROXY_URL", "")
OUTPUT_DIR = Path(os.environ.get("OUTPUT_DIR", "data"))

STATUS_MAP = {
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

PRIORITY_MAP = {
    "Critical": "critical",
    "High": "high",
    "Medium": "medium",
    "Low": "low",
    "None": "medium",
}


def fetch_github_items() -> list[dict]:
    """Fetch items from the GitHub Project via gh CLI."""
    result = subprocess.run(
        [
            "gh", "project", "item-list", GH_PROJECT_NUMBER,
            "--owner", GH_ORG,
            "--format", "json",
            "--limit", "500",
        ],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        print(f"Error fetching GitHub project: {result.stderr}", file=sys.stderr)
        sys.exit(1)

    data = json.loads(result.stdout)
    return data.get("items", data if isinstance(data, list) else [])


def map_item_to_task(item: dict, index: int) -> dict:
    """Map a GitHub Project item to the dashboard's task format."""
    content = item.get("content") or {}
    gh_status = item.get("status") or "Todo"
    assignees = item.get("assignees") or []
    labels = item.get("labels") or []
    priority = item.get("priority") or "None"

    issue_number = content.get("number")
    issue_url = content.get("url", "")
    title = (item.get("title") or content.get("title") or "(untitled)").strip()
    body = (content.get("body") or "").strip()
    created_at = content.get("createdAt") or content.get("created_at") or ""
    updated_at = content.get("updatedAt") or content.get("updated_at") or ""

    lattice_status = STATUS_MAP.get(gh_status, "backlog")
    lattice_priority = PRIORITY_MAP.get(priority, "medium")

    # Build description
    desc_parts = []
    if issue_url and issue_number:
        repo = content.get("repository", GH_REPO or GH_ORG)
        desc_parts.append(f"GitHub Issue: [{repo}#{issue_number}]({issue_url})")
    if body:
        desc_parts.append("")
        desc_parts.append(body)

    # Use the GitHub Project item ID as a stable identifier
    item_id = item.get("id", f"gh_{index}")

    task = {
        "id": item_id,
        "short_id": f"STKS-{issue_number}" if issue_number else f"STKS-{index + 1}",
        "title": title,
        "status": lattice_status,
        "priority": lattice_priority,
        "type": "task",
        "assigned_to": f"human:{assignees[0]}" if assignees else None,
        "tags": [label.strip().lower().replace(" ", "-") for label in labels if label.strip()] or None,
        "created_at": created_at,
        "updated_at": updated_at or created_at,
        "done_at": updated_at if lattice_status == "done" else None,
        "last_status_changed_at": updated_at or created_at,
        "comment_count": content.get("comments", {}).get("totalCount", 0) if isinstance(content.get("comments"), dict) else 0,
        "reopened_count": 0,
        "relationships_out_count": 0,
        "evidence_ref_count": 0,
        "branch_link_count": 0,
        "has_active_session": lattice_status == "in_progress" and bool(assignees),
        "description": "\n".join(desc_parts) if desc_parts else None,
        "custom_fields": {
            "github_url": issue_url,
            "github_issue_number": str(issue_number) if issue_number else "",
            "github_project_item_id": item_id,
        },
    }
    return task


def collect_statuses(tasks: list[dict]) -> list[str]:
    """Derive the ordered status list from actual task data."""
    order = ["backlog", "in_progress", "review", "done", "cancelled", "blocked"]
    seen = {t["status"] for t in tasks}
    result = [s for s in order if s in seen]
    for s in sorted(seen):
        if s not in result:
            result.append(s)
    return result


def build_config(tasks: list[dict]) -> dict:
    """Build a dashboard-compatible config for GitHub Pages mode."""
    statuses = collect_statuses(tasks)

    # Default workflow transitions
    transitions = {}
    for s in statuses:
        transitions[s] = [other for other in statuses if other != s]

    config = {
        "mode": "github",
        "project_code": "STKS",
        "instance_name": f"{GH_ORG} Project #{GH_PROJECT_NUMBER}",
        "default_status": "backlog",
        "default_priority": "medium",
        "workflow": {
            "statuses": statuses,
            "transitions": transitions,
        },
        "task_types": ["task", "bug", "feature", "chore"],
        "github": {
            "org": GH_ORG,
            "project_number": int(GH_PROJECT_NUMBER) if GH_PROJECT_NUMBER else 0,
            "repo": GH_REPO,
        },
        "dashboard": {
            "theme": "carbon",
        },
    }

    if OAUTH_CLIENT_ID:
        config["github"]["oauth_client_id"] = OAUTH_CLIENT_ID
    if OAUTH_PROXY_URL:
        config["github"]["oauth_proxy_url"] = OAUTH_PROXY_URL

    return config


def main() -> None:
    if not GH_ORG or not GH_PROJECT_NUMBER:
        print("Error: GH_ORG and GH_PROJECT_NUMBER env vars are required.", file=sys.stderr)
        sys.exit(1)

    print(f"Fetching {GH_ORG} project #{GH_PROJECT_NUMBER}...")
    items = fetch_github_items()
    print(f"Found {len(items)} items.")

    tasks = [map_item_to_task(item, i) for i, item in enumerate(items)]

    # Sort by issue number where available
    tasks.sort(key=lambda t: int(t.get("custom_fields", {}).get("github_issue_number") or "99999"))

    snapshot = {"ok": True, "data": tasks}
    config = build_config(tasks)

    # Add generation metadata
    config["_generated_at"] = datetime.now(timezone.utc).isoformat()
    config["_item_count"] = len(tasks)

    # Write output
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    snapshot_path = OUTPUT_DIR / "snapshot.json"
    snapshot_path.write_text(json.dumps(snapshot, indent=2, sort_keys=True) + "\n")
    print(f"Wrote {snapshot_path} ({len(tasks)} tasks)")

    config_path = OUTPUT_DIR / "config.json"
    config_path.write_text(json.dumps(config, indent=2, sort_keys=True) + "\n")
    print(f"Wrote {config_path}")

    # Also write a graph stub (empty, since GH Projects don't have relationships)
    graph_path = OUTPUT_DIR / "graph.json"
    graph_path.write_text(json.dumps({"ok": True, "data": {"links": []}}, indent=2) + "\n")

    print("Done.")


if __name__ == "__main__":
    main()
