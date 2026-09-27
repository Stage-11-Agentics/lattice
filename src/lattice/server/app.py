"""The HTTP API, protocol 1 (SPEC §8.4), as a Starlette app.

An operation request passes, in order and each before the next:

1. the protocol header (``PROTOCOL_MISMATCH``);
2. authentication and project visibility (401, then 403 before any existence
   lookup; 404 only for a token that may see the project);
3. the client version (``CLIENT_TOO_OLD``) and the JSON content type;
4. the per-token limits (in flight, op rate, body bytes), answered at once;
5. the body, read as it streams and refused the moment it passes
   ``max_body_bytes`` (a larger ``Content-Length`` is refused unread);
6. the envelope: ``op_id``, the operation (``UNKNOWN_OP``), its params
   (``UNSUPPORTED_PARAM`` for a parameter this server's operation lacks),
   ``origin.reported``, the ``task.event`` data cap, the string actor;
7. the disk floor (``STORAGE_LOW``);
8. admission: the project's ``asyncio.Lock``, awaited on the event loop with
   a timeout (``BOARD_BUSY``); a project that has never loaded loads here;
9. work: a worker thread takes the project's work lock and runs
   :meth:`Project.run_write`.

Nothing that can raise inside a worker reaches the event loop:
``BaseException`` is contained in the thread and answered with 500 (G-6).
Every response carries the three ``Lattice-*`` headers; every ``/v1``
response also carries ``Cache-Control: no-store``.
"""

from __future__ import annotations

import dataclasses
import json
import re
import time
import traceback
from collections.abc import Awaitable, Callable
from dataclasses import asdict
from pathlib import Path
from typing import Any

from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from lattice.core.errors import OpError
from lattice.core.events import BUILTIN_EVENT_TYPES
from lattice.core.ids import generate_op_id, validate_actor
from lattice.ops.base import Caller, OpResult, check_op_id, get_operation, parse_params
from lattice.ops.base import registered_operations as _registered_operations
from lattice.server import admin
from lattice.server.config import ServerConfig
from lattice.server.journal import fingerprint
from lattice.server.limits import DiskFloor, TokenLimits, check_event_data_cap
from lattice.server.log import ServerLog
from lattice.server.project import Project, WriteRequest
from lattice.server.protocol import (
    HEADER_CLIENT_VERSION,
    HEADER_MIN_CLIENT_VERSION,
    HEADER_PROTOCOL,
    HEADER_SERVER_VERSION,
    MIN_CLIENT_VERSION,
    PROTOCOL,
    is_older,
    server_version,
)
from lattice.server.registry import ProjectRegistry, WorkerCrash, in_worker
from lattice.server.tokens import TokenRecord, TokenStore
from lattice.storage.locks import LockTimeout

_ENVELOPE_KEYS = {"op_id", "params", "actor", "actor_name", "origin", "attestations", "expect"}
_REPORTED_KEYS = {"host", "os_user", "worktree", "branch", "client_version", "source"}
_REPORTED_CAP = 256
_WORKTREE_CAP = 1024
_CONTROL_RE = re.compile(r"[\x00-\x1f\x7f-\x9f]")


def error_status(exc: OpError) -> int:
    return exc.http_status


class ServerState:
    """Everything a request needs, built once per app."""

    def __init__(self, root: Path, config: ServerConfig, log: ServerLog) -> None:
        from lattice.core.ids import generate_instance_id

        self.root = Path(root)
        self.config = config
        self.log = log
        self.version = server_version()
        self.server_id = "srv_" + generate_instance_id().removeprefix("inst_")
        self.registry = ProjectRegistry(self.root, config, log, self.server_id)
        self.tokens = TokenStore(self.root, on_reload=self._tokens_reloaded)
        self.limits = TokenLimits(config.limits)
        self.disk = DiskFloor(self.root, config.limits.min_free_disk_bytes)

    def _tokens_reloaded(self, **fields: Any) -> None:
        self.log.info("config_reload", file="tokens.json", **fields)


# ---------------------------------------------------------------------------
# Responses
# ---------------------------------------------------------------------------


def envelope_ok(data: Any, status: int = 200) -> JSONResponse:
    return JSONResponse({"ok": True, "data": data}, status_code=status)


