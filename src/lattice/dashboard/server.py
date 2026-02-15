"""HTTP server for the Lattice dashboard."""

from __future__ import annotations

import json
import sys
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from lattice.core.config import serialize_config, validate_status, validate_transition
from lattice.core.events import create_event, serialize_event
from lattice.core.ids import validate_actor, validate_id
from lattice.core.tasks import apply_event_to_snapshot, compact_snapshot, serialize_snapshot
from lattice.storage.fs import atomic_write, jsonl_append
from lattice.storage.locks import multi_lock

STATIC_DIR = Path(__file__).parent / "static"

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


def _make_handler_class(lattice_dir: Path) -> type:
    """Create a handler class bound to a specific .lattice/ directory."""

    class LatticeHandler(BaseHTTPRequestHandler):
        _lattice_dir: Path = lattice_dir

        # Suppress default access logging to stdout; send to stderr instead
        def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
            sys.stderr.write(f"{self.address_string()} - {format % args}\n")

        def do_GET(self) -> None:  # noqa: N802
            parsed = urlparse(self.path)
            path = parsed.path.rstrip("/") or "/"

            if path == "/":
                self._serve_static("index.html", "text/html")
            elif path.startswith("/api/"):
                self._route_api(path)
            else:
                self._send_json(404, _err("NOT_FOUND", f"Not found: {path}"))

        def do_POST(self) -> None:  # noqa: N802
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
            elif path.startswith("/api/tasks/"):
                remainder = path[len("/api/tasks/") :]
                if "/" in remainder:
                    # /api/tasks/<id>/events
                    task_id, sub = remainder.rsplit("/", 1)
                    if sub == "events":
                        self._handle_task_events(ld, task_id)
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
            elif path.startswith("/api/tasks/"):
                remainder = path[len("/api/tasks/") :]
                if "/" in remainder:
                    task_id, sub = remainder.rsplit("/", 1)
                    if sub == "status":
                        self._handle_post_task_status(ld, task_id)
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

            # Enrich with notes_exists and artifacts
            if is_archived:
                notes_path = ld / "archive" / "notes" / f"{task_id}.md"
            else:
                notes_path = ld / "notes" / f"{task_id}.md"

            result = dict(snapshot)
            result["notes_exists"] = notes_path.exists()
            result["artifacts"] = _read_artifact_info(ld, snapshot)
            if is_archived:
                result["archived"] = True

            self._send_json(200, _ok(result))

        def _handle_task_events(self, ld: Path, task_id: str) -> None:
            if not validate_id(task_id, "task"):
                self._send_json(400, _err("INVALID_ID", "Invalid task ID format"))
                return

            events = _read_task_events(ld, task_id)
            if events is None:
                # Check archive
                events = _read_task_events_archive(ld, task_id)

            if events is None:
                events = []

            # Return newest first
            events.reverse()
            self._send_json(200, _ok(events))

        def _handle_activity(self, ld: Path) -> None:
            all_events: list[dict] = []
            events_dir = ld / "events"
            if events_dir.is_dir():
                for event_file in events_dir.glob("*.jsonl"):
                    if event_file.name == "_lifecycle.jsonl":
                        continue
                    # Tail: read last few events from each file
                    try:
                        lines = event_file.read_text().splitlines()
                    except OSError:
                        continue
                    # Take last 5 lines from each file
                    for line in lines[-5:]:
                        line = line.strip()
                        if line:
                            try:
                                all_events.append(json.loads(line))
                            except json.JSONDecodeError:
                                continue

            # Sort by (ts, id) descending
            all_events.sort(key=lambda e: (e.get("ts", ""), e.get("id", "")), reverse=True)
            # Return top 50
            self._send_json(200, _ok(all_events[:50]))

        def _handle_stats(self, ld: Path) -> None:
            tasks_dir = ld / "tasks"
            by_status: dict[str, int] = {}
            by_type: dict[str, int] = {}
            by_priority: dict[str, int] = {}
            total_active = 0

            if tasks_dir.is_dir():
                for task_file in tasks_dir.glob("*.json"):
                    try:
                        snap = json.loads(task_file.read_text())
                    except (json.JSONDecodeError, OSError):
                        continue
                    total_active += 1
                    status = snap.get("status", "unknown")
                    by_status[status] = by_status.get(status, 0) + 1
                    task_type = snap.get("type", "unknown")
                    by_type[task_type] = by_type.get(task_type, 0) + 1
                    priority = snap.get("priority", "unknown")
                    by_priority[priority] = by_priority.get(priority, 0) + 1

            # Count archived
            archive_dir = ld / "archive" / "tasks"
            total_archived = 0
            if archive_dir.is_dir():
                for _ in archive_dir.glob("*.json"):
                    total_archived += 1

            self._send_json(
                200,
                _ok(
                    {
                        "total_active": total_active,
                        "total_archived": total_archived,
                        "by_status": by_status,
                        "by_type": by_type,
                        "by_priority": by_priority,
                    }
                ),
            )

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
                    compact["archived"] = True
                    snapshots.append(compact)
            snapshots.sort(key=lambda s: s.get("id", ""))
            self._send_json(200, _ok(snapshots))

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

            # Read current snapshot with lock
            locks_dir = ld / "locks"
            lock_keys = [f"events_{task_id}", f"tasks_{task_id}"]
            lock_keys.sort()

            try:
                with multi_lock(locks_dir, lock_keys):
                    snapshot = _read_snapshot(ld, task_id)
                    if snapshot is None:
                        self._send_json(404, _err("NOT_FOUND", f"Task {task_id} not found"))
                        return

                    current_status = snapshot["status"]

                    if current_status == new_status:
                        self._send_json(
                            200,
                            _ok({"message": f"Already at status {new_status}"}),
                        )
                        return

                    if not validate_transition(config, current_status, new_status):
                        self._send_json(
                            400,
                            _err(
                                "INVALID_TRANSITION",
                                f"Invalid transition from {current_status} to {new_status}",
                            ),
                        )
                        return

                    # Create event
                    event = create_event(
                        type="status_changed",
                        task_id=task_id,
                        actor=actor,
                        data={"from": current_status, "to": new_status},
                    )

                    # Apply to snapshot
                    updated_snapshot = apply_event_to_snapshot(snapshot, event)

                    # Write event to per-task log
                    event_path = ld / "events" / f"{task_id}.jsonl"
                    jsonl_append(event_path, serialize_event(event))

                    # Materialize snapshot
                    snapshot_path = ld / "tasks" / f"{task_id}.json"
                    atomic_write(snapshot_path, serialize_snapshot(updated_snapshot))

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
            allowed_keys = {"background_image", "lane_colors"}
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

            # Validate background_image if present
            if "background_image" in body:
                bg = body["background_image"]
                if bg is not None and not isinstance(bg, str):
                    self._send_json(
                        400,
                        _err("VALIDATION_ERROR", "'background_image' must be a string or null"),
                    )
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

    return LatticeHandler


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


