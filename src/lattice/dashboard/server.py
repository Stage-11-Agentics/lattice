"""HTTP server for the Lattice dashboard."""

from __future__ import annotations

import json
import platform
import shutil
import subprocess
import sys
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

from lattice.core.comments import (
    materialize_comments,
    validate_comment_body,
    validate_comment_for_delete,
    validate_comment_for_edit,
    validate_comment_for_react,
    validate_comment_for_reply,
    validate_emoji,
)
from lattice.core.config import (
    VALID_PRIORITIES,
    VALID_URGENCIES,
    serialize_config,
    validate_status,
    validate_task_type,
    validate_transition,
)
from lattice.core.events import create_event, serialize_event, utc_now
from lattice.core.ids import generate_task_id, validate_actor, validate_id
from lattice.core.tasks import (
    apply_event_to_snapshot,
    compact_snapshot,
    compute_epic_derived_status,
    serialize_snapshot,
)
from lattice.storage.fs import atomic_write, jsonl_append
from lattice.storage.locks import multi_lock
from lattice.storage.hooks import execute_hooks
from lattice.storage.operations import scaffold_plan, write_task_event
from lattice.storage.readers import read_task_events
from lattice.storage.short_ids import allocate_short_id

STATIC_DIR = Path(__file__).parent / "static"

# Maximum allowed request body size (1 MiB) to prevent DoS via oversized payloads.
MAX_REQUEST_BODY_BYTES = 1_048_576

# ---------------------------------------------------------------------------
# JSON envelope helpers
# ---------------------------------------------------------------------------


def _ok(data: Any) -> str:
    return json.dumps({"ok": True, "data": data}, sort_keys=True, indent=2) + "\n"


def _err(code: str, message: str) -> str:
    return (
        json.dumps(
            {"ok": False, "error": {"code": code, "message": message}},
            sort_keys=True,
            indent=2,
        )
        + "\n"
    )


# ---------------------------------------------------------------------------
# Request handler
# ---------------------------------------------------------------------------