def envelope_error(exc: OpError) -> JSONResponse:
    details = dict(exc.details)
    retry_after = details.pop("retry_after", None)
    error: dict[str, Any] = {"code": exc.code, "message": exc.message}
    if details:
        error["details"] = details
    headers = {}
    if exc.code == "BOARD_BUSY":
        retry_after = retry_after or 2
    if retry_after is not None:
        headers["Retry-After"] = str(int(retry_after))
    return JSONResponse(
        {"ok": False, "error": error}, status_code=error_status(exc), headers=headers
    )


def internal_error() -> JSONResponse:
    return JSONResponse(
        {"ok": False, "error": {"code": "INTERNAL_ERROR", "message": "internal server error"}},
        status_code=500,
    )


class HeadersMiddleware:
    """The ``Lattice-*`` headers on every response; ``no-store`` on ``/v1``; request logs."""

    def __init__(self, app: ASGIApp, state: ServerState) -> None:
        self.app = app
        self.state = state

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        started = time.monotonic()
        path = scope.get("path", "")
        fields = scope.setdefault("state", {}).setdefault("log", {})
        status_holder = {"status": 500}
        extra = [
            (HEADER_SERVER_VERSION.encode(), self.state.version.encode()),
            (HEADER_MIN_CLIENT_VERSION.encode(), MIN_CLIENT_VERSION.encode()),
            (HEADER_PROTOCOL.encode(), str(PROTOCOL).encode()),
        ]
        if path.startswith("/v1"):
            extra.append((b"cache-control", b"no-store"))

        async def send_with_headers(message: Message) -> None:
            if message["type"] == "http.response.start":
                status_holder["status"] = message["status"]
                headers = [
                    (k, v) for k, v in message.get("headers", []) if k.lower() != b"cache-control"
                ]
                message = {**message, "headers": headers + extra}
            await send(message)

        try:
            await self.app(scope, receive, send_with_headers)
        finally:
            if path != "/healthz":
                level = fields.pop("_level", "info")
                self.state.log.emit(
                    level,
                    "request",
                    method=scope.get("method"),
                    path=path,
                    status=status_holder["status"],
                    duration_ms=round((time.monotonic() - started) * 1000, 1),
                    project=fields.get("project"),
                    op=fields.get("op"),
                    op_id=fields.get("op_id"),
                    token_id=fields.get("token_id"),
                    actor=fields.get("actor"),
                    seq=fields.get("seq"),
                    error_code=fields.get("error_code"),
                )


def endpoint(
    handler: Callable[[Request, ServerState], Awaitable[Response]],
) -> Callable[[Request], Awaitable[Response]]:
    """Wrap a handler: ``OpError`` → envelope; anything else → logged 500."""

    async def wrapped(request: Request) -> Response:
        state: ServerState = request.app.state.lattice
        log_fields = request.scope.setdefault("state", {}).setdefault("log", {})
        try:
            return await handler(request, state)
        except OpError as exc:
            log_fields["error_code"] = exc.code
            return envelope_error(exc)
        except LockTimeout as exc:
            log_fields["error_code"] = "BOARD_BUSY"
            return envelope_error(OpError("BOARD_BUSY", str(exc)))
        except WorkerCrash as exc:
            log_fields["error_code"] = "INTERNAL_ERROR"
            state.log.error(
                "op_crashed",
                exception=type(exc.original).__name__,
                message=str(exc.original)[:500],
                traceback="".join(traceback.format_exception(exc.original))[-4000:],
                **{k: log_fields.get(k) for k in ("project", "op", "op_id", "token_id")},
            )
            return internal_error()
        except Exception as exc:  # noqa: BLE001 - a bug must answer 500, never drop the loop
            log_fields["error_code"] = "INTERNAL_ERROR"
            state.log.error(
                "request_crashed",
                exception=type(exc).__name__,
                message=str(exc)[:500],
                traceback="".join(traceback.format_exception(exc))[-4000:],
            )
            return internal_error()

    return wrapped


# ---------------------------------------------------------------------------
# Request checks
# ---------------------------------------------------------------------------


