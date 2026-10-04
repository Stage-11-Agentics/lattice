"""HTTP server for the Lattice dashboard.

Reads are answered by ``dashboard/api.py``; every write is a named operation
run through the board (``board.execute``), so the dashboard obeys the CLI's
rules and stamps a browser origin (SPEC §4, §10). POSTs must be same-origin
JSON: ``Content-Type: application/json`` and an ``Origin`` equal to the
served host.

The server is threaded, but one lock (:data:`_BOARD_LOCK`) runs every POST
and every GET other than issue media and ``/api/boot`` one at a time, as a
single-threaded server did. Only issue media GETs (``dashboard/media.py``,
LAT-366) run beside board operations, so a video held open by a browser never
stalls the board.
"""

from __future__ import annotations

import ipaddress
import json
import os
import platform
import secrets
import socket
import subprocess
import sys
import threading
import time
from collections.abc import Callable
from contextlib import AbstractContextManager, nullcontext
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, HTTPServer, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from lattice.boards import LocalBoard, browser_reported_origin
from lattice.core.errors import OpError
from lattice.core.ids import validate_id
from lattice.dashboard import api, media, media_prep
from lattice.dashboard.api import (
    MAX_ISSUE_FILE_MEDIA_ITEMS,
    MAX_REQUEST_BODY_BYTES,
    ApiError,
    ApiResponse,
)
from lattice.core.plans import scaffold_plan_text
from lattice.storage.operations import resolve_task_prose_path

__all__ = ["MAX_REQUEST_BODY_BYTES", "STATIC_DIR", "create_server", "origin_allowed"]

# A file request can contain base64 media and up to eight derived JPEG frames
# per video. Keep its allowance separate from ordinary dashboard writes and
# hard-bound it even when the board owner raises the media settings.
MAX_ISSUE_FILE_BODY_BYTES = 2 * 1024 * 1024 * 1024
_POST_BODY_FAILED = object()

# A bound checkout exposes issue metadata, but the cached board is read-only.
_BOUND_CHECKOUT_ISSUES_READ_ONLY = (
    400,
    "LOCAL_ONLY",
    "This bound checkout is read-only. File and comment on the hosted dashboard or with 'lattice issue'.",
)