def _make_handler_class(lattice_dir: Path, *, readonly: bool = False) -> type:
    """Create a handler class bound to a specific .lattice/ directory."""

    class LatticeHandler(BaseHTTPRequestHandler):
        _lattice_dir: Path = lattice_dir
        _readonly: bool = readonly

        # Suppress default access logging to stdout; send to stderr instead
        def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
            sys.stderr.write(f"{self.address_string()} - {format % args}\n")

        def do_GET(self) -> None:  # noqa: N802
            parsed = urlparse(self.path)
            path = parsed.path.rstrip("/") or "/"

            if path == "/":
                self._serve_static("index.html", "text/html")
            elif path == "/stats-demo":
                self._serve_notes_file("stats-demo/demo.html", "text/html")
            elif path.startswith("/api/"):
                self._route_api(path)
            elif path.startswith("/static/"):
                # Serve static assets (JS, CSS) with path traversal protection
                rel_path = path[len("/static/") :]
                # Block path traversal
                if ".." in rel_path or rel_path.startswith("/"):
                    self._send_json(403, _err("FORBIDDEN", "Path traversal not allowed"))
                    return
                # Determine content type
                content_types = {
                    ".js": "application/javascript",
                    ".css": "text/css",
                    ".json": "application/json",
                    ".svg": "image/svg+xml",
                    ".png": "image/png",
                }
                ext = "." + rel_path.rsplit(".", 1)[-1] if "." in rel_path else ""
                content_type = content_types.get(ext, "application/octet-stream")
                self._serve_static(rel_path, content_type)
            else:
                self._send_json(404, _err("NOT_FOUND", f"Not found: {path}"))

        def do_POST(self) -> None:  # noqa: N802
            if self._readonly:
                self._send_json(403, _err("FORBIDDEN", "Dashboard is in read-only mode"))
                return

            parsed = urlparse(self.path)
            path = parsed.path.rstrip("/") or "/"

            if path.startswith("/api/"):
                self._route_api_post(path)
            else:
                self._send_json(404, _err("NOT_FOUND", f"Not found: {path}"))

        # ---------------------------------------------------------------
        # Static file serving
        # ---------------------------------------------------------------

        def _serve_static(self, filename: str, content_type: str) -> None:
            filepath = STATIC_DIR / filename
            if not filepath.is_file():
                self._send_json(404, _err("NOT_FOUND", f"Static file not found: {filename}"))
                return
            data = filepath.read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", f"{content_type}; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def _serve_notes_file(self, relpath: str, content_type: str) -> None:
            """Serve a file from the repo's notes/ directory."""
            repo_root = Path(self._lattice_dir).resolve().parent
            filepath = repo_root / "notes" / relpath
            if not filepath.is_file():
                self._send_json(404, _err("NOT_FOUND", f"File not found: {relpath}"))
                return
            data = filepath.read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", f"{content_type}; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        # ---------------------------------------------------------------
        # API routing
        # ---------------------------------------------------------------

        def _route_api(self, path: str) -> None:
            ld = self._lattice_dir

            if path == "/api/config":
                self._handle_config(ld)
            elif path == "/api/tasks":
                self._handle_tasks(ld)
            elif path == "/api/stats":
                self._handle_stats(ld)
            elif path == "/api/activity":
                self._handle_activity(ld)
            elif path == "/api/archived":
                self._handle_archived(ld)
            elif path == "/api/graph":
                self._handle_graph(ld)
            elif path == "/api/epics":
                self._handle_epics(ld)
            elif path == "/api/git":
                self._handle_git_summary(ld)
            elif path.startswith("/api/git/branches/"):
                # /api/git/branches/<name>/commits
                remainder = path[len("/api/git/branches/") :]
                if remainder.endswith("/commits"):
                    branch_name = remainder[: -len("/commits")]
                    self._handle_git_branch_commits(ld, branch_name)
                else:
                    self._send_json(404, _err("NOT_FOUND", f"Not found: {path}"))
            elif path.startswith("/api/tasks/"):
                remainder = path[len("/api/tasks/") :]
                if "/" in remainder:
                    # /api/tasks/<id>/events
                    task_id, sub = remainder.rsplit("/", 1)
                    if sub == "events":
                        self._handle_task_events(ld, task_id)
                    elif sub == "comments":
                        self._handle_task_comments(ld, task_id)
                    elif sub == "full":
                        self._handle_task_full(ld, task_id)
                    else:
                        self._send_json(404, _err("NOT_FOUND", f"Not found: {path}"))
                else:
                    self._handle_task_detail(ld, remainder)
            else:
                self._send_json(404, _err("NOT_FOUND", f"Unknown API endpoint: {path}"))

        def _route_api_post(self, path: str) -> None:
            ld = self._lattice_dir

            if path == "/api/config/dashboard":
                self._handle_post_dashboard_config(ld)
            elif path == "/api/tasks":
                self._handle_post_create_task(ld)
            elif path.startswith("/api/tasks/"):
                remainder = path[len("/api/tasks/") :]
                if "/" in remainder:
                    task_id, sub = remainder.rsplit("/", 1)
                    if sub == "status":
                        self._handle_post_task_status(ld, task_id)
                    elif sub == "assign":
                        self._handle_post_task_assign(ld, task_id)
                    elif sub == "comment":
                        self._handle_post_task_comment(ld, task_id)
                    elif sub == "update":
                        self._handle_post_task_update(ld, task_id)
                    elif sub == "archive":
                        self._handle_post_task_archive(ld, task_id)
                    elif sub == "comment-edit":
                        self._handle_post_task_comment_edit(ld, task_id)
                    elif sub == "comment-delete":
                        self._handle_post_task_comment_delete(ld, task_id)
                    elif sub == "react":
                        self._handle_post_task_react(ld, task_id)
                    elif sub == "unreact":
                        self._handle_post_task_unreact(ld, task_id)
                    elif sub == "open-notes":
                        self._handle_post_open_notes(ld, task_id)
                    elif sub == "open-plans":
                        self._handle_post_open_plans(ld, task_id)
                    else:
                        self._send_json(404, _err("NOT_FOUND", f"Not found: {path}"))
                else:
                    self._send_json(404, _err("NOT_FOUND", f"Not found: {path}"))
            else:
                self._send_json(404, _err("NOT_FOUND", f"Unknown API endpoint: {path}"))

        # ---------------------------------------------------------------
        # JSON response helper
        # ---------------------------------------------------------------

        def _send_json(self, status: int, body: str) -> None:
            data = body.encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        # ---------------------------------------------------------------
        # Endpoint handlers
        # ---------------------------------------------------------------

        def _handle_config(self, ld: Path) -> None:
            config_path = ld / "config.json"
            try:
                config = json.loads(config_path.read_text())
            except (json.JSONDecodeError, OSError) as exc:
                self._send_json(500, _err("READ_ERROR", f"Failed to read config: {exc}"))
                return
            self._send_json(200, _ok(config))

        def _handle_tasks(self, ld: Path) -> None:
            tasks_dir = ld / "tasks"
            snapshots: list[dict] = []
            if tasks_dir.is_dir():
                for task_file in sorted(tasks_dir.glob("*.json")):
                    try:
                        snap = json.loads(task_file.read_text())
                    except (json.JSONDecodeError, OSError):
                        continue
                    compact = compact_snapshot(snap)
                    compact["updated_at"] = snap.get("updated_at")
                    compact["created_at"] = snap.get("created_at")
                    compact["done_at"] = snap.get("done_at")
                    snapshots.append(compact)
            # Sort by ID
            snapshots.sort(key=lambda s: s.get("id", ""))
            self._send_json(200, _ok(snapshots))

        def _handle_task_detail(self, ld: Path, task_id: str) -> None:
            if not validate_id(task_id, "task"):
                self._send_json(400, _err("INVALID_ID", "Invalid task ID format"))
                return

            snapshot = _read_snapshot(ld, task_id)
            is_archived = False

            if snapshot is None:
                # Check archive
                snapshot = _read_snapshot_archive(ld, task_id)
                if snapshot is not None:
                    is_archived = True

            if snapshot is None:
                self._send_json(404, _err("NOT_FOUND", f"Task {task_id} not found"))
                return

            # Enrich with notes_exists, plan_exists, and artifacts
            if is_archived:
                notes_path = ld / "archive" / "notes" / f"{task_id}.md"
                plan_path = ld / "archive" / "plans" / f"{task_id}.md"
            else:
                notes_path = ld / "notes" / f"{task_id}.md"
                plan_path = ld / "plans" / f"{task_id}.md"

            result = dict(snapshot)
            result["notes_exists"] = notes_path.exists()
            result["plan_exists"] = plan_path.exists()
            result["artifacts"] = _read_artifact_info(ld, snapshot)
            if is_archived:
                result["archived"] = True

            self._send_json(200, _ok(result))

        def _handle_task_events(self, ld: Path, task_id: str) -> None:
            if not validate_id(task_id, "task"):
                self._send_json(400, _err("INVALID_ID", "Invalid task ID format"))
                return

            events = read_task_events(ld, task_id)
            if not events:
                # Check archive
                events = read_task_events(ld, task_id, is_archived=True)

            # Return newest first
            events.reverse()
            self._send_json(200, _ok(events))

        def _handle_task_comments(self, ld: Path, task_id: str) -> None:
            """Handle GET /api/tasks/<id>/comments — materialized comment tree."""
            if not validate_id(task_id, "task"):
                self._send_json(400, _err("INVALID_ID", "Invalid task ID format"))
                return

            # Try active events first, then archive
            events = read_task_events(ld, task_id)
            if not events:
                events = read_task_events(ld, task_id, is_archived=True)

            comments = materialize_comments(events)
            self._send_json(200, _ok(comments))

        def _handle_task_full(self, ld: Path, task_id: str) -> None:
            """Handle GET /api/tasks/<id>/full — combined snapshot + events + comments for Cube LOD 4."""
            if not validate_id(task_id, "task"):
                self._send_json(400, _err("INVALID_ID", "Invalid task ID format"))
                return

            snapshot = _read_snapshot(ld, task_id)
            is_archived = False
            if snapshot is None:
                snapshot = _read_snapshot_archive(ld, task_id)
                if snapshot is not None:
                    is_archived = True

            if snapshot is None:
                self._send_json(404, _err("NOT_FOUND", f"Task {task_id} not found"))
                return

            # Read events (latest 20)
            events = read_task_events(ld, task_id, is_archived=is_archived)
            events.reverse()
            recent_events = events[:20]

            # Materialize comments
            all_events = read_task_events(ld, task_id, is_archived=is_archived)
            comments = materialize_comments(all_events)

            # Enrich snapshot
            result = dict(snapshot)
            if is_archived:
                notes_path = ld / "archive" / "notes" / f"{task_id}.md"
                plan_path = ld / "archive" / "plans" / f"{task_id}.md"
            else:
                notes_path = ld / "notes" / f"{task_id}.md"
                plan_path = ld / "plans" / f"{task_id}.md"

            result["notes_exists"] = notes_path.exists()
            result["plan_exists"] = plan_path.exists()
            result["artifacts"] = _read_artifact_info(ld, snapshot)
            result["recent_events"] = recent_events
            result["comments"] = comments
            if is_archived:
                result["archived"] = True

            self._send_json(200, _ok(result))

        def _handle_activity(self, ld: Path) -> None:
            # Re-parse path to get query string (since _route_api strips it)
            parsed = urlparse(self.path)
            params = parse_qs(parsed.query)

            def _qs(key: str) -> str | None:
                vals = params.get(key)
                return vals[0] if vals else None

            # Parse pagination params
            try:
                limit = max(1, min(200, int(_qs("limit") or "50")))
            except (ValueError, TypeError):
                limit = 50
            try:
                offset = max(0, int(_qs("offset") or "0"))
            except (ValueError, TypeError):
                offset = 0

            type_filter = _qs("type")
            task_param = _qs("task")
            actor_filter = _qs("actor")
            after = _qs("after")
            before = _qs("before")
            search = _qs("search")

            has_filters = any([type_filter, task_param, actor_filter, after, before, search])

            # Resolve short ID for task filter
            task_filter: str | None = None
            if task_param:
                if validate_id(task_param, "task"):
                    task_filter = task_param
                else:
                    # Try short ID resolution via ids.json
                    from lattice.core.ids import is_short_id
                    from lattice.storage.short_ids import resolve_short_id

                    if is_short_id(task_param):
                        resolved = resolve_short_id(ld, task_param.upper())
                        if resolved:
                            task_filter = resolved
                        else:
                            # Unknown short ID — return empty
                            self._send_json(
                                200,
                                _ok(
                                    {
                                        "events": [],
                                        "total": 0,
                                        "offset": offset,
                                        "limit": limit,
                                        "has_more": False,
                                        "facets": {"types": [], "actors": [], "tasks": []},
                                    }
                                ),
                            )
                            return
                    else:
                        self._send_json(
                            400,
                            _err(
                                "VALIDATION_ERROR",
                                f"Invalid task filter: '{task_param}'",
                            ),
                        )
                        return

            # Collect events — full scan when filters active, tail otherwise
            all_events = _collect_events(ld, full_scan=has_filters, tail_n=10)

            # Build facets from the full (unfiltered) set for dropdown population
            facets = _build_facets(all_events, ld)

            # Apply filters
            filtered = _apply_activity_filters(
                all_events,
                type_filter=type_filter,
                task_filter=task_filter,
                actor_filter=actor_filter,
                after=after,
                before=before,
                search=search,
            )

            # Sort by (ts, id) descending
            filtered.sort(key=lambda e: (e.get("ts", ""), e.get("id", "")), reverse=True)

            total = len(filtered)
            page = filtered[offset : offset + limit]
            has_more = (offset + limit) < total

            self._send_json(
                200,
                _ok(
                    {
                        "events": page,
                        "total": total,
                        "offset": offset,
                        "limit": limit,
                        "has_more": has_more,
                        "facets": facets,
                    }
                ),
            )

        def _handle_stats(self, ld: Path) -> None:
            config_path = ld / "config.json"
            try:
                config = json.loads(config_path.read_text())
            except (json.JSONDecodeError, OSError) as exc:
                self._send_json(500, _err("READ_ERROR", f"Failed to read config: {exc}"))
                return
            from lattice.core.stats import build_stats

            stats = build_stats(ld, config)
            self._send_json(200, _ok(stats))

        def _handle_archived(self, ld: Path) -> None:
            archive_dir = ld / "archive" / "tasks"
            snapshots: list[dict] = []
            if archive_dir.is_dir():
                for task_file in sorted(archive_dir.glob("*.json")):
                    try:
                        snap = json.loads(task_file.read_text())
                    except (json.JSONDecodeError, OSError):
                        continue
                    compact = compact_snapshot(snap)
                    compact["updated_at"] = snap.get("updated_at")
                    compact["created_at"] = snap.get("created_at")
                    compact["done_at"] = snap.get("done_at")
                    compact["archived"] = True
                    snapshots.append(compact)
            snapshots.sort(key=lambda s: s.get("id", ""))
            self._send_json(200, _ok(snapshots))

        def _handle_graph(self, ld: Path) -> None:
            """Handle GET /api/graph — return nodes + directed edges for graph visualization."""
            tasks_dir = ld / "tasks"
            snapshots: list[dict] = []
            if tasks_dir.is_dir():
                for task_file in sorted(tasks_dir.glob("*.json")):
                    try:
                        snap = json.loads(task_file.read_text())
                    except (json.JSONDecodeError, OSError):
                        continue
                    snapshots.append(snap)

            # Build set of active task IDs for filtering link targets
            active_ids: set[str] = {s["id"] for s in snapshots if "id" in s}

            # Build nodes — extract only the fields needed for graph rendering
            nodes: list[dict] = []
            max_updated_at = ""
            for snap in snapshots:
                node = {
                    "id": snap.get("id"),
                    "short_id": snap.get("short_id"),
                    "title": snap.get("title"),
                    "status": snap.get("status"),
                    "priority": snap.get("priority"),
                    "type": snap.get("type"),
                    "assigned_to": snap.get("assigned_to"),
                    "branch_links": snap.get("branch_links", []),
                    "created_at": snap.get("created_at"),
                    "updated_at": snap.get("updated_at"),
                    "description_snippet": (snap.get("description") or "")[:200],
                }
                nodes.append(node)

                updated = snap.get("updated_at", "")
                if updated > max_updated_at:
                    max_updated_at = updated

            # Build directed edges from relationships_out.
            # Edges are directed: source is the task containing the relationship,
            # target is the referenced task. Edge direction meaning varies by type
            # (e.g., for "blocks", source blocks target).
            links: list[dict] = []
            for snap in snapshots:
                task_id = snap.get("id")
                for rel in snap.get("relationships_out", []):
                    target_id = rel.get("target_task_id")
                    # Only emit link if target exists in the active task set
                    if target_id and target_id in active_ids:
                        links.append(
                            {
                                "source": task_id,
                                "target": target_id,
                                "type": rel.get("type"),
                            }
                        )

            # Revision string for cheap change detection
            revision = f"{len(nodes)}:{max_updated_at}"

            # ETag / 304 support — return early if client has current data
            etag = f'"{revision}"'  # ETags must be quoted per RFC 7232
            if_none_match = self.headers.get("If-None-Match")
            if if_none_match and if_none_match == etag:
                self.send_response(304)
                self.send_header("ETag", etag)
                self.end_headers()
                return

            body = _ok({"nodes": nodes, "links": links, "revision": revision})
            data = body.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.send_header("ETag", etag)
            self.end_headers()
            self.wfile.write(data)

        # ---------------------------------------------------------------
        # Epics API handler
        # ---------------------------------------------------------------

        def _handle_epics(self, ld: Path) -> None:
            """Handle GET /api/epics — return epic tree hierarchy with derived status."""
            tasks_dir = ld / "tasks"
            snapshots: list[dict] = []
            if tasks_dir.is_dir():
                for task_file in sorted(tasks_dir.glob("*.json")):
                    try:
                        snap = json.loads(task_file.read_text())
                    except (json.JSONDecodeError, OSError):
                        continue
                    snapshots.append(snap)

            # Build graph links from relationships_out
            active_ids: set[str] = {s["id"] for s in snapshots if "id" in s}
            graph_links: list[dict] = []
            for snap in snapshots:
                for rel in snap.get("relationships_out", []):
                    target_id = rel.get("target_task_id")
                    if target_id and target_id in active_ids and rel.get("type") == "subtask_of":
                        graph_links.append(
                            {
                                "source": snap["id"],
                                "target": target_id,
                                "type": "subtask_of",
                            }
                        )

            # Find which tasks are children (have a subtask_of relationship)
            child_ids: set[str] = {link["source"] for link in graph_links}
            # Find which tasks are parents (are targets of subtask_of)
            parent_ids: set[str] = {link["target"] for link in graph_links}

            # Root epics: tasks that are parents but not children
            root_ids = parent_ids - child_ids

            # Build parent -> children map
            parent_to_children: dict[str, list[str]] = {}
            for link in graph_links:
                parent_to_children.setdefault(link["target"], []).append(link["source"])

            snap_by_id: dict[str, dict] = {s["id"]: s for s in snapshots if "id" in s}

            def build_tree_node(task_id: str, depth: int = 0) -> dict | None:
                snap = snap_by_id.get(task_id)
                if snap is None:
                    return None
                children_ids = parent_to_children.get(task_id, [])
                children = []
                for cid in sorted(
                    children_ids, key=lambda x: snap_by_id.get(x, {}).get("short_id", x)
                ):
                    child_node = build_tree_node(cid, depth + 1)
                    if child_node:
                        children.append(child_node)

                node: dict[str, Any] = {
                    "id": snap.get("id"),
                    "short_id": snap.get("short_id"),
                    "title": snap.get("title"),
                    "status": snap.get("status"),
                    "priority": snap.get("priority"),
                    "type": snap.get("type"),
                    "assigned_to": snap.get("assigned_to"),
                    "depth": depth,
                }
                if children:
                    node["children"] = children
                    # Compute derived status for this parent
                    derived = compute_epic_derived_status(task_id, snapshots, graph_links)
                    node["derived"] = derived
                return node

            trees: list[dict] = []
            for rid in sorted(root_ids, key=lambda x: snap_by_id.get(x, {}).get("short_id", x)):
                tree = build_tree_node(rid)
                if tree:
                    trees.append(tree)

            # Also include orphan tasks (not parent, not child) for completeness
            orphan_ids = active_ids - parent_ids - child_ids
            orphans: list[dict] = []
            for oid in sorted(orphan_ids, key=lambda x: snap_by_id.get(x, {}).get("short_id", x)):
                snap = snap_by_id.get(oid)
                if snap:
                    orphans.append(
                        {
                            "id": snap.get("id"),
                            "short_id": snap.get("short_id"),
                            "title": snap.get("title"),
                            "status": snap.get("status"),
                            "priority": snap.get("priority"),
                            "type": snap.get("type"),
                            "assigned_to": snap.get("assigned_to"),
                            "depth": 0,
                        }
                    )

            self._send_json(
                200,
                _ok(
                    {
                        "trees": trees,
                        "orphans": orphans,
                        "total_tasks": len(snapshots),
                        "total_epics": len(root_ids),
                    }
                ),
            )

        # ---------------------------------------------------------------
        # Git API handlers
        # ---------------------------------------------------------------

        def _handle_git_summary(self, ld: Path) -> None:
            """Handle GET /api/git — return full git summary with caching + ETag."""
            from lattice.dashboard.git_reader import get_git_summary

            summary, etag_value = get_git_summary(ld)

            if not summary.get("available", False) or not etag_value:
                # Not available or no etag — return without caching headers
                self._send_json(200, _ok(summary))
                return

            # ETag / 304 support
            etag = f'"{etag_value}"'
            if_none_match = self.headers.get("If-None-Match")
            if if_none_match and if_none_match == etag:
                self.send_response(304)
                self.send_header("ETag", etag)
                self.end_headers()
                return

            body = _ok(summary)
            data = body.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.send_header("ETag", etag)
            self.send_header("Cache-Control", "max-age=30")
            self.end_headers()
            self.wfile.write(data)

        def _handle_git_branch_commits(self, ld: Path, branch_name: str) -> None:
            """Handle GET /api/git/branches/<name>/commits — recent commits for a branch."""
            from urllib.parse import unquote

            from lattice.dashboard.git_reader import (
                _validate_branch_name,
                find_git_root,
                get_recent_commits,
                git_available,
            )

            if not branch_name:
                self._send_json(400, _err("VALIDATION_ERROR", "Branch name is required"))
                return

            # URL-decode the branch name (e.g., %2F -> /)
            branch_name = unquote(branch_name)

            # Reject branch names that could be interpreted as git flags
            if not _validate_branch_name(branch_name):
                self._send_json(
                    400,
                    _err("VALIDATION_ERROR", "Invalid branch name"),
                )
                return

            if not git_available():
                self._send_json(
                    200,
                    _ok({"available": False, "reason": "git_not_installed"}),
                )
                return

            repo_root = find_git_root(ld.parent)
            if repo_root is None:
                self._send_json(
                    200,
                    _ok({"available": False, "reason": "not_a_git_repo"}),
                )
                return

            commits = get_recent_commits(repo_root, branch_name)
            self._send_json(
                200,
                _ok(
                    {
                        "branch": branch_name,
                        "commits": commits,
                        "count": len(commits),
                    }
                ),
            )

        # ---------------------------------------------------------------
        # POST endpoint handlers
        # ---------------------------------------------------------------

        def _read_request_body(self) -> dict | None:
            """Read and parse a JSON request body. Returns None on failure."""
            try:
                content_length = int(self.headers.get("Content-Length", 0))
            except (TypeError, ValueError):
                self._send_json(400, _err("BAD_REQUEST", "Missing or invalid Content-Length"))
                return None

            if content_length == 0:
                self._send_json(400, _err("BAD_REQUEST", "Empty request body"))
                return None

            if content_length > MAX_REQUEST_BODY_BYTES:
                self._send_json(
                    413,
                    _err(
                        "PAYLOAD_TOO_LARGE", f"Request body exceeds {MAX_REQUEST_BODY_BYTES} bytes"
                    ),
                )
                return None

            try:
                raw = self.rfile.read(content_length)
                return json.loads(raw)
            except json.JSONDecodeError:
                self._send_json(400, _err("BAD_REQUEST", "Invalid JSON in request body"))
                return None

        def _handle_post_task_status(self, ld: Path, task_id: str) -> None:
            """Handle POST /api/tasks/<id>/status — change task status."""
            if not validate_id(task_id, "task"):
                self._send_json(400, _err("INVALID_ID", "Invalid task ID format"))
                return

            body = self._read_request_body()
            if body is None:
                return  # error already sent

            new_status = body.get("status")
            actor = body.get("actor", "dashboard:web")
            force = body.get("force", False)
            reason = body.get("reason")

            if not new_status:
                self._send_json(400, _err("VALIDATION_ERROR", "Missing 'status' field"))
                return

            if not isinstance(new_status, str):
                self._send_json(400, _err("VALIDATION_ERROR", "'status' must be a string"))
                return

            if not validate_actor(actor):
                self._send_json(400, _err("VALIDATION_ERROR", f"Invalid actor format: '{actor}'"))
                return

            # Read config
            config_path = ld / "config.json"
            try:
                config = json.loads(config_path.read_text())
            except (json.JSONDecodeError, OSError) as exc:
                self._send_json(500, _err("READ_ERROR", f"Failed to read config: {exc}"))
                return

            # Validate new_status is a known status
            if not validate_status(config, new_status):
                valid = ", ".join(config.get("workflow", {}).get("statuses", []))
                self._send_json(
                    400,
                    _err("VALIDATION_ERROR", f"Invalid status: '{new_status}'. Valid: {valid}"),
                )
                return

            # Read snapshot (outside lock — acceptable TOCTOU at v0 scale)
            snapshot = _read_snapshot(ld, task_id)
            if snapshot is None:
                self._send_json(404, _err("NOT_FOUND", f"Task {task_id} not found"))
                return

            # Epics cannot have their status set directly
            if snapshot.get("type") == "epic":
                self._send_json(
                    400,
                    _err(
                        "EPIC_STATUS_REJECTED",
                        "Cannot set status on epics. Epic status is derived from subtask completion.",
                    ),
                )
                return

            current_status = snapshot["status"]

            if current_status == new_status:
                self._send_json(
                    200,
                    _ok({"message": f"Already at status {new_status}"}),
                )
                return

            if not validate_transition(config, current_status, new_status):
                if not force:
                    self._send_json(
                        400,
                        _err(
                            "INVALID_TRANSITION",
                            f"Invalid transition from {current_status} to {new_status}. "
                            "Send force=true with a reason to override.",
                        ),
                    )
                    return
                if not reason or not isinstance(reason, str) or not reason.strip():
                    self._send_json(
                        400,
                        _err("VALIDATION_ERROR", "'reason' is required with force=true"),
                    )
                    return

            # Build event data
            event_data: dict = {"from": current_status, "to": new_status}
            if force:
                event_data["force"] = True
                event_data["reason"] = reason

            event = create_event(
                type="status_changed",
                task_id=task_id,
                actor=actor,
                data=event_data,
            )
            updated_snapshot = apply_event_to_snapshot(snapshot, event)

            try:
                write_task_event(ld, task_id, [event], updated_snapshot, config)
            except Exception as exc:
                self._send_json(500, _err("WRITE_ERROR", f"Failed to update status: {exc}"))
                return

            self._send_json(200, _ok(updated_snapshot))

        def _handle_post_dashboard_config(self, ld: Path) -> None:
            """Handle POST /api/config/dashboard — save dashboard settings."""
            body = self._read_request_body()
            if body is None:
                return  # error already sent

            # Validate body structure: only allow known keys
            allowed_keys = {"background_image", "lane_colors", "theme", "voice"}
            unknown = set(body.keys()) - allowed_keys
            if unknown:
                self._send_json(
                    400,
                    _err("VALIDATION_ERROR", f"Unknown keys: {', '.join(sorted(unknown))}"),
                )
                return

            # Validate lane_colors if present
            if "lane_colors" in body:
                lc = body["lane_colors"]
                if not isinstance(lc, dict):
                    self._send_json(
                        400, _err("VALIDATION_ERROR", "'lane_colors' must be an object")
                    )
                    return
                for k, v in lc.items():
                    if not isinstance(k, str) or not isinstance(v, str):
                        self._send_json(
                            400,
                            _err(
                                "VALIDATION_ERROR", "lane_colors keys and values must be strings"
                            ),
                        )
                        return

            # Validate theme if present
            if "theme" in body:
                theme = body["theme"]
                if theme is not None and not isinstance(theme, str):
                    self._send_json(
                        400,
                        _err("VALIDATION_ERROR", "'theme' must be a string or null"),
                    )
                    return

            # Validate background_image if present
            if "background_image" in body:
                bg = body["background_image"]
                if bg is not None and not isinstance(bg, str):
                    self._send_json(
                        400,
                        _err("VALIDATION_ERROR", "'background_image' must be a string or null"),
                    )
                    return
                if bg is not None and bg != "" and not bg.startswith(("http://", "https://")):
                    self._send_json(
                        400,
                        _err(
                            "VALIDATION_ERROR", "'background_image' must be an http or https URL"
                        ),
                    )
                    return

            # Validate voice if present
            if "voice" in body:
                v = body["voice"]
                if not isinstance(v, str):
                    self._send_json(400, _err("VALIDATION_ERROR", "'voice' must be a string"))
                    return

            # Read, merge, write config atomically
            config_path = ld / "config.json"
            locks_dir = ld / "locks"

            try:
                with multi_lock(locks_dir, ["config"]):
                    try:
                        config = json.loads(config_path.read_text())
                    except (json.JSONDecodeError, OSError) as exc:
                        self._send_json(500, _err("READ_ERROR", f"Failed to read config: {exc}"))
                        return

                    dashboard = config.get("dashboard", {})

                    if "background_image" in body:
                        bg = body["background_image"]
                        if bg is None or bg == "":
                            dashboard.pop("background_image", None)
                        else:
                            dashboard["background_image"] = bg

                    if "lane_colors" in body:
                        dashboard["lane_colors"] = body["lane_colors"]

                    if "theme" in body:
                        theme = body["theme"]
                        if theme is None:
                            dashboard.pop("theme", None)
                        else:
                            dashboard["theme"] = theme

                    if "voice" in body:
                        voice = body["voice"]
                        if voice is None:
                            dashboard.pop("voice", None)
                        else:
                            dashboard["voice"] = voice

                    if dashboard:
                        config["dashboard"] = dashboard
                    else:
                        config.pop("dashboard", None)

                    atomic_write(config_path, serialize_config(config))

            except Exception as exc:
                self._send_json(
                    500, _err("WRITE_ERROR", f"Failed to save dashboard config: {exc}")
                )
                return

            self._send_json(200, _ok(config.get("dashboard", {})))

        # ---------------------------------------------------------------
        # POST /api/tasks — Create Task
        # ---------------------------------------------------------------

        def _handle_post_create_task(self, ld: Path) -> None:
            """Handle POST /api/tasks — create a new task."""
            body = self._read_request_body()
            if body is None:
                return

            title = body.get("title")
            actor = body.get("actor", "dashboard:web")

            if not title or not isinstance(title, str) or not title.strip():
                self._send_json(400, _err("VALIDATION_ERROR", "Missing or empty 'title' field"))
                return

            title = title.strip()

            if not validate_actor(actor):
                self._send_json(400, _err("VALIDATION_ERROR", f"Invalid actor format: '{actor}'"))
                return

            # Read config
            config_path = ld / "config.json"
            try:
                config = json.loads(config_path.read_text())
            except (json.JSONDecodeError, OSError) as exc:
                self._send_json(500, _err("READ_ERROR", f"Failed to read config: {exc}"))
                return

            # Apply defaults
            status = body.get("status") or config.get("default_status", "backlog")
            priority = body.get("priority") or config.get("default_priority", "medium")
            task_type = body.get("type") or "task"
            description = body.get("description")
            tags = body.get("tags")
            assigned_to = body.get("assigned_to")
            urgency = body.get("urgency")

            # Validate
            if not validate_status(config, status):
                valid = ", ".join(config.get("workflow", {}).get("statuses", []))
                self._send_json(
                    400, _err("VALIDATION_ERROR", f"Invalid status: '{status}'. Valid: {valid}")
                )
                return

            if not validate_task_type(config, task_type):
                valid = ", ".join(config.get("task_types", []))
                self._send_json(
                    400,
                    _err("VALIDATION_ERROR", f"Invalid task type: '{task_type}'. Valid: {valid}"),
                )
                return

            if priority not in VALID_PRIORITIES:
                valid = ", ".join(VALID_PRIORITIES)
                self._send_json(
                    400,
                    _err("VALIDATION_ERROR", f"Invalid priority: '{priority}'. Valid: {valid}"),
                )
                return

            if urgency is not None and urgency not in VALID_URGENCIES:
                valid = ", ".join(VALID_URGENCIES)
                self._send_json(
                    400,
                    _err("VALIDATION_ERROR", f"Invalid urgency: '{urgency}'. Valid: {valid}"),
                )
                return

            if assigned_to is not None and not validate_actor(assigned_to):
                self._send_json(
                    400,
                    _err("VALIDATION_ERROR", f"Invalid assigned_to format: '{assigned_to}'"),
                )
                return

            if tags is not None and not isinstance(tags, list):
                self._send_json(400, _err("VALIDATION_ERROR", "'tags' must be an array"))
                return

            if description is not None and not isinstance(description, str):
                self._send_json(400, _err("VALIDATION_ERROR", "'description' must be a string"))
                return

            # Generate ID
            task_id = generate_task_id()

            # Allocate short ID if project code is configured
            project_code = config.get("project_code")
            subproject_code = config.get("subproject_code")
            short_id: str | None = None
            if project_code:
                prefix = f"{project_code}-{subproject_code}" if subproject_code else project_code
                short_id, _ = allocate_short_id(ld, prefix, task_ulid=task_id)

            # Build event data
            event_data: dict = {
                "title": title,
                "status": status,
                "type": task_type,
                "priority": priority,
            }
            if urgency is not None:
                event_data["urgency"] = urgency
            if description is not None:
                event_data["description"] = description
            if tags:
                event_data["tags"] = tags
            if assigned_to is not None:
                event_data["assigned_to"] = assigned_to
            if short_id is not None:
                event_data["short_id"] = short_id

            event = create_event(
                type="task_created",
                task_id=task_id,
                actor=actor,
                data=event_data,
            )
            snapshot = apply_event_to_snapshot(None, event)

            try:
                write_task_event(ld, task_id, [event], snapshot, config)
            except Exception as exc:
                self._send_json(500, _err("WRITE_ERROR", f"Failed to create task: {exc}"))
                return

            # Scaffold plan file (plans are scaffolded on create; notes are lazy)
            scaffold_plan(ld, task_id, title, short_id, description)

            self._send_json(201, _ok(snapshot))

        # ---------------------------------------------------------------
        # POST /api/tasks/<id>/assign — Assign/Unassign
        # ---------------------------------------------------------------

        def _handle_post_task_assign(self, ld: Path, task_id: str) -> None:
            """Handle POST /api/tasks/<id>/assign — assign or unassign a task."""
            if not validate_id(task_id, "task"):
                self._send_json(400, _err("INVALID_ID", "Invalid task ID format"))
                return

            body = self._read_request_body()
            if body is None:
                return

            assigned_to = body.get("assigned_to")
            actor = body.get("actor", "dashboard:web")

            if not validate_actor(actor):
                self._send_json(400, _err("VALIDATION_ERROR", f"Invalid actor format: '{actor}'"))
                return

            # assigned_to can be null (unassign) or a valid actor string
            if assigned_to is not None and not validate_actor(assigned_to):
                self._send_json(
                    400,
                    _err("VALIDATION_ERROR", f"Invalid assigned_to format: '{assigned_to}'"),
                )
                return

            # Read config for hooks
            config_path = ld / "config.json"
            try:
                config = json.loads(config_path.read_text())
            except (json.JSONDecodeError, OSError) as exc:
                self._send_json(500, _err("READ_ERROR", f"Failed to read config: {exc}"))
                return

            snapshot = _read_snapshot(ld, task_id)
            if snapshot is None:
                self._send_json(404, _err("NOT_FOUND", f"Task {task_id} not found"))
                return

            current_assigned = snapshot.get("assigned_to")

            if current_assigned == assigned_to:
                self._send_json(200, _ok(snapshot))
                return

            event = create_event(
                type="assignment_changed",
                task_id=task_id,
                actor=actor,
                data={"from": current_assigned, "to": assigned_to},
            )
            updated_snapshot = apply_event_to_snapshot(snapshot, event)

            try:
                write_task_event(ld, task_id, [event], updated_snapshot, config)
            except Exception as exc:
                self._send_json(500, _err("WRITE_ERROR", f"Failed to assign task: {exc}"))
                return

            self._send_json(200, _ok(updated_snapshot))

        # ---------------------------------------------------------------
        # POST /api/tasks/<id>/comment — Add Comment
        # ---------------------------------------------------------------

        def _handle_post_task_comment(self, ld: Path, task_id: str) -> None:
            """Handle POST /api/tasks/<id>/comment — add a comment (optionally threaded)."""
            if not validate_id(task_id, "task"):
                self._send_json(400, _err("INVALID_ID", "Invalid task ID format"))
                return

            body = self._read_request_body()
            if body is None:
                return

            comment_body = body.get("body", "")
            actor = body.get("actor", "dashboard:web")
            parent_id = body.get("parent_id")

            try:
                comment_body = validate_comment_body(comment_body)
            except ValueError as exc:
                self._send_json(400, _err("VALIDATION_ERROR", str(exc)))
                return

            if not validate_actor(actor):
                self._send_json(400, _err("VALIDATION_ERROR", f"Invalid actor format: '{actor}'"))
                return

            # Read config for hooks
            config_path = ld / "config.json"
            try:
                config = json.loads(config_path.read_text())
            except (json.JSONDecodeError, OSError) as exc:
                self._send_json(500, _err("READ_ERROR", f"Failed to read config: {exc}"))
                return

            snapshot = _read_snapshot(ld, task_id)
            if snapshot is None:
                self._send_json(404, _err("NOT_FOUND", f"Task {task_id} not found"))
                return

            # Validate parent_id for threaded replies
            if parent_id is not None:
                events = read_task_events(ld, task_id)
                try:
                    validate_comment_for_reply(events, parent_id)
                except ValueError as exc:
                    self._send_json(400, _err("VALIDATION_ERROR", str(exc)))
                    return

            event_data: dict = {"body": comment_body.strip()}
            if parent_id is not None:
                event_data["parent_id"] = parent_id

            event = create_event(
                type="comment_added",
                task_id=task_id,
                actor=actor,
                data=event_data,
            )
            updated_snapshot = apply_event_to_snapshot(snapshot, event)

            try:
                write_task_event(ld, task_id, [event], updated_snapshot, config)
            except Exception as exc:
                self._send_json(500, _err("WRITE_ERROR", f"Failed to add comment: {exc}"))
                return

            self._send_json(200, _ok(updated_snapshot))

        # ---------------------------------------------------------------
        # POST /api/tasks/<id>/update — Edit Fields
        # ---------------------------------------------------------------

        _UPDATABLE_FIELDS = frozenset(
            {"title", "description", "priority", "urgency", "type", "tags"}
        )

        def _handle_post_task_update(self, ld: Path, task_id: str) -> None:
            """Handle POST /api/tasks/<id>/update — edit task fields."""
            if not validate_id(task_id, "task"):
                self._send_json(400, _err("INVALID_ID", "Invalid task ID format"))
                return

            body = self._read_request_body()
            if body is None:
                return

            fields = body.get("fields")
            actor = body.get("actor", "dashboard:web")

            if not fields or not isinstance(fields, dict):
                self._send_json(
                    400, _err("VALIDATION_ERROR", "Missing or invalid 'fields' object")
                )
                return

            if not validate_actor(actor):
                self._send_json(400, _err("VALIDATION_ERROR", f"Invalid actor format: '{actor}'"))
                return

            # Read config for validation
            config_path = ld / "config.json"
            try:
                config = json.loads(config_path.read_text())
            except (json.JSONDecodeError, OSError) as exc:
                self._send_json(500, _err("READ_ERROR", f"Failed to read config: {exc}"))
                return

            # Validate field names
            unknown = set(fields.keys()) - self._UPDATABLE_FIELDS
            if unknown:
                valid = ", ".join(sorted(self._UPDATABLE_FIELDS))
                self._send_json(
                    400,
                    _err(
                        "VALIDATION_ERROR",
                        f"Unknown fields: {', '.join(sorted(unknown))}. Updatable: {valid}",
                    ),
                )
                return

            # Validate field values
            if "priority" in fields and fields["priority"] not in VALID_PRIORITIES:
                valid = ", ".join(VALID_PRIORITIES)
                self._send_json(
                    400,
                    _err(
                        "VALIDATION_ERROR",
                        f"Invalid priority: '{fields['priority']}'. Valid: {valid}",
                    ),
                )
                return

            if "urgency" in fields and fields["urgency"] not in VALID_URGENCIES:
                valid = ", ".join(VALID_URGENCIES)
                self._send_json(
                    400,
                    _err(
                        "VALIDATION_ERROR",
                        f"Invalid urgency: '{fields['urgency']}'. Valid: {valid}",
                    ),
                )
                return

            if "type" in fields and not validate_task_type(config, fields["type"]):
                valid = ", ".join(config.get("task_types", []))
                self._send_json(
                    400,
                    _err(
                        "VALIDATION_ERROR",
                        f"Invalid task type: '{fields['type']}'. Valid: {valid}",
                    ),
                )
                return

            if "title" in fields:
                if not isinstance(fields["title"], str) or not fields["title"].strip():
                    self._send_json(
                        400, _err("VALIDATION_ERROR", "Title must be a non-empty string")
                    )
                    return

            if "tags" in fields:
                if not isinstance(fields["tags"], list):
                    self._send_json(400, _err("VALIDATION_ERROR", "'tags' must be an array"))
                    return

            snapshot = _read_snapshot(ld, task_id)
            if snapshot is None:
                self._send_json(404, _err("NOT_FOUND", f"Task {task_id} not found"))
                return

            # Build events for changed fields
            shared_ts = utc_now()
            events: list[dict] = []

            for field, new_value in fields.items():
                if field == "tags":
                    old_value = snapshot.get("tags") or []
                else:
                    old_value = snapshot.get(field)

                if old_value == new_value:
                    continue

                events.append(
                    create_event(
                        type="field_updated",
                        task_id=task_id,
                        actor=actor,
                        data={"field": field, "from": old_value, "to": new_value},
                        ts=shared_ts,
                    )
                )

            if not events:
                self._send_json(200, _ok(snapshot))
                return

            # Apply events incrementally
            updated_snapshot = snapshot
            for event in events:
                updated_snapshot = apply_event_to_snapshot(updated_snapshot, event)

            try:
                write_task_event(ld, task_id, events, updated_snapshot, config)
            except Exception as exc:
                self._send_json(500, _err("WRITE_ERROR", f"Failed to update task: {exc}"))
                return

            self._send_json(200, _ok(updated_snapshot))

        # ---------------------------------------------------------------
        # POST /api/tasks/<id>/archive — Archive Task
        # ---------------------------------------------------------------

        def _handle_post_task_archive(self, ld: Path, task_id: str) -> None:
            """Handle POST /api/tasks/<id>/archive — archive a task."""
            if not validate_id(task_id, "task"):
                self._send_json(400, _err("INVALID_ID", "Invalid task ID format"))
                return

            body = self._read_request_body()
            if body is None:
                return

            actor = body.get("actor", "dashboard:web")

            if not validate_actor(actor):
                self._send_json(400, _err("VALIDATION_ERROR", f"Invalid actor format: '{actor}'"))
                return

            # Read config for hooks
            config_path = ld / "config.json"
            try:
                config = json.loads(config_path.read_text())
            except (json.JSONDecodeError, OSError) as exc:
                self._send_json(500, _err("READ_ERROR", f"Failed to read config: {exc}"))
                return

            # Check if already archived
            archive_check = ld / "archive" / "tasks" / f"{task_id}.json"
            if archive_check.is_file():
                self._send_json(400, _err("CONFLICT", f"Task {task_id} is already archived"))
                return

            event = None  # will be set inside the lock
            try:
                locks_dir = ld / "locks"
                lock_keys = sorted([f"events_{task_id}", f"tasks_{task_id}", "events__lifecycle"])

                with multi_lock(locks_dir, lock_keys):
                    snapshot = _read_snapshot(ld, task_id)
                    if snapshot is None:
                        self._send_json(404, _err("NOT_FOUND", f"Task {task_id} not found"))
                        return

                    event = create_event(
                        type="task_archived",
                        task_id=task_id,
                        actor=actor,
                        data={},
                    )
                    updated_snapshot = apply_event_to_snapshot(snapshot, event)

                    # 1. Append event to per-task log
                    event_path = ld / "events" / f"{task_id}.jsonl"
                    jsonl_append(event_path, serialize_event(event))

                    # 2. Append to lifecycle log
                    lifecycle_path = ld / "events" / "_lifecycle.jsonl"
                    jsonl_append(lifecycle_path, serialize_event(event))

                    # 3. Write snapshot to archive
                    atomic_write(
                        ld / "archive" / "tasks" / f"{task_id}.json",
                        serialize_snapshot(updated_snapshot),
                    )

                    # 4. Remove active snapshot
                    snapshot_path = ld / "tasks" / f"{task_id}.json"
                    if snapshot_path.exists():
                        snapshot_path.unlink()

                    # 5. Move event log to archive
                    if event_path.exists():
                        shutil.move(
                            str(event_path),
                            str(ld / "archive" / "events" / f"{task_id}.jsonl"),
                        )

                    # 6. Move notes if they exist
                    notes_path = ld / "notes" / f"{task_id}.md"
                    if notes_path.exists():
                        shutil.move(
                            str(notes_path),
                            str(ld / "archive" / "notes" / f"{task_id}.md"),
                        )

                    # 7. Move plans if they exist
                    plans_path = ld / "plans" / f"{task_id}.md"
                    if plans_path.exists():
                        (ld / "archive" / "plans").mkdir(parents=True, exist_ok=True)
                        shutil.move(
                            str(plans_path),
                            str(ld / "archive" / "plans" / f"{task_id}.md"),
                        )

            except Exception as exc:
                self._send_json(500, _err("WRITE_ERROR", f"Failed to archive task: {exc}"))
                return

            # Fire hooks after locks released
            if event is not None:
                execute_hooks(config, ld, task_id, event)

            self._send_json(200, _ok({"message": f"Task {task_id} archived"}))

        # ---------------------------------------------------------------
        # POST /api/tasks/<id>/comment-edit — Edit Comment
        # ---------------------------------------------------------------

        def _handle_post_task_comment_edit(self, ld: Path, task_id: str) -> None:
            """Handle POST /api/tasks/<id>/comment-edit — edit a comment's body."""
            if not validate_id(task_id, "task"):
                self._send_json(400, _err("INVALID_ID", "Invalid task ID format"))
                return

            body = self._read_request_body()
            if body is None:
                return

            comment_id = body.get("comment_id")
            new_body = body.get("body", "")
            actor = body.get("actor", "dashboard:web")

            if not comment_id or not isinstance(comment_id, str):
                self._send_json(
                    400, _err("VALIDATION_ERROR", "Missing or invalid 'comment_id' field")
                )
                return

            try:
                new_body = validate_comment_body(new_body)
            except ValueError as exc:
                self._send_json(400, _err("VALIDATION_ERROR", str(exc)))
                return

            if not validate_actor(actor):
                self._send_json(400, _err("VALIDATION_ERROR", f"Invalid actor format: '{actor}'"))
                return

            # Read config for hooks
            config_path = ld / "config.json"
            try:
                config = json.loads(config_path.read_text())
            except (json.JSONDecodeError, OSError) as exc:
                self._send_json(500, _err("READ_ERROR", f"Failed to read config: {exc}"))
                return

            snapshot = _read_snapshot(ld, task_id)
            if snapshot is None:
                self._send_json(404, _err("NOT_FOUND", f"Task {task_id} not found"))
                return

            events = read_task_events(ld, task_id)
            try:
                previous_body = validate_comment_for_edit(events, comment_id)
            except ValueError as exc:
                self._send_json(400, _err("VALIDATION_ERROR", str(exc)))
                return

            event = create_event(
                type="comment_edited",
                task_id=task_id,
                actor=actor,
                data={
                    "comment_id": comment_id,
                    "body": new_body.strip(),
                    "previous_body": previous_body,
                },
            )
            updated_snapshot = apply_event_to_snapshot(snapshot, event)

            try:
                write_task_event(ld, task_id, [event], updated_snapshot, config)
            except Exception as exc:
                self._send_json(500, _err("WRITE_ERROR", f"Failed to edit comment: {exc}"))
                return

            self._send_json(200, _ok(updated_snapshot))

        # ---------------------------------------------------------------
        # POST /api/tasks/<id>/comment-delete — Delete Comment
        # ---------------------------------------------------------------

        def _handle_post_task_comment_delete(self, ld: Path, task_id: str) -> None:
            """Handle POST /api/tasks/<id>/comment-delete — soft-delete a comment."""
            if not validate_id(task_id, "task"):
                self._send_json(400, _err("INVALID_ID", "Invalid task ID format"))
                return

            body = self._read_request_body()
            if body is None:
                return

            comment_id = body.get("comment_id")
            actor = body.get("actor", "dashboard:web")

            if not comment_id or not isinstance(comment_id, str):
                self._send_json(
                    400, _err("VALIDATION_ERROR", "Missing or invalid 'comment_id' field")
                )
                return

            if not validate_actor(actor):
                self._send_json(400, _err("VALIDATION_ERROR", f"Invalid actor format: '{actor}'"))
                return

            # Read config for hooks
            config_path = ld / "config.json"
            try:
                config = json.loads(config_path.read_text())
            except (json.JSONDecodeError, OSError) as exc:
                self._send_json(500, _err("READ_ERROR", f"Failed to read config: {exc}"))
                return

            snapshot = _read_snapshot(ld, task_id)
            if snapshot is None:
                self._send_json(404, _err("NOT_FOUND", f"Task {task_id} not found"))
                return

            events = read_task_events(ld, task_id)
            try:
                validate_comment_for_delete(events, comment_id)
            except ValueError as exc:
                self._send_json(400, _err("VALIDATION_ERROR", str(exc)))
                return

            event = create_event(
                type="comment_deleted",
                task_id=task_id,
                actor=actor,
                data={"comment_id": comment_id},
            )
            updated_snapshot = apply_event_to_snapshot(snapshot, event)

            try:
                write_task_event(ld, task_id, [event], updated_snapshot, config)
            except Exception as exc:
                self._send_json(500, _err("WRITE_ERROR", f"Failed to delete comment: {exc}"))
                return

            self._send_json(200, _ok(updated_snapshot))

        # ---------------------------------------------------------------
        # POST /api/tasks/<id>/react — Add Reaction
        # ---------------------------------------------------------------

        def _handle_post_task_react(self, ld: Path, task_id: str) -> None:
            """Handle POST /api/tasks/<id>/react — add an emoji reaction to a comment."""
            if not validate_id(task_id, "task"):
                self._send_json(400, _err("INVALID_ID", "Invalid task ID format"))
                return

            body = self._read_request_body()
            if body is None:
                return

            comment_id = body.get("comment_id")
            emoji = body.get("emoji")
            actor = body.get("actor", "dashboard:web")

            if not comment_id or not isinstance(comment_id, str):
                self._send_json(
                    400, _err("VALIDATION_ERROR", "Missing or invalid 'comment_id' field")
                )
                return

            if not emoji or not isinstance(emoji, str):
                self._send_json(400, _err("VALIDATION_ERROR", "Missing or invalid 'emoji' field"))
                return

            if not validate_emoji(emoji):
                self._send_json(
                    400,
                    _err(
                        "VALIDATION_ERROR",
                        f"Invalid emoji: '{emoji}'. Must be 1-50 alphanumeric/underscore/hyphen chars.",
                    ),
                )
                return

            if not validate_actor(actor):
                self._send_json(400, _err("VALIDATION_ERROR", f"Invalid actor format: '{actor}'"))
                return

            # Read config for hooks
            config_path = ld / "config.json"
            try:
                config = json.loads(config_path.read_text())
            except (json.JSONDecodeError, OSError) as exc:
                self._send_json(500, _err("READ_ERROR", f"Failed to read config: {exc}"))
                return

            snapshot = _read_snapshot(ld, task_id)
            if snapshot is None:
                self._send_json(404, _err("NOT_FOUND", f"Task {task_id} not found"))
                return

            events = read_task_events(ld, task_id)
            try:
                validate_comment_for_react(events, comment_id)
            except ValueError as exc:
                self._send_json(400, _err("VALIDATION_ERROR", str(exc)))
                return

            # Idempotency: check if actor already has this reaction
            comments = materialize_comments(events)
            for c in comments:
                if c["id"] == comment_id:
                    if actor in c.get("reactions", {}).get(emoji, []):
                        self._send_json(200, _ok(snapshot))
                        return
                    break
                for reply in c.get("replies", []):
                    if reply["id"] == comment_id:
                        if actor in reply.get("reactions", {}).get(emoji, []):
                            self._send_json(200, _ok(snapshot))
                            return
                        break

            event = create_event(
                type="reaction_added",
                task_id=task_id,
                actor=actor,
                data={"comment_id": comment_id, "emoji": emoji},
            )
            updated_snapshot = apply_event_to_snapshot(snapshot, event)

            try:
                write_task_event(ld, task_id, [event], updated_snapshot, config)
            except Exception as exc:
                self._send_json(500, _err("WRITE_ERROR", f"Failed to add reaction: {exc}"))
                return

            self._send_json(200, _ok(updated_snapshot))

        # ---------------------------------------------------------------
        # POST /api/tasks/<id>/unreact — Remove Reaction
        # ---------------------------------------------------------------

        def _handle_post_task_unreact(self, ld: Path, task_id: str) -> None:
            """Handle POST /api/tasks/<id>/unreact — remove an emoji reaction from a comment."""
            if not validate_id(task_id, "task"):
                self._send_json(400, _err("INVALID_ID", "Invalid task ID format"))
                return

            body = self._read_request_body()
            if body is None:
                return

            comment_id = body.get("comment_id")
            emoji = body.get("emoji")
            actor = body.get("actor", "dashboard:web")

            if not comment_id or not isinstance(comment_id, str):
                self._send_json(
                    400, _err("VALIDATION_ERROR", "Missing or invalid 'comment_id' field")
                )
                return

            if not emoji or not isinstance(emoji, str):
                self._send_json(400, _err("VALIDATION_ERROR", "Missing or invalid 'emoji' field"))
                return

            if not validate_emoji(emoji):
                self._send_json(
                    400,
                    _err(
                        "VALIDATION_ERROR",
                        f"Invalid emoji: '{emoji}'. Must be 1-50 alphanumeric/underscore/hyphen chars.",
                    ),
                )
                return

            if not validate_actor(actor):
                self._send_json(400, _err("VALIDATION_ERROR", f"Invalid actor format: '{actor}'"))
                return

            # Read config for hooks
            config_path = ld / "config.json"
            try:
                config = json.loads(config_path.read_text())
            except (json.JSONDecodeError, OSError) as exc:
                self._send_json(500, _err("READ_ERROR", f"Failed to read config: {exc}"))
                return

            snapshot = _read_snapshot(ld, task_id)
            if snapshot is None:
                self._send_json(404, _err("NOT_FOUND", f"Task {task_id} not found"))
                return

            events = read_task_events(ld, task_id)
            try:
                validate_comment_for_react(events, comment_id)
            except ValueError as exc:
                self._send_json(400, _err("VALIDATION_ERROR", str(exc)))
                return

            # Check the reaction exists for this actor
            comments = materialize_comments(events)
            found = False
            for c in comments:
                if c["id"] == comment_id:
                    if actor in c.get("reactions", {}).get(emoji, []):
                        found = True
                    break
                for reply in c.get("replies", []):
                    if reply["id"] == comment_id:
                        if actor in reply.get("reactions", {}).get(emoji, []):
                            found = True
                        break

            if not found:
                self._send_json(
                    404,
                    _err(
                        "NOT_FOUND", f"No '{emoji}' reaction by {actor} on comment {comment_id}."
                    ),
                )
                return

            event = create_event(
                type="reaction_removed",
                task_id=task_id,
                actor=actor,
                data={"comment_id": comment_id, "emoji": emoji},
            )
            updated_snapshot = apply_event_to_snapshot(snapshot, event)

            try:
                write_task_event(ld, task_id, [event], updated_snapshot, config)
            except Exception as exc:
                self._send_json(500, _err("WRITE_ERROR", f"Failed to remove reaction: {exc}"))
                return

            self._send_json(200, _ok(updated_snapshot))

        # ---------------------------------------------------------------
        # POST /api/tasks/<id>/open-notes — Open notes file in editor
        # ---------------------------------------------------------------

        def _handle_post_open_notes(self, ld: Path, task_id: str) -> None:
            """Handle POST /api/tasks/<id>/open-notes — open the notes file in the system default editor."""
            if not validate_id(task_id, "task"):
                self._send_json(400, _err("INVALID_ID", "Invalid task ID format"))
                return

            # Resolve notes path (check active, then archive)
            notes_path = ld / "notes" / f"{task_id}.md"
            if not notes_path.is_file():
                notes_path = ld / "archive" / "notes" / f"{task_id}.md"
            if not notes_path.is_file():
                self._send_json(404, _err("NOT_FOUND", f"No notes file for task {task_id}"))
                return

            # Security: ensure resolved path is within .lattice/
            resolved = notes_path.resolve()
            lattice_resolved = ld.resolve()
            if not str(resolved).startswith(str(lattice_resolved)):
                self._send_json(403, _err("FORBIDDEN", "Path traversal not allowed"))
                return

            # Open in system default editor
            system = platform.system()
            try:
                if system == "Darwin":
                    subprocess.Popen(["open", str(resolved)])
                elif system == "Linux":
                    subprocess.Popen(["xdg-open", str(resolved)])
                elif system == "Windows":
                    subprocess.Popen(["start", "", str(resolved)], shell=True)
                else:
                    self._send_json(500, _err("UNSUPPORTED", f"Unsupported platform: {system}"))
                    return
            except OSError as exc:
                self._send_json(500, _err("OPEN_ERROR", f"Failed to open file: {exc}"))
                return

            self._send_json(200, _ok({"opened": str(resolved)}))

        # ---------------------------------------------------------------
        # POST /api/tasks/<id>/open-plans — Open plan file in editor
        # ---------------------------------------------------------------

        def _handle_post_open_plans(self, ld: Path, task_id: str) -> None:
            """Handle POST /api/tasks/<id>/open-plans — open the plan file in the system default editor."""
            if not validate_id(task_id, "task"):
                self._send_json(400, _err("INVALID_ID", "Invalid task ID format"))
                return

            # Resolve plan path (check active, then archive, then scaffold)
            plan_path = ld / "plans" / f"{task_id}.md"
            if not plan_path.is_file():
                plan_path = ld / "archive" / "plans" / f"{task_id}.md"
            if not plan_path.is_file():
                # Scaffold a fresh plan file so the user lands in a useful template
                plan_path = ld / "plans" / f"{task_id}.md"
                snapshot = _read_snapshot(ld, task_id)
                if snapshot is None:
                    self._send_json(404, _err("NOT_FOUND", f"Task {task_id} not found"))
                    return
                title = snapshot.get("title", "Untitled")
                short_id = snapshot.get("short_id")
                description = snapshot.get("description")
                scaffold_plan(ld, task_id, title, short_id, description)

            # Security: ensure resolved path is within .lattice/
            resolved = plan_path.resolve()
            lattice_resolved = ld.resolve()
            if not str(resolved).startswith(str(lattice_resolved)):
                self._send_json(403, _err("FORBIDDEN", "Path traversal not allowed"))
                return

            # Open in system default editor
            system = platform.system()
            try:
                if system == "Darwin":
                    subprocess.Popen(["open", str(resolved)])
                elif system == "Linux":
                    subprocess.Popen(["xdg-open", str(resolved)])
                elif system == "Windows":
                    subprocess.Popen(["start", "", str(resolved)], shell=True)
                else:
                    self._send_json(500, _err("UNSUPPORTED", f"Unsupported platform: {system}"))
                    return
            except OSError as exc:
                self._send_json(500, _err("OPEN_ERROR", f"Failed to open file: {exc}"))
                return

            self._send_json(200, _ok({"opened": str(resolved)}))

    return LatticeHandler


