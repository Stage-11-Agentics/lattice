#!/usr/bin/env python3
"""Export Lattice relationship links to dashboard-compatible graph JSON.

Reads .lattice/tasks/*.json, extracts relationships_out, and writes:
  - data/graph.json   (nodes + links for cube/web views)
  - Updates data/snapshot.json with correct relationships_out_count

The key challenge is ID mapping: .lattice/ uses Lattice task IDs while
data/snapshot.json uses GitHub Project item IDs. We bridge via the
custom_fields.github_project_item_id stored in both systems.

Usage:
  python scripts/export_lattice_graph.py
  python scripts/export_lattice_graph.py --lattice-dir .lattice --output-dir data
"""

import json
import os
import sys
from pathlib import Path

LATTICE_DIR = Path(os.environ.get("LATTICE_DIR", ".lattice"))
OUTPUT_DIR = Path(os.environ.get("OUTPUT_DIR", "data"))


def main() -> None:
    tasks_dir = LATTICE_DIR / "tasks"
    if not tasks_dir.is_dir():
        print(f"Error: {tasks_dir} not found", file=sys.stderr)
        sys.exit(1)

    # 1. Read all Lattice task snapshots
    lattice_tasks: list[dict] = []
    for f in sorted(tasks_dir.glob("*.json")):
        try:
            lattice_tasks.append(json.loads(f.read_text()))
        except (json.JSONDecodeError, OSError) as e:
            print(f"Warning: skipping {f.name}: {e}", file=sys.stderr)

    print(f"Read {len(lattice_tasks)} tasks from {tasks_dir}")

    # 2. Build ID mappings:
    #    lattice_id -> gh_item_id (for translating link source/target)
    #    lattice_id -> task data (for building nodes)
    lattice_to_gh: dict[str, str] = {}
    lattice_by_id: dict[str, dict] = {}

    for t in lattice_tasks:
        tid = t["id"]
        lattice_by_id[tid] = t
        gh_item_id = (t.get("custom_fields") or {}).get("github_project_item_id")
        if gh_item_id:
            lattice_to_gh[tid] = gh_item_id

    # 3. Extract all links, translating IDs to GitHub item IDs
    links: list[dict] = []
    link_counts: dict[str, int] = {}  # gh_item_id -> outgoing link count

    for t in lattice_tasks:
        source_lattice_id = t["id"]
        source_gh_id = lattice_to_gh.get(source_lattice_id)
        if not source_gh_id:
            continue

        rels = t.get("relationships_out") or []
        count = 0
        for rel in rels:
            target_lattice_id = rel.get("target_task_id")
            target_gh_id = lattice_to_gh.get(target_lattice_id)
            if not target_gh_id:
                continue  # target not in our set

            links.append({
                "source": source_gh_id,
                "target": target_gh_id,
                "type": rel.get("type", "related_to"),
            })
            count += 1

        if count > 0:
            link_counts[source_gh_id] = count

    print(f"Exported {len(links)} links")

    # 4. Build nodes array for graph.json
    nodes: list[dict] = []
    for t in lattice_tasks:
        gh_id = lattice_to_gh.get(t["id"])
        if not gh_id:
            continue
        nodes.append({
            "id": gh_id,
            "short_id": t.get("short_id", ""),
            "title": t.get("title", ""),
            "status": t.get("status", "backlog"),
            "priority": t.get("priority", "medium"),
            "type": t.get("type", "task"),
            "assigned_to": t.get("assigned_to"),
            "branch_links": [],
            "created_at": t.get("created_at", ""),
            "updated_at": t.get("updated_at", ""),
            "description_snippet": (t.get("description") or "")[:200],
        })

    # 5. Write graph.json
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    graph = {
        "ok": True,
        "data": {
            "nodes": nodes,
            "links": links,
        },
    }
    graph_path = OUTPUT_DIR / "graph.json"
    graph_path.write_text(json.dumps(graph, indent=2, sort_keys=True) + "\n")
    print(f"Wrote {graph_path} ({len(nodes)} nodes, {len(links)} links)")

    # 6. Update snapshot.json with relationship counts
    snapshot_path = OUTPUT_DIR / "snapshot.json"
    if snapshot_path.exists():
        snap = json.loads(snapshot_path.read_text())
        tasks_list = snap.get("data", snap if isinstance(snap, list) else [])
        updated = 0
        for task in tasks_list:
            task_id = task.get("id", "")
            if task_id in link_counts:
                task["relationships_out_count"] = link_counts[task_id]
                updated += 1
        snap_text = json.dumps(snap, indent=2, sort_keys=True) + "\n"
        snapshot_path.write_text(snap_text)
        print(f"Updated {updated} tasks in {snapshot_path} with relationship counts")
    else:
        print(f"Warning: {snapshot_path} not found, skipping count update")

    # Summary by type
    type_counts: dict[str, int] = {}
    for link in links:
        lt = link["type"]
        type_counts[lt] = type_counts.get(lt, 0) + 1
    for lt, count in sorted(type_counts.items()):
        print(f"  {lt}: {count}")


if __name__ == "__main__":
    main()