def check_protocol(request: Request) -> None:
    sent = request.headers.get(HEADER_PROTOCOL)
    if sent is not None and sent.strip() != str(PROTOCOL):
        raise OpError(
            "PROTOCOL_MISMATCH",
            f"client speaks Lattice protocol {sent.strip()!r}; this server speaks {PROTOCOL}",
            {"client_protocol": sent.strip(), "server_protocol": PROTOCOL},
        )


def client_version(request: Request) -> str | None:
    value = request.headers.get(HEADER_CLIENT_VERSION)
    return value.strip()[:64] if value else None


def check_client_version(request: Request) -> None:
    version = client_version(request)
    if version is not None and is_older(version, MIN_CLIENT_VERSION):
        raise OpError(
            "CLIENT_TOO_OLD",
            f"this client runs Lattice {version}; the server ({server_version()}) needs at "
            f"least {MIN_CLIENT_VERSION}. Upgrade Lattice.",
            {"client_version": version, "min_client_version": MIN_CLIENT_VERSION},
        )


def authenticate(request: Request, state: ServerState) -> TokenRecord:
    token = state.tokens.authenticate(request.headers.get("authorization"))
    request.scope["state"]["log"]["token_id"] = token.id
    return token


def resolve_project(state: ServerState, token: TokenRecord, slug: str) -> Project:
    """403 for a token without the project (before any lookup); 404 for a missing one."""
    request_slug_ok = isinstance(slug, str) and admin.SLUG_RE.fullmatch(slug) is not None
    if not request_slug_ok or not token.permits_project(slug):
        raise OpError("FORBIDDEN", f"token {token.id} may not use project '{slug}'.")
    project = state.registry.get(slug)
    if project is None:
        raise OpError("NOT_FOUND", f"No project '{slug}' on this server.")
    return project


async def read_body(request: Request, state: ServerState, token: TokenRecord) -> bytes:
    """The body, refused (413) as soon as it passes ``max_body_bytes``; charges the
    token's byte bucket up front from ``Content-Length`` or as the bytes arrive."""
    limit = state.config.limits.max_body_bytes
    declared = request.headers.get("content-length")
    too_large = OpError(
        "PAYLOAD_TOO_LARGE", f"request body is over the server's limit of {limit} bytes"
    )
    if declared is not None:
        try:
            length = int(declared)
        except ValueError:
            raise OpError("VALIDATION_ERROR", "invalid Content-Length") from None
        if length > limit:
            raise too_large
        state.limits.take_bytes(token.id, length)
    received = bytearray()
    async for chunk in request.stream():
        received.extend(chunk)
        if len(received) > limit:
            raise too_large
        if declared is None:
            state.limits.take_bytes(token.id, len(chunk))
    return bytes(received)


def check_reported(reported: Any) -> dict:
    """SPEC §4: an object of strings from six keys, capped, with no control characters."""
    if reported is None:
        return {}
    if not isinstance(reported, dict):
        raise OpError("VALIDATION_ERROR", "origin.reported must be an object.")
    for key, value in reported.items():
        if key not in _REPORTED_KEYS:
            raise OpError("VALIDATION_ERROR", f"origin.reported has an unknown key {key!r}.")
        if not isinstance(value, str):
            raise OpError("VALIDATION_ERROR", f"origin.reported.{key} must be a string.")
        cap = _WORKTREE_CAP if key == "worktree" else _REPORTED_CAP
        if len(value) > cap:
            raise OpError("VALIDATION_ERROR", f"origin.reported.{key} is over {cap} characters.")
        if _CONTROL_RE.search(value):
            raise OpError("VALIDATION_ERROR", f"origin.reported.{key} holds a control character.")
    if "source" in reported and reported["source"] != "browser":
        raise OpError("VALIDATION_ERROR", "origin.reported.source may only be 'browser'.")
    return dict(reported)


def _versions_text(request: Request) -> str:
    return f"server runs Lattice {server_version()}; client runs {client_version(request) or 'unknown'}"