# ---------------------------------------------------------------------------
# Activity helpers (module-level, stateless)
# ---------------------------------------------------------------------------


def _collect_events(ld: Path, *, full_scan: bool = False, tail_n: int = 10) -> list[dict]:
    """Read events from all JSONL files in the events directory.

    When *full_scan* is True, reads every line.  Otherwise reads the last
    *tail_n* lines from each file (fast path for the unfiltered default).
    Also scans archived events when doing a full scan.
    """
    all_events: list[dict] = []
    dirs = [ld / "events"]
    if full_scan:
        archive_events = ld / "archive" / "events"
        if archive_events.is_dir():
            dirs.append(archive_events)

    for events_dir in dirs:
        if not events_dir.is_dir():
            continue
        for event_file in events_dir.glob("*.jsonl"):
            if event_file.name == "_lifecycle.jsonl":
                continue
            try:
                lines = event_file.read_text().splitlines()
            except OSError:
                continue
            subset = lines if full_scan else lines[-tail_n:]
            for line in subset:
                line = line.strip()
                if line:
                    try:
                        all_events.append(json.loads(line))
                    except json.JSONDecodeError:
                        continue
    return all_events


def _build_facets(events: list[dict], ld: Path) -> dict:
    """Extract distinct types, actors, and tasks from a set of events."""
    from lattice.core.events import get_actor_display

    types: set[str] = set()
    actors: set[str] = set()
    task_ids: set[str] = set()

    for ev in events:
        if ev.get("type"):
            types.add(ev["type"])
        if ev.get("actor"):
            actors.add(get_actor_display(ev["actor"]))
        if ev.get("task_id"):
            task_ids.add(ev["task_id"])

    # Build task info list with short_id and title from snapshots
    task_info: list[dict] = []
    for tid in sorted(task_ids):
        info: dict = {"id": tid}
        # Try active snapshot
        snap_path = ld / "tasks" / f"{tid}.json"
        if not snap_path.is_file():
            snap_path = ld / "archive" / "tasks" / f"{tid}.json"
        if snap_path.is_file():
            try:
                snap = json.loads(snap_path.read_text())
                info["short_id"] = snap.get("short_id")
                info["title"] = snap.get("title")
            except (json.JSONDecodeError, OSError):
                pass
        task_info.append(info)

    return {
        "types": sorted(types),
        "actors": sorted(actors),
        "tasks": task_info,
    }


