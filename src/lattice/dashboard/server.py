"""HTTP server for the Lattice dashboard.

Reads are answered by ``dashboard/api.py``; every write is a named operation
run through the board (``board.execute``), so the dashboard obeys the CLI's
rules and stamps a browser origin (SPEC §4, §10). POSTs must be same-origin
JSON: ``Content-Type: application/json`` and an ``Origin`` equal to the
served host.
"""

from __future__ import annotations

import ipaddress
import json
import platform
import subprocess
import sys
from collections.abc import Callable
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from lattice.boards import LocalBoard, browser_reported_origin
from lattice.core.errors import OpError
from lattice.core.ids import validate_id
from lattice.dashboard import api
from lattice.dashboard.api import MAX_REQUEST_BODY_BYTES, ApiError, ApiResponse
from lattice.core.plans import scaffold_plan_text
from lattice.storage.operations import resolve_task_prose_path

__all__ = ["MAX_REQUEST_BODY_BYTES", "STATIC_DIR", "create_server", "origin_allowed"]

STATIC_DIR = Path(__file__).parent / "static"

_LOOPBACK_NAMES = frozenset({"localhost", "127.0.0.1", "::1"})

_STATIC_TYPES = {
    ".js": "application/javascript",
    ".css": "text/css",
    ".json": "application/json",
    ".svg": "image/svg+xml",
    ".png": "image/png",
    ".ico": "image/png",
    ".html": "text/html",
}


# ---------------------------------------------------------------------------
# Request checks
# ---------------------------------------------------------------------------


def _is_loopback(host: str) -> bool:
    if host in _LOOPBACK_NAMES:
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def _host_name(host_header: str) -> str:
    """The hostname of a ``Host`` header value (``[::1]:8799`` -> ``::1``)."""
    if host_header.startswith("["):
        return host_header[1 : host_header.find("]")] if "]" in host_header else host_header
    return host_header.rsplit(":", 1)[0] if ":" in host_header else host_header


def origin_allowed(origin: str | None, host_header: str | None, bound_host: str) -> bool:
    """Whether a POST's ``Origin`` is the dashboard's own (SPEC §10).

    The origin must equal ``http://<Host>``, so another site's page cannot
    post here. When the server listens on loopback, the ``Host`` itself must
    name loopback too, so a hostile name re-pointed at 127.0.0.1 (DNS
    rebinding) is refused. A missing or ``null`` origin is refused.
    """
    if not origin or not host_header or origin != f"http://{host_header}":
        return False
    if _is_loopback(bound_host):
        return _is_loopback(_host_name(host_header))
    return True


# ---------------------------------------------------------------------------
# The board a dashboard serves
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class DashboardBoard:
    """The board behind a dashboard and how its writes are attributed.

    ``board``: a ``LocalBoard`` or ``HostedBoard`` (``execute``,
    ``lattice_dir``). ``browser_actor``: on a bound checkout, the actor every
    browser write acts as (SPEC §8.3), overriding any actor the request names;
    ``None`` locally, where a request's own actor, else ``dashboard:web``, is
    used. ``hosted``: a bound checkout, whose cache the dashboard only reads.
    """

    board: Any
    browser_actor: Callable[[], str] | None = None
    hosted: bool = False

    @property
    def lattice_dir(self) -> Path:
        return self.board.lattice_dir

    def actor_for(self, requested: Any) -> Any:
        if self.browser_actor is not None:
            return self.browser_actor()
        return api.DEFAULT_ACTOR if requested is None else requested


# ---------------------------------------------------------------------------
# Request handler
# ---------------------------------------------------------------------------