def _read_task_events(ld: Path, task_id: str) -> list[dict] | None:
    path = ld / "events" / f"{task_id}.jsonl"
    if not path.is_file():
        return None
    events: list[dict] = []
    try:
        for line in path.read_text().splitlines():
            line = line.strip()
            if line:
                try:
                    events.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
    except OSError:
        return None
    return events


def _read_task_events_archive(ld: Path, task_id: str) -> list[dict] | None:
    path = ld / "archive" / "events" / f"{task_id}.jsonl"
    if not path.is_file():
        return None
    events: list[dict] = []
    try:
        for line in path.read_text().splitlines():
            line = line.strip()
            if line:
                try:
                    events.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
    except OSError:
        return None
    return events


def _read_artifact_info(ld: Path, snapshot: dict) -> list[dict]:
    artifacts: list[dict] = []
    for art_id in snapshot.get("artifact_refs", []):
        meta_path = ld / "artifacts" / "meta" / f"{art_id}.json"
        info: dict = {"id": art_id}
        if meta_path.is_file():
            try:
                meta = json.loads(meta_path.read_text())
                info["title"] = meta.get("title")
                info["type"] = meta.get("type")
            except (json.JSONDecodeError, OSError):
                pass
        artifacts.append(info)
    return artifacts


# ---------------------------------------------------------------------------
# Server factory
# ---------------------------------------------------------------------------


def create_server(lattice_dir: Path, host: str, port: int) -> HTTPServer:
    """Create an HTTP server bound to *host*:*port* serving the Lattice dashboard.

    Parameters
    ----------
    lattice_dir:
        Path to the ``.lattice/`` directory (not the project root).
    host:
        Bind address (e.g. ``"127.0.0.1"``).
    port:
        TCP port to listen on.
    """
    handler_cls = _make_handler_class(lattice_dir)
    server = HTTPServer((host, port), handler_cls)
    return server