def _apply_activity_filters(
    events: list[dict],
    *,
    type_filter: str | None = None,
    task_filter: str | None = None,
    actor_filter: str | None = None,
    after: str | None = None,
    before: str | None = None,
    search: str | None = None,
) -> list[dict]:
    """Apply filter chain to a list of events. All filters are AND-combined."""
    result = events

    if type_filter:
        allowed = {t.strip() for t in type_filter.split(",")}
        result = [e for e in result if e.get("type") in allowed]

    if task_filter:
        result = [e for e in result if e.get("task_id") == task_filter]

    if actor_filter:
        from lattice.core.events import get_actor_display

        result = [
            e for e in result if e.get("actor") and get_actor_display(e["actor"]) == actor_filter
        ]

    if after:
        result = [e for e in result if (e.get("ts") or "") > after]

    if before:
        result = [e for e in result if (e.get("ts") or "") < before]

    if search:
        search_lower = search.lower()

        def _matches(ev: dict) -> bool:
            from lattice.core.events import get_actor_display

            # Search in event data values (comment bodies, field values, etc.)
            data = ev.get("data") or {}
            for v in data.values():
                if isinstance(v, str) and search_lower in v.lower():
                    return True
            # Also search in actor and type
            actor_str = get_actor_display(ev["actor"]) if ev.get("actor") else ""
            if search_lower in actor_str.lower():
                return True
            if search_lower in (ev.get("type") or "").lower():
                return True
            return False

        result = [e for e in result if _matches(e)]

    return result