def _make_handler_class(target: DashboardBoard, *, readonly: bool = False) -> type:
    """Create a handler class bound to one dashboard board."""

    class LatticeHandler(BaseHTTPRequestHandler):
        _target: DashboardBoard = target
        _readonly: bool = readonly

        # Suppress default access logging to stdout; send to stderr instead
        def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
            sys.stderr.write(f"{self.address_string()} - {format % args}\n")

        def do_GET(self) -> None:  # noqa: N802
            parsed = urlparse(self.path)
            path = parsed.path.rstrip("/") or "/"

            if path == "/":
                self._serve_static("index.html", "text/html")
            elif path == "/favicon.ico":
                self._serve_static("favicon.ico", "image/png")
            elif path == "/stats-demo":
                self._serve_notes_file("stats-demo/demo.html", "text/html")
            elif path.startswith("/api/"):
                self._send(
                    api.route_get(
                        self._target.lattice_dir,
                        path,
                        parsed.query,
                        self.headers.get("If-None-Match"),
                    )
                )
            elif path.startswith("/static/"):
                rel_path = path[len("/static/") :]
                if ".." in rel_path or rel_path.startswith("/"):
                    self._send_error(403, "FORBIDDEN", "Path traversal not allowed")
                    return
                ext = "." + rel_path.rsplit(".", 1)[-1] if "." in rel_path else ""
                self._serve_static(rel_path, _STATIC_TYPES.get(ext, "application/octet-stream"))
            else:
                self._send_error(404, "NOT_FOUND", f"Not found: {path}")

        def do_POST(self) -> None:  # noqa: N802
            if self._readonly:
                self._send_error(403, "FORBIDDEN", "Dashboard is in read-only mode")
                return

            path = urlparse(self.path).path.rstrip("/") or "/"
            if not path.startswith("/api/"):
                self._send_error(404, "NOT_FOUND", f"Not found: {path}")
                return

            origin = self.headers.get("Origin")
            if not origin_allowed(origin, self.headers.get("Host"), self.server.server_address[0]):
                self._send_error(
                    403,
                    "FORBIDDEN",
                    f"Cross-origin request refused: Origin {origin!r} is not this dashboard.",
                )
                return
            if self.headers.get_content_type() != "application/json":
                self._send_error(
                    415, "VALIDATION_ERROR", "POST requires Content-Type: application/json"
                )
                return

            body = self._read_request_body()
            if body is None:
                return  # error already sent

            if path.startswith("/api/tasks/") and path.rsplit("/", 1)[-1] in (
                "open-notes",
                "open-plans",
            ):
                task_id, sub = path[len("/api/tasks/") :].rsplit("/", 1)
                self._open_prose(task_id, "notes" if sub == "open-notes" else "plan")
                return

            try:
                request = api.translate_post(path, body)
            except ApiError as exc:
                self._send(ApiResponse(exc.status, exc.envelope()))
                return
            self._execute(request)

        # ---------------------------------------------------------------
        # Responses
        # ---------------------------------------------------------------

        def _send(self, response: ApiResponse) -> None:
            data = response.body()
            self.send_response(response.status)
            if response.envelope is not None:
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Content-Length", str(len(data)))
            for name, value in response.headers.items():
                self.send_header(name, value)
            self.end_headers()
            if data:
                self.wfile.write(data)

        def _send_error(self, status: int, code: str, message: str) -> None:
            self._send(api.error(status, code, message))

        def _serve_static(self, filename: str, content_type: str) -> None:
            filepath = STATIC_DIR / filename
            if not filepath.is_file():
                self._send_error(404, "NOT_FOUND", f"Static file not found: {filename}")
                return
            self._send_bytes(filepath.read_bytes(), content_type, cache=False)

        def _serve_notes_file(self, relpath: str, content_type: str) -> None:
            """Serve a file from the repo's notes/ directory."""
            filepath = Path(self._target.lattice_dir).resolve().parent / "notes" / relpath
            if not filepath.is_file():
                self._send_error(404, "NOT_FOUND", f"File not found: {relpath}")
                return
            self._send_bytes(filepath.read_bytes(), content_type, cache=True)

        def _send_bytes(self, data: bytes, content_type: str, *, cache: bool) -> None:
            self.send_response(200)
            self.send_header("Content-Type", f"{content_type}; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            if not cache:
                self.send_header("Cache-Control", "no-cache")
            self.end_headers()
            self.wfile.write(data)

        # ---------------------------------------------------------------
        # Writes
        # ---------------------------------------------------------------

        def _read_request_body(self) -> Any:
            """Read and parse a JSON request body. Returns ``None`` on failure."""
            try:
                content_length = int(self.headers.get("Content-Length", 0))
            except (TypeError, ValueError):
                self._send_error(400, "BAD_REQUEST", "Missing or invalid Content-Length")
                return None
            if content_length == 0:
                self._send_error(400, "BAD_REQUEST", "Empty request body")
                return None
            if content_length > MAX_REQUEST_BODY_BYTES:
                self._send_error(
                    413,
                    "PAYLOAD_TOO_LARGE",
                    f"Request body exceeds {MAX_REQUEST_BODY_BYTES} bytes",
                )
                return None
            try:
                return json.loads(self.rfile.read(content_length))
            except (json.JSONDecodeError, UnicodeDecodeError):
                self._send_error(400, "BAD_REQUEST", "Invalid JSON in request body")
                return None

        def _run(self, request: api.WriteRequest, *, exists_ok: bool = False) -> Any:
            """Run *request*'s operation on the board as a browser write.

            Returns the ``OpResult``; on a refusal or failure, sends the error
            response and returns ``None``. With *exists_ok*, an ``if_absent``
            write that found the file already there returns ``True`` instead.
            """
            from lattice.ops import Caller

            try:
                caller = Caller(
                    actor=self._target.actor_for(request.actor),
                    origin={"reported": browser_reported_origin()},
                )
                return self._target.board.execute(request.op_name, request.params, caller)
            except OpError as exc:
                if exists_ok and exc.details.get("reason") == "ALREADY_EXISTS":
                    return True
                refused = api.write_error(request, exc)
                self._send(ApiResponse(refused.status, refused.envelope()))
            except Exception as exc:  # noqa: BLE001 - the page gets an envelope, never a reset
                self._send_error(500, "WRITE_ERROR", f"Failed to write: {exc}")
            return None

        def _execute(self, request: api.WriteRequest) -> None:
            result = self._run(request)
            if result is not None:
                status, data = request.render(result)
                self._send(api.ok(data, status))

        def _open_prose(self, task_id: str, kind: str) -> None:
            """Open a task's notes or plan file in the system's default editor.

            A missing plan of an active task is scaffolded first (through
            ``task.plan_write``), so the user lands in a useful template. A bound checkout's cache is read-only,
            so there is nothing to open there (``LOCAL_ONLY``).
            """
            if not validate_id(task_id, "task"):
                self._send_error(400, "INVALID_ID", "Invalid task ID format")
                return
            if self._target.hosted:
                self._send_error(
                    400,
                    "LOCAL_ONLY",
                    f"This board lives on a server; write the {kind} with "
                    f"'lattice {kind} write {task_id} --file <path>'.",
                )
                return
            ld = self._target.lattice_dir
            path, authority = resolve_task_prose_path(ld, task_id, kind)
            if path is None:
                if kind == "notes":
                    self._send_error(404, "NOT_FOUND", f"No notes file for task {task_id}")
                    return
                if authority.location != "active":
                    self._send_error(404, "NOT_FOUND", f"Task {task_id} not found")
                    return
                # A missing plan is scaffolded through the plan-write operation,
                # like any other dashboard write (SPEC §10), and only if it is
                # still missing under the task lock: a plan written since the
                # check above is opened, never replaced.
                snapshot = authority.snapshot
                scaffold = scaffold_plan_text(
                    snapshot.get("title", "Untitled"),
                    snapshot.get("short_id"),
                    snapshot.get("description"),
                )
                request = api.WriteRequest(
                    "task.plan_write",
                    {"task": task_id, "stdin": scaffold, "if_absent": True},
                    None,
                    lambda result: (200, result.value),
                    task_id,
                )
                if self._run(request, exists_ok=True) is None:
                    return
                path, _ = resolve_task_prose_path(ld, task_id, kind)
                if path is None:
                    self._send_error(404, "NOT_FOUND", f"Task {task_id} not found")
                    return

            resolved = path.resolve()
            if not resolved.is_relative_to(ld.resolve()):
                self._send_error(403, "FORBIDDEN", "Path traversal not allowed")
                return

            system = platform.system()
            commands = {
                "Darwin": ["open", str(resolved)],
                "Linux": ["xdg-open", str(resolved)],
                "Windows": ["start", "", str(resolved)],
            }
            if system not in commands:
                self._send_error(500, "UNSUPPORTED", f"Unsupported platform: {system}")
                return
            try:
                subprocess.Popen(commands[system], shell=system == "Windows")
            except OSError as exc:
                self._send_error(500, "OPEN_ERROR", f"Failed to open file: {exc}")
                return
            self._send(api.ok({"opened": str(resolved)}))

    return LatticeHandler


# ---------------------------------------------------------------------------
# Server factory
# ---------------------------------------------------------------------------


def create_server(
    lattice_dir: Path,
    host: str,
    port: int,
    *,
    readonly: bool = False,
    board: DashboardBoard | None = None,
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
    board:
        The board to serve and write through; default: the local board at
        *lattice_dir*.
    """
    if board is None:
        root = Path(lattice_dir).parent
        board = DashboardBoard(LocalBoard(root=root, start=root))
    handler_cls = _make_handler_class(board, readonly=readonly)
    return HTTPServer((host, port), handler_cls)