def parse_envelope(
    request: Request, state: ServerState, token: TokenRecord, op_name: str, raw: bytes
) -> tuple[WriteRequest, dict]:
    """Validate the op request body into a :class:`WriteRequest`."""
    try:
        body = json.loads(raw.decode("utf-8")) if raw else {}
    except (UnicodeDecodeError, ValueError):
        raise OpError("VALIDATION_ERROR", "request body is not valid JSON.") from None
    if not isinstance(body, dict):
        raise OpError("VALIDATION_ERROR", "request body must be a JSON object.")
    unknown = sorted(set(body) - _ENVELOPE_KEYS)
    if unknown:
        raise OpError("VALIDATION_ERROR", f"unknown request field(s): {', '.join(unknown)}.")
    log_fields = request.scope["state"]["log"]

    op_id = body.get("op_id")
    if op_id is None:
        op_id = generate_op_id()  # never deduplicated (SPEC §8.4)
    else:
        check_op_id(op_id)
    log_fields["op_id"] = op_id

    try:
        op_cls = get_operation(op_name)
    except OpError as exc:
        if exc.code != "UNKNOWN_OP":
            raise
        raise OpError(
            "UNKNOWN_OP",
            f"this server has no operation '{op_name}' ({_versions_text(request)}).",
            {
                "op": op_name,
                "server_version": server_version(),
                "client_version": client_version(request),
            },
        ) from None

    params_json = body.get("params", {})
    if params_json is None:
        params_json = {}
    try:
        params = parse_params(op_cls.Params, params_json, op_name=op_name)
    except OpError as exc:
        if exc.details.get("reason") != "UNKNOWN_PARAM":
            raise
        param = exc.details.get("param")
        raise OpError(
            "UNSUPPORTED_PARAM",
            f"operation {op_name} on this server does not support parameter '{param}' "
            f"({_versions_text(request)}).",
            {
                "op": op_name,
                "param": param,
                "server_version": server_version(),
                "client_version": client_version(request),
            },
        ) from None
    check_event_data_cap(op_name, params_json, state.config.limits.max_event_data_bytes)

    origin = body.get("origin") or {}
    if not isinstance(origin, dict):
        raise OpError("VALIDATION_ERROR", "origin must be an object.")
    reported = check_reported(origin.get("reported"))  # any client "authenticated" is dropped

    attestations = body.get("attestations") or {}
    if not isinstance(attestations, dict):
        raise OpError("VALIDATION_ERROR", "attestations must be an object.")
    expect = body.get("expect") or {}
    if not isinstance(expect, dict) or set(expect) - {"last_event_id"}:
        raise OpError("VALIDATION_ERROR", "expect may hold only last_event_id.")
    expect_last = expect.get("last_event_id")
    if expect_last is not None and not isinstance(expect_last, str):
        raise OpError("VALIDATION_ERROR", "expect.last_event_id must be a string.")

    actor = body.get("actor")
    actor_name = body.get("actor_name")
    if actor is not None and not isinstance(actor, str):
        raise OpError("VALIDATION_ERROR", "actor must be a string.")
    if actor_name is not None and not isinstance(actor_name, str):
        raise OpError("VALIDATION_ERROR", "actor_name must be a string.")
    if not getattr(op_cls, "no_actor", False) and actor_name is None:
        if actor is None:
            actor = token.default_actor
            if actor is None:
                raise OpError(
                    "MISSING_ACTOR",
                    f"no actor given, and token {token.id} has no single default actor; "
                    "pass --actor.",
                )
        if not validate_actor(actor):
            raise OpError(
                "INVALID_ACTOR",
                f"Invalid actor format: '{actor}'. "
                "Expected prefix:identifier (e.g., human:atin, agent:claude).",
            )
    log_fields["actor"] = actor if actor_name is None else f"name:{actor_name}"
    if actor is not None and actor_name is None:
        token.authorize_actor(actor)

    caller = Caller(
        actor=actor,
        actor_name=actor_name,
        origin={
            "op_id": op_id,
            "reported": reported,
            "authenticated": token.authenticated_origin(),
        },
        attestations=attestations,
        expect_last_event_id=expect_last,
    )
    fp = fingerprint(
        op_name, params_json, body.get("actor"), actor_name, attestations, expect_last
    )
    write = WriteRequest(
        op=op_name,
        params=params,
        caller=caller,
        token_id=token.id,
        fp=fp,
        authorize_identity=token.authorize_actor,
    )
    return write, body


def result_json(result: OpResult) -> dict:
    data = asdict(result)
    data.pop("paths", None)
    return data


# ---------------------------------------------------------------------------
# Handlers
# ---------------------------------------------------------------------------