def issue_file_body_limit(lattice_dir: Path) -> int:
    """Bound quick-file JSON from configured media limits and frame overhead."""
    from lattice.core.issue_media import MAX_FRAME_BYTES, MAX_FRAMES, media_limits

    config = api.get_config(lattice_dir)
    _per_file, per_issue = media_limits(config)
    decoded_allowance = per_issue + MAX_ISSUE_FILE_MEDIA_ITEMS * MAX_FRAMES * MAX_FRAME_BYTES
    base64_allowance = ((decoded_allowance + 2) // 3) * 4
    # Payload object keys, filenames, hashes, dimensions and frame timestamps.
    return min(MAX_ISSUE_FILE_BODY_BYTES, base64_allowance + 1024 * 1024)


STATIC_DIR = Path(__file__).parent / "static"

_LOOPBACK_NAMES = frozenset({"localhost", "127.0.0.1", "::1"})

_HEADER_READ_TIMEOUT = 1.0
WRITE_DRAIN_TIMEOUT = 5.0
_PROCESS_BOOT_ID = secrets.token_urlsafe(18)
_PROCESS_PID = os.getpid()

#: Held around every request but issue media GETs: the board sees one request
#: at a time, as it did before the server was threaded.
_BOARD_LOCK = threading.Lock()


class _RestartAwareHTTPServer(ThreadingHTTPServer):
    """Threaded server that drains admitted writes before a local restart."""

    daemon_threads = True
    block_on_close = False

    def __init__(self, address: tuple[str, int], handler: type[BaseHTTPRequestHandler]):
        self.boot_id = _PROCESS_BOOT_ID
        self.pid = _PROCESS_PID
        self._restart_condition = threading.Condition()
        self._request_states: dict[socket.socket, str] = {}
        self._draining = False
        super().__init__(address, handler)

    def get_request(self) -> tuple[socket.socket, Any]:
        request, client_address = super().get_request()
        request.settimeout(_HEADER_READ_TIMEOUT)
        return request, client_address

    def process_request(self, request: socket.socket, client_address: Any) -> None:
        with self._restart_condition:
            self._request_states[request] = "unparsed"
            self._restart_condition.notify_all()
        try:
            super().process_request(request, client_address)
        except BaseException:
            with self._restart_condition:
                self._request_states.pop(request, None)
                self._restart_condition.notify_all()
            raise

    def process_request_thread(self, request: socket.socket, client_address: Any) -> None:
        try:
            super().process_request_thread(request, client_address)
        finally:
            with self._restart_condition:
                self._request_states.pop(request, None)
                self._restart_condition.notify_all()

    def admit_write(self, request: socket.socket) -> bool:
        """Admit a parsed POST, or mark it for an explicit restart refusal."""
        with self._restart_condition:
            if self._draining:
                self._request_states[request] = "refused"
                self._restart_condition.notify_all()
                return False
            self._request_states[request] = "write"
            self._restart_condition.notify_all()
            return True

    def mark_read(self, request: socket.socket) -> None:
        with self._restart_condition:
            self._request_states[request] = "read"
            self._restart_condition.notify_all()

    def finish_write_response(self, request: socket.socket) -> None:
        """Release a write only after BaseHTTPRequestHandler flushed its reply."""
        with self._restart_condition:
            state = self._request_states.get(request)
            if state in {"write", "refused"}:
                self._request_states[request] = "read"
            self._restart_condition.notify_all()

    @staticmethod
    def _close_connection(request: socket.socket) -> None:
        try:
            request.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        try:
            request.close()
        except OSError:
            pass

    def begin_write_drain(self, timeout: float = WRITE_DRAIN_TIMEOUT) -> tuple[bool, int]:
        """Stop admitting writes, close nonwrites, and wait a bounded time."""
        deadline = time.monotonic() + timeout
        header_deadline = min(deadline, time.monotonic() + _HEADER_READ_TIMEOUT)
        with self._restart_condition:
            self._draining = True
            self._restart_condition.notify_all()
            active_reads = [
                request for request, state in self._request_states.items() if state == "read"
            ]
        for request in active_reads:
            self._close_connection(request)

        with self._restart_condition:
            while True:
                now = time.monotonic()
                unparsed = [
                    request
                    for request, state in self._request_states.items()
                    if state == "unparsed"
                ]
                pending = sum(
                    state in {"write", "refused"} for state in self._request_states.values()
                )
                if not unparsed and pending == 0:
                    return True, 0
                if unparsed and now >= header_deadline:
                    for request in unparsed:
                        if self._request_states.get(request) == "unparsed":
                            self._request_states[request] = "closing"
                    to_close = unparsed
                else:
                    to_close = []
                if pending and now >= deadline:
                    self._draining = False
                    self._restart_condition.notify_all()
                    return False, pending
                if to_close:
                    for request in to_close:
                        self._close_connection(request)
                    continue
                wake_at = deadline if pending else header_deadline
                self._restart_condition.wait(max(0, wake_at - now))


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


def host_allowed(host_header: str | None, bound_host: str) -> bool:
    """Apply the dashboard's Host check only to loopback-bound servers.

    A network bind is deliberately reachable through the address or hostname
    the client uses. For a loopback bind, the Host must still name loopback to
    block DNS rebinding to the local dashboard.
    """
    if not _is_loopback(bound_host):
        return True
    if not host_header:
        return False
    return _is_loopback(_host_name(host_header))


def _is_issue_api_path(path: str) -> bool:
    return path == "/api/issues" or path.startswith("/api/issues/")


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
    ``read_dir``: the ``.lattice/`` reads use (default: the board's).
    ``reading``: a context manager held around each read, yielding that
    directory (a bound checkout takes the cache's shared read lock).
    """

    board: Any
    browser_actor: Callable[[], str] | None = None
    hosted: bool = False
    read_dir: Path | None = None
    reading: Callable[[], AbstractContextManager[Path]] | None = None

    @property
    def lattice_dir(self) -> Path:
        return self.read_dir if self.read_dir is not None else self.board.lattice_dir

    def read(self) -> AbstractContextManager[Path]:
        return self.reading() if self.reading is not None else nullcontext(self.lattice_dir)

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

        def handle_one_request(self) -> None:
            self._restart_write_admitted = False
            self._restart_write_refused = False
            try:
                super().handle_one_request()
            finally:
                if self._restart_write_admitted or self._restart_write_refused:
                    self.server.finish_write_response(self.connection)

        def parse_request(self) -> bool:
            parsed = super().parse_request()
            if not parsed:
                return False
            self.connection.settimeout(media.SOCKET_TIMEOUT)
            if self.command == "POST":
                self._restart_write_admitted = self.server.admit_write(self.connection)
                self._restart_write_refused = not self._restart_write_admitted
            else:
                self.server.mark_read(self.connection)
            return True

        def do_GET(self) -> None:  # noqa: N802
            path = urlparse(self.path).path.rstrip("/") or "/"
            if not host_allowed(self.headers.get("Host"), self.server.server_address[0]):
                self._send_error(403, "FORBIDDEN", "Non-loopback Host refused")
                return
            if path == "/api/boot":
                self._send(
                    api.ok(
                        {"pid": self.server.pid, "boot_id": self.server.boot_id},
                        headers={"Cache-Control": "no-store"},
                    )
                )
                return
            if media.MEDIA_ROUTE.fullmatch(path):
                self.connection.settimeout(media.SOCKET_TIMEOUT)
                media.serve_issue_media(self, self._target, path)
                return
            with _BOARD_LOCK:
                self._do_get()

        def do_POST(self) -> None:  # noqa: N802
            if self._restart_write_refused:
                self.close_connection = True
                self._send_error(
                    503,
                    "RESTARTING",
                    "Dashboard restart is draining writes; retry this request.",
                )
                return
            # The body is read before the board lock is taken, so a client that
            # stalls mid-upload holds only its own connection, never the board.
            self.connection.settimeout(media.SOCKET_TIMEOUT)
            prepared = self._prepare_post()
            if prepared is None:
                return  # error already sent
            path, body = prepared
            with _BOARD_LOCK:
                self._do_post(path, body)

        def _do_get(self) -> None:
            parsed = urlparse(self.path)
            path = parsed.path.rstrip("/") or "/"

            if path == "/":
                self._serve_static("index.html", "text/html")
            elif path == "/favicon.ico":
                self._serve_static("favicon.ico", "image/png")
            elif path == "/stats-demo":
                self._serve_notes_file("stats-demo/demo.html", "text/html")
            elif path == "/api/head":
                self._send_head()
            elif path == "/api/git" or path.startswith("/api/git/"):
                # git only (SPEC §9.4): it reads no board file, so it runs
                # without the cache's read lock and never under it.
                response = api.route_get(
                    self._target.lattice_dir, path, parsed.query, self.headers.get("If-None-Match")
                )
                self._send(response)
            elif path.startswith("/api/"):
                try:
                    with self._target.read() as ld:
                        response = api.route_get(
                            ld,
                            path,
                            parsed.query,
                            self.headers.get("If-None-Match"),
                            issue_media_route=(
                                None if self._target.hosted else "/api/issues/{issue_id}/media"
                            ),
                        )
                        if (
                            path == "/api/config"
                            and self._target.hosted
                            and response.envelope is not None
                            and response.envelope.get("ok") is True
                        ):
                            response.envelope["data"]["dashboard_mode"] = {"bound_checkout": True}
                except OpError as exc:  # a bound checkout's cache cannot be read
                    refused = ApiError.from_op_error(exc)
                    response = ApiResponse(refused.status, refused.envelope())
                except Exception as exc:  # nor can a path in it (maybe wrapped)
                    from lattice.remote.cache_paths import cache_access_error

                    mapped = cache_access_error(exc)
                    if mapped is None:
                        raise
                    refused = ApiError.from_op_error(mapped)
                    response = ApiResponse(refused.status, refused.envelope())
                self._send(response)
            elif path.startswith("/static/"):
                rel_path = path[len("/static/") :]
                if ".." in rel_path or rel_path.startswith("/"):
                    self._send_error(403, "FORBIDDEN", "Path traversal not allowed")
                    return
                ext = "." + rel_path.rsplit(".", 1)[-1] if "." in rel_path else ""
                self._serve_static(rel_path, _STATIC_TYPES.get(ext, "application/octet-stream"))
            else:
                self._send_error(404, "NOT_FOUND", f"Not found: {path}")

        def _prepare_post(self) -> tuple[str, Any] | None:
            """Check a POST's headers and read its body; ``None`` once an error is sent."""
            path = urlparse(self.path).path.rstrip("/") or "/"
            if self._readonly:
                self._send_error(403, "FORBIDDEN", "Dashboard is in read-only mode")
                return None

            if not path.startswith("/api/"):
                self._send_error(404, "NOT_FOUND", f"Not found: {path}")
                return None

            origin = self.headers.get("Origin")
            if not origin_allowed(origin, self.headers.get("Host"), self.server.server_address[0]):
                self._send_error(
                    403,
                    "FORBIDDEN",
                    f"Cross-origin request refused: Origin {origin!r} is not this dashboard.",
                )
                return None
            if self.headers.get_content_type() != "application/json":
                self._send_error(
                    415, "VALIDATION_ERROR", "POST requires Content-Type: application/json"
                )
                return None

            if _is_issue_api_path(path):
                if self._target.hosted:
                    self._send_error(*_BOUND_CHECKOUT_ISSUES_READ_ONLY)
                    return None
                try:
                    with self._target.read() as ld:
                        api._require_issues_enabled(ld)
                except ApiError as exc:
                    self._send(ApiResponse(exc.status, exc.envelope()))
                    return None

            body = self._read_request_body(path)
            if body is _POST_BODY_FAILED:
                return None  # error already sent
            try:
                api.validate_json_post_body(path, body)
            except ApiError as exc:
                self._send(ApiResponse(exc.status, exc.envelope()))
                return None

            if path == "/api/issues":
                try:
                    # The cheap checks (title, text size, item count) run before the
                    # slow media step, so a request that will be refused costs no ffmpeg.
                    api.translate_post(path, body)
                    if isinstance(body, dict) and isinstance(body.get("media"), list):
                        # Strip metadata and derive frames as the CLI does (can take a while).
                        body = {**body, "media": media_prep.prepare_issue_media(body["media"])}
                except ApiError as exc:
                    self._send(ApiResponse(exc.status, exc.envelope()))
                    return None
                except Exception as exc:  # noqa: BLE001 - the page gets an envelope, never a reset
                    self._send_error(500, "WRITE_ERROR", f"Could not prepare the media: {exc}")
                    return None
            return path, body

        def _do_post(self, path: str, body: Any) -> None:
            route = api.match_json_post_route(path)
            if route in api._TASK_OPEN_ROUTE_TEMPLATES:
                task_id, sub = path[len("/api/tasks/") :].rsplit("/", 1)
                self._open_prose(task_id, "notes" if sub == "open-notes" else "plan")
                return

            try:
                request = api.translate_post(path, body)
            except ApiError as exc:
                self._send(ApiResponse(exc.status, exc.envelope()))
                return
            self._execute(request)

        def _send_head(self) -> None:
            """``GET /api/head``: the bound cache's ``{"epoch", "seq"}``, which the page
            polls every second to refetch as soon as the embedded follower moves
            the cache (SPEC §10). A local board has no journal: ``{"head": null}``,
            and the page keeps its 5-second poll."""
            if not self._target.hosted:
                self._send(api.ok({"head": None}))
                return
            try:
                with self._target.read() as ld:
                    try:
                        state = json.loads((ld / "cache" / "state.json").read_text())
                    except (OSError, ValueError):
                        state = {}
            except OpError as exc:
                refused = ApiError.from_op_error(exc)
                self._send(ApiResponse(refused.status, refused.envelope()))
                return
            head = None
            if state.get("epoch") is not None and isinstance(state.get("head_seq"), int):
                head = {"epoch": state["epoch"], "seq": state["head_seq"]}
            self._send(api.ok({"head": head}))

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

        def _read_request_body(self, path: str) -> Any:
            """Read and parse JSON; ``_POST_BODY_FAILED`` means an error was sent."""
            try:
                content_length = int(self.headers.get("Content-Length", 0))
            except (TypeError, ValueError):
                self._send_error(400, "BAD_REQUEST", "Missing or invalid Content-Length")
                return _POST_BODY_FAILED
            if content_length <= 0:
                message = (
                    "Empty request body"
                    if content_length == 0
                    else "Missing or invalid Content-Length"
                )
                self._send_error(400, "BAD_REQUEST", message)
                return _POST_BODY_FAILED
            body_limit = MAX_REQUEST_BODY_BYTES
            if path == "/api/issues" and not self._target.hosted:
                try:
                    body_limit = issue_file_body_limit(self._target.lattice_dir)
                except ApiError as exc:
                    self._send(ApiResponse(exc.status, exc.envelope()))
                    return _POST_BODY_FAILED
            if content_length > body_limit:
                self._send_error(
                    413,
                    "PAYLOAD_TOO_LARGE",
                    f"Request body exceeds {body_limit} bytes",
                )
                return _POST_BODY_FAILED
            try:
                return json.loads(self.rfile.read(content_length))
            except (json.JSONDecodeError, UnicodeDecodeError):
                self._send_error(400, "BAD_REQUEST", "Invalid JSON in request body")
                return _POST_BODY_FAILED
            except TimeoutError:
                self.close_connection = True
                self._send_error(408, "REQUEST_TIMEOUT", "Request body was not received in time")
                return _POST_BODY_FAILED

        def _run(self, request: api.WriteRequest, *, exists_ok: bool = False) -> Any:
            """Run *request*'s operation on the board as a browser write.

            Returns the ``OpResult``; on a refusal or failure, sends the error
            response and returns ``None``. With *exists_ok*, an ``if_absent``
            write that found the file already there returns ``True`` instead.
            """
            from lattice.boards import HostedBoard
            from lattice.ops import Caller

            board = self._target.board
            if isinstance(board, HostedBoard):
                from lattice.remote.session import forget_window_at_start

                # Each browser write waits once per outage, as a CLI command does
                # (SPEC §8.6), not by the window as it was when the dashboard started.
                forget_window_at_start(board.root)
            try:
                caller = Caller(
                    actor=self._author_for(request),
                    origin={"reported": browser_reported_origin()},
                )
                return board.execute(request.op_name, request.params, caller)
            except OpError as exc:
                if exists_ok and exc.details.get("reason") == "ALREADY_EXISTS":
                    return True
                refused = api.write_error(request, exc)
                self._send(ApiResponse(refused.status, refused.envelope()))
            except Exception as exc:  # noqa: BLE001 - the page gets an envelope, never a reset
                from lattice.remote.cache_paths import cache_access_error

                mapped = cache_access_error(exc)
                if mapped is not None:
                    refused = ApiError.from_op_error(mapped)
                    self._send(ApiResponse(refused.status, refused.envelope()))
                else:
                    self._send_error(500, "WRITE_ERROR", f"Failed to write: {exc}")
            return None

        def _author_for(self, request: api.WriteRequest) -> Any:
            """The actor for a write. Issues and comments come from the person at the
            dashboard, so with no actor sent they are the board's configured human
            (``default_actor: human:...``); everything else, and a board with no
            human configured, keeps ``dashboard:web``."""
            actor = self._target.actor_for(request.actor)
            if (
                request.actor is None
                and actor == api.DEFAULT_ACTOR
                and request.op_name in api.HUMAN_AUTHORED_OPS
            ):
                with self._target.read() as ld:
                    return api.human_author(api.get_config(ld)) or actor
            return actor

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
    return _RestartAwareHTTPServer((host, port), handler_cls)