# ---------------------------------------------------------------------------
# File-reading helpers (no locking needed — read-only)
# ---------------------------------------------------------------------------


def _read_snapshot(ld: Path, task_id: str) -> dict | None:
    path = ld / "tasks" / f"{task_id}.json"
    if not path.is_file():
        return None
    try:
        return json.loads(path.read_text())
    except (json.JSONDecodeError, OSError):
        return None


def _read_snapshot_archive(ld: Path, task_id: str) -> dict | None:
    path = ld / "archive" / "tasks" / f"{task_id}.json"
    if not path.is_file():
        return None
    try:
        return json.loads(path.read_text())
    except (json.JSONDecodeError, OSError):
        return None


def _read_artifact_info(ld: Path, snapshot: dict) -> list[dict]:
    artifacts: list[dict] = []
    # Read from evidence_refs (new) with fallback to artifact_refs (legacy)
    refs = _get_artifact_evidence_refs(snapshot)
    for art_id, role in refs:
        meta_path = ld / "artifacts" / "meta" / f"{art_id}.json"
        info: dict = {"id": art_id, "role": role}
        if meta_path.is_file():
            try:
                meta = json.loads(meta_path.read_text())
                info["title"] = meta.get("title")
                info["type"] = meta.get("type")
            except (json.JSONDecodeError, OSError):
                pass
        artifacts.append(info)
    return artifacts