async def healthz(request: Request, state: ServerState) -> Response:
    free = state.disk.free_bytes()
    ok = free >= state.config.limits.min_free_disk_bytes
    body = {
        "ok": ok,
        "version": state.version,
        "protocol": PROTOCOL,
        "disk_free_bytes": free,
        "projects": state.registry.counts(),
    }
    return JSONResponse(body, status_code=200 if ok else 503)


async def _with_token(
    request: Request,
    state: ServerState,
    fn: Callable[[TokenRecord], Awaitable[Response]],
) -> Response:
    check_protocol(request)
    token = authenticate(request, state)
    state.limits.enter(token.id)
    try:
        return await fn(token)
    finally:
        state.limits.leave(token.id)


def _visible_slugs(state: ServerState, token: TokenRecord) -> list[str]:
    return [s for s in state.registry.slugs() if token.permits_project(s)]


async def info(request: Request, state: ServerState) -> Response:
    async def run(token: TokenRecord) -> Response:
        ops = {
            name: sorted(f.name for f in dataclasses.fields(cls.Params) if f.init)
            for name, cls in _registered_operations().items()
        }
        audit = state.config.audit
        return envelope_ok(
            {
                "version": state.version,
                "protocol": PROTOCOL,
                "min_client_version": MIN_CLIENT_VERSION,
                "stream_heartbeat_seconds": state.config.stream.heartbeat_seconds,
                "identity": {
                    "token_id": token.id,
                    "user": token.user,
                    "machine": token.machine,
                    "actors": list(token.actors),
                    "default_actor": token.default_actor,
                    "browser_actor": token.browser_actor,
                },
                "projects": _visible_slugs(state, token),
                "ops": ops,
                "event_types": sorted(BUILTIN_EVENT_TYPES),
                "audit": {
                    "configured": audit.enabled,
                    "active": False,
                    "reason": "audit history is not available in this server version",
                },
            }
        )

    return await _with_token(request, state, run)


async def projects(request: Request, state: ServerState) -> Response:
    async def run(token: TokenRecord) -> Response:
        rows = []
        for slug in _visible_slugs(state, token):
            project = state.registry.get(slug)
            if project is None:
                continue
            code = None
            try:
                config = json.loads((project.board / "config.json").read_text(encoding="utf-8"))
                code = config.get("project_code")
            except (OSError, ValueError):
                pass
            head = project.journal.head_seq if project.journal is not None else None
            rows.append(
                {"slug": slug, "project_code": code, "head_seq": head, "state": project.state}
            )
        return envelope_ok({"projects": rows})

    return await _with_token(request, state, run)


async def op_request(request: Request, state: ServerState) -> Response:
    slug = request.path_params["slug"]
    op_name = request.path_params["op"]
    log_fields = request.scope["state"]["log"]
    log_fields.update(project=slug, op=op_name)
    check_protocol(request)
    token = authenticate(request, state)
    project = resolve_project(state, token, slug)
    check_client_version(request)
    content_type = request.headers.get("content-type", "")
    if content_type.split(";")[0].strip().lower() != "application/json":
        raise OpError(
            "VALIDATION_ERROR", "operation requests need Content-Type: application/json."
        )
    state.limits.enter(token.id)
    try:
        state.limits.take_op(token.id)
        raw = await read_body(request, state, token)
        write, _body = parse_envelope(request, state, token, op_name, raw)
        state.disk.check()

        def work() -> Any:
            with project.locked():
                project.admit()
                return project.run_write(write)

        async with state.registry.admitted(project):
            outcome = await in_worker(work)
        log_fields["seq"] = outcome.seq
        return envelope_ok(
            {
                "result": result_json(outcome.result),
                "seq": outcome.seq,
                "op_id": write.caller.origin["op_id"],
            }
        )
    finally:
        state.limits.leave(token.id)


def _task_payload(board: Path, raw_id: str, *, include_plan: bool = True) -> dict:
    from lattice.core.ids import is_short_id, validate_id
    from lattice.storage.operations import read_task_authority
    from lattice.storage.short_ids import resolve_short_id

    if validate_id(raw_id, "task"):
        task_id = raw_id
    elif is_short_id(raw_id):
        task_id = resolve_short_id(board, raw_id.upper())
        if task_id is None:
            raise OpError("NOT_FOUND", f"Short ID '{raw_id.upper()}' not found.")
    else:
        raise OpError("INVALID_ID", f"Invalid task ID format: '{raw_id}'.")
    authority = read_task_authority(board, task_id, allow_missing=True)
    if authority is None:
        raise OpError("NOT_FOUND", f"Task {task_id} not found.")
    plan = None
    if include_plan:
        base = board / "archive" if authority.location == "archived" else board
        plan_path = base / "plans" / f"{task_id}.md"
        try:
            plan = plan_path.read_text(encoding="utf-8")
        except OSError:
            plan = None
    return {"snapshot": authority.snapshot, "events": list(authority.events), "plan": plan}


async def task_read(request: Request, state: ServerState) -> Response:
    slug = request.path_params["slug"]
    request.scope["state"]["log"]["project"] = slug

    async def run(token: TokenRecord) -> Response:
        project = resolve_project(state, token, slug)
        raw_id = request.path_params["task_id"]
        data = await state.registry.run_locked(
            project, lambda: _task_payload(project.board, raw_id)
        )
        return envelope_ok(data)

    return await _with_token(request, state, run)


def _task_list(
    board: Path, status: str | None, assigned: str | None, archived: bool
) -> list[dict]:
    from lattice.core.stats import load_all_snapshots
    from lattice.core.tasks import compact_snapshot

    active, archived_rows = load_all_snapshots(board)
    rows = active + (archived_rows if archived else [])
    out = []
    for snap in rows:
        if status and snap.get("status") != status:
            continue
        if assigned and snap.get("assigned_to") != assigned:
            continue
        out.append(compact_snapshot(snap))
    return sorted(out, key=lambda s: s.get("id", ""))


async def task_list(request: Request, state: ServerState) -> Response:
    slug = request.path_params["slug"]
    request.scope["state"]["log"]["project"] = slug

    async def run(token: TokenRecord) -> Response:
        project = resolve_project(state, token, slug)
        q = request.query_params
        include_archived = q.get("include_archived", "").lower() in ("1", "true", "yes")
        data = await state.registry.run_locked(
            project,
            lambda: _task_list(
                project.board, q.get("status"), q.get("assigned"), include_archived
            ),
        )
        return envelope_ok({"tasks": data})

    return await _with_token(request, state, run)


async def not_found(request: Request, state: ServerState) -> Response:
    raise OpError("NOT_FOUND", f"no route {request.method} {request.url.path}")


# ---------------------------------------------------------------------------
# App
# ---------------------------------------------------------------------------


def create_app(root: Path, *, config: ServerConfig, log: ServerLog | None = None) -> ASGIApp:
    """The server app for *root*. Starts the prewarm and control poller on startup."""
    import contextlib
    import logging

    for name in ("uvicorn", "uvicorn.error", "uvicorn.access"):
        logger = logging.getLogger(name)
        logger.handlers = [logging.NullHandler()]
        logger.propagate = False

    state = ServerState(root, config, log or ServerLog(config.log_level))

    @contextlib.asynccontextmanager
    async def lifespan(app: Starlette):  # noqa: ANN202
        state.log.info(
            "startup",
            version=state.version,
            protocol=PROTOCOL,
            server_id=state.server_id,
            projects=len(state.registry.slugs()),
        )
        state.registry.start()
        try:
            yield
        finally:
            await state.registry.stop()
            state.log.info("shutdown", server_id=state.server_id)

    routes = [
        Route("/healthz", endpoint(healthz), methods=["GET"]),
        Route("/v1/info", endpoint(info), methods=["GET"]),
        Route("/v1/projects", endpoint(projects), methods=["GET"]),
        Route("/v1/projects/{slug}/ops/{op}", endpoint(op_request), methods=["POST"]),
        Route("/v1/projects/{slug}/tasks", endpoint(task_list), methods=["GET"]),
        Route("/v1/projects/{slug}/tasks/{task_id}", endpoint(task_read), methods=["GET"]),
        Route(
            "/{path:path}", endpoint(not_found), methods=["GET", "POST", "PUT", "DELETE", "PATCH"]
        ),
    ]
    app = Starlette(routes=routes, lifespan=lifespan)
    app.state.lattice = state
    return HeadersMiddleware(app, state)