def _get_artifact_evidence_refs(snapshot: dict) -> list[tuple[str, str | None]]:
    """Extract (artifact_id, role) pairs from evidence_refs or legacy artifact_refs."""
    evidence_refs = snapshot.get("evidence_refs")
    if evidence_refs is not None:
        return [
            (ref["id"], ref.get("role"))
            for ref in evidence_refs
            if ref.get("source_type") == "artifact"
        ]
    # Legacy fallback
    result = []
    for ref in snapshot.get("artifact_refs", []):
        if isinstance(ref, dict):
            result.append((ref["id"], ref.get("role")))
        else:
            result.append((ref, None))
    return result


# ---------------------------------------------------------------------------
# Server factory
# ---------------------------------------------------------------------------


def create_server(
    lattice_dir: Path, host: str, port: int, *, readonly: bool = False
) -> HTTPServer:
    """Create an HTTP server bound to *host*:*port* serving the Lattice dashboard.

    Parameters
    ----------
    lattice_dir:
        Path to the ``.lattice/`` directory (not the project root).
    host:
        Bind address (e.g. ``"127.0.0.1"``).
    port:
        TCP port to listen on.
    readonly:
        If ``True``, all POST requests return 403 FORBIDDEN.
    """
    handler_cls = _make_handler_class(lattice_dir, readonly=readonly)
    server = HTTPServer((host, port), handler_cls)
    return server
