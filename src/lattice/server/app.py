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

import asyncio
import contextlib
import dataclasses
import json
import re
import time
from collections.abc import Awaitable, Callable, Iterator
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
from lattice.ops.base import (
    MISSING_ACTOR_MESSAGE,
    Caller,
    check_op_id,
    check_path_component,
    get_operation,
    parse_params,
)
from lattice.ops.base import registered_operations as _registered_operations
from lattice.server import admin, dashboard, web
from lattice.server.dashboard import ReadMemos
from lattice.server.config import ServerConfig
from lattice.server.journal import fingerprint
from lattice.server.limits import DiskFloor, TokenLimits, check_event_data_cap
from lattice.server.log import ServerLog, exception_fields
from lattice.server.project import LOADED, LOADING, UNLOADED, Project, WriteRequest
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
from lattice.server.stream import (
    RAW_SEND,
    EventStream,
    Subscriber,
    journal_frame,
    parse_entry_id,
    reset_frame,
)
from lattice.server.syncstate import (
    check_file_path,
    entry_events,
    delta_body,
    fast_path_body,
    manifest_body,
    needs_reset,
    read_board_file,
    reset_body,
)
from lattice.server.sessions import SessionStore
from lattice.server.tokens import TokenRecord, TokenStore
from lattice.server.web import (
    CLEAR_COOKIE,
    WebAssets,
    page_headers,
    require_origin,
    session_auth,
    session_cookie,
)
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
        #: Dashboard sessions, the page and its CSP, and the read memos (SPEC §10).
        self.sessions = SessionStore(self.root, self.tokens, on_error=self._sessions_failed)
        self.web = WebAssets()
        self.dashboard_memos = ReadMemos()
        self.limits = TokenLimits(config.limits)
        self.disk = DiskFloor(self.root, config.limits.min_free_disk_bytes)
        #: The stream heartbeat period; ``server.json`` sets it, and in-process
        #: test servers may lower it below a second (``/v1/info`` reports it).
        self.heartbeat_seconds: float = config.stream.heartbeat_seconds
        #: The event loop serving requests (set at startup); streams are woken on it.
        self.loop: asyncio.AbstractEventLoop | None = None
        #: Pending operations, ``(slug, token_id, op_id) -> count`` (SPEC §8.6, op
        #: status): joined once a request carrying a client ``op_id`` passed the
        #: per-token limits, before admission; left after its finish step recorded
        #: the commit, or after rollback, rejection, or a lock timeout. A count, so
        #: a retry queued behind its own first attempt keeps the pair pending.
        #: Touched only on the event loop.
        self.pending_ops: dict[tuple[str, str, str], int] = {}
        #: The descriptor limit ``serve`` set at startup (``before``, ``soft``,
        #: ``hard``), logged on the startup line; ``None`` in an in-process server.
        self.fd_limit: dict[str, int | None] | None = None

    def _sessions_failed(self, **fields: Any) -> None:
        self.log.emit("error", "config_reload", file="web_sessions.json", ok=False, **fields)

    def _tokens_reloaded(self, **fields: Any) -> None:
        level = "info" if fields.get("ok") else "error"
        self.log.emit(level, "config_reload", file="tokens.json", **fields)


# ---------------------------------------------------------------------------
# Responses
# ---------------------------------------------------------------------------


class AsciiJSONResponse(JSONResponse):
    """JSON with every non-ASCII character escaped: event data may hold a lone
    surrogate (valid JSON, e.g. from ``--data '{"k": "\\ud800"}'``), which has no
    UTF-8 encoding, so Starlette's UTF-8 rendering would fail after the commit."""

    def render(self, content: Any) -> bytes:
        return json.dumps(content, separators=(",", ":")).encode("ascii")


def envelope_ok(data: Any, status: int = 200) -> JSONResponse:
    return AsciiJSONResponse({"ok": True, "data": data}, status_code=status)


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
    return AsciiJSONResponse(
        {"ok": False, "error": error}, status_code=error_status(exc), headers=headers
    )


def internal_error() -> JSONResponse:
    return AsciiJSONResponse(
        {"ok": False, "error": {"code": "INTERNAL_ERROR", "message": "internal server error"}},
        status_code=500,
    )


class HeadersMiddleware:
    """The ``Lattice-*`` headers on every response; ``no-store`` on ``/v1``; outside
    ``/v1``, SPEC §10's ``nosniff`` and CSP (and ``no-store`` on hosted dashboard
    API responses); request logs. The one header policy, outermost."""

    def __init__(self, app: ASGIApp, state: ServerState) -> None:
        self.app = app
        self.state = state

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        started = time.monotonic()
        scope[RAW_SEND] = send  # an aborted stream aborts its connection through it
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
        extra.extend(page_headers(path, self.state.web.csp))

        async def send_with_headers(message: Message) -> None:
            if message["type"] == "http.response.start":
                status_holder["status"] = message["status"]
                headers = [
                    (k, v) for k, v in message.get("headers", []) if k.lower() != b"cache-control"
                ]
                clear = scope["state"].get(CLEAR_COOKIE)
                if clear:  # a dead session cookie is cleared on any answer (SPEC §10)
                    headers.append((b"set-cookie", clear.encode("latin-1")))
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
                    replayed=fields.get("replayed"),
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
        except Exception as exc:  # noqa: BLE001 - a bug must answer 500, never drop the loop
            original = exc.original if isinstance(exc, WorkerCrash) else exc
            log_fields["error_code"] = "INTERNAL_ERROR"
            state.log.error(
                "op_crashed" if log_fields.get("op") else "request_crashed",
                **exception_fields(original),
                **{k: log_fields.get(k) for k in ("project", "op", "op_id", "token_id")},
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
        if len(received) + len(chunk) > limit:
            raise too_large  # checked before copying: nothing is buffered past the limit
        received.extend(chunk)
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
        return _parse_envelope(request, state, token, op_name, raw)
    except RecursionError:
        raise OpError("VALIDATION_ERROR", "request body is nested too deeply.") from None


def _parse_envelope(
    request: Request, state: ServerState, token: TokenRecord, op_name: str, raw: bytes
) -> tuple[WriteRequest, dict]:
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
    minted = op_id is None
    if minted:
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

    origin = body.get("origin")
    origin = {} if origin is None else origin
    if not isinstance(origin, dict):
        raise OpError("VALIDATION_ERROR", "origin must be an object.")
    reported = check_reported(origin.get("reported"))  # any client "authenticated" is dropped

    attestations = body.get("attestations")
    attestations = {} if attestations is None else attestations
    if not isinstance(attestations, dict):
        raise OpError("VALIDATION_ERROR", "attestations must be an object.")
    expect = body.get("expect")
    expect = {} if expect is None else expect
    if not isinstance(expect, dict) or set(expect) - {"last_event_id"}:
        raise OpError("VALIDATION_ERROR", "expect may hold only last_event_id.")
    expect_last = expect.get("last_event_id")
    if expect_last is not None and not isinstance(expect_last, str):
        raise OpError("VALIDATION_ERROR", "expect.last_event_id must be a string.")

    authenticated = token.authenticated_origin()
    if getattr(op_cls, "no_actor", False):
        # SPEC §3.7: these take no actor. The envelope's actor and actor_name are
        # ignored (not authorized, not fingerprinted, not logged); the token
        # authorizes the call as its own default actor, recorded in
        # origin.authenticated.
        if token.default_actor is None:
            raise OpError(
                "MISSING_ACTOR",
                f"operation {op_name} runs as the token's default actor, and token "
                f"{token.id} has none (it needs exactly one literal actor pattern).",
            )
        actor, actor_name = None, None
        authenticated["actor"] = token.default_actor
        log_fields["actor"] = token.default_actor
    else:
        actor, actor_name = _resolve_request_actor(body, token)
        log_fields["actor"] = actor if actor_name is None else f"name:{actor_name}"
        if actor_name is None:
            token.authorize_actor(actor)

    caller = Caller(
        actor=actor,
        actor_name=actor_name,
        origin={
            "op_id": op_id,
            "reported": reported,
            "authenticated": authenticated,
        },
        attestations=attestations,
        expect_last_event_id=expect_last,
    )
    fp = fingerprint(op_name, params_json, actor, actor_name, attestations, expect_last)
    write = WriteRequest(
        op=op_name,
        params=params,
        caller=caller,
        token_id=token.id,
        fp=fp,
        authorize=lambda identity, _caller: token.authorize_actor(identity),
        minted=minted,
    )
    return write, body


def _resolve_request_actor(body: dict, token: TokenRecord) -> tuple[str | None, str | None]:
    """The envelope's ``(actor, actor_name)``: a session name checked as one safe path
    component (it is authorized later, by ``execute``, once resolved), else a string
    actor defaulted from the token and validated."""
    actor = body.get("actor")
    actor_name = body.get("actor_name")
    if actor is not None and not isinstance(actor, str):
        raise OpError("VALIDATION_ERROR", "actor must be a string.")
    if actor_name is not None:
        if not isinstance(actor_name, str):
            raise OpError("VALIDATION_ERROR", "actor_name must be a string.")
        check_path_component(actor_name, "session name")
        return None, actor_name  # a session wins over --actor, as locally
    if actor is None:
        actor = token.default_actor
        if actor is None:
            raise OpError("MISSING_ACTOR", MISSING_ACTOR_MESSAGE)
    if not validate_actor(actor):
        raise OpError(
            "INVALID_ACTOR",
            f"Invalid actor format: '{actor}'. "
            "Expected prefix:identifier (e.g., human:atin, agent:claude).",
        )
    return actor, None


# ---------------------------------------------------------------------------
# Handlers
# ---------------------------------------------------------------------------


async def healthz(request: Request, state: ServerState) -> Response:
    free, counts = await in_worker(lambda: (state.disk.free_bytes(), state.registry.counts()))
    ok = free >= state.config.limits.min_free_disk_bytes
    body = {
        "ok": ok,
        "version": state.version,
        "protocol": PROTOCOL,
        "disk_free_bytes": free,
        "projects": counts,
    }
    return AsciiJSONResponse(body, status_code=200 if ok else 503)


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
        registry = state.registry
        return envelope_ok(
            {
                "version": state.version,
                "protocol": PROTOCOL,
                "min_client_version": MIN_CLIENT_VERSION,
                "stream_heartbeat_seconds": state.heartbeat_seconds,
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
                    "configured": state.config.audit.enabled,
                    "active": registry.audit_active,
                    "reason": registry.audit_reason,
                },
            }
        )

    return await _with_token(request, state, run)


async def projects(request: Request, state: ServerState) -> Response:
    """Visible projects: slug, project code, head seq, state. Each row is read under
    that project's admission and work lock, loading it first if it never loaded, as
    any request does (SPEC §8.5)."""

    def row_for(project: Project) -> dict:
        if project.state == LOADED:
            # The admission checks every request runs: hand edits are journaled
            # and queued control requests applied before the row is captured.
            # A check that quarantines the project leaves it listed as unavailable.
            try:
                project.admit()
            except OpError:
                pass
        code = None
        try:
            config = json.loads((project.board / "config.json").read_text(encoding="utf-8"))
            code = config.get("project_code")
        except (OSError, ValueError):
            pass
        head = project.journal.head_seq if project.journal is not None else None
        return {
            "slug": project.slug,
            "project_code": code,
            "head_seq": head,
            "state": project.state,
        }

    async def run(token: TokenRecord) -> Response:
        slugs = await in_worker(lambda: _visible_slugs(state, token))
        rows = []
        for slug in slugs:
            project = state.registry.get(slug)
            if project is None:
                continue
            rows.append(
                await state.registry.run_locked(project, lambda p=project: row_for(p), admit=False)
            )
        return envelope_ok({"projects": rows})

    return await _with_token(request, state, run)


@contextlib.contextmanager
def pending_op(state: ServerState, key: tuple[str, str, str] | None) -> Iterator[None]:
    """Count *key* as pending for the block (SPEC §8.6, op status).

    Entered before admission; left however the block ends: the finish step has
    recorded the commit, or the request was rolled back, rejected, timed out on
    the lock, or cancelled (a ``CancelledError`` at an ``await`` inside the
    block unwinds through here too). ``None`` (no client ``op_id``) never joins.
    """
    if key is None:
        yield
        return
    state.pending_ops[key] = state.pending_ops.get(key, 0) + 1
    try:
        yield
    finally:
        left = state.pending_ops[key] - 1
        if left:
            state.pending_ops[key] = left
        else:
            del state.pending_ops[key]


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
        write, body = parse_envelope(request, state, token, op_name, raw)
        state.disk.check()

        def work() -> Any:
            with project.locked():
                project.admit()
                return project.run_write(write)

        # A request without a client op_id has nothing to look up (SPEC §8.4).
        key = (slug, token.id, write.caller.origin["op_id"]) if body.get("op_id") else None
        with pending_op(state, key):
            async with state.registry.admitted(project):
                outcome = await in_worker(work)
        log_fields["seq"] = outcome.seq
        if outcome.replayed:
            log_fields["replayed"] = True
        return envelope_ok(
            {
                "result": outcome.result_data,
                "seq": outcome.seq,
                "op_id": write.caller.origin["op_id"],
            }
        )
    finally:
        state.limits.leave(token.id)


async def op_status(request: Request, state: ServerState) -> Response:
    """``GET /v1/projects/{slug}/ops/{op_id}``: the outcome of one of the caller's
    own operations (SPEC §8.6): ``committed``, ``in_flight`` (still pending,
    possibly queued for the locks), or ``not_found``. A loaded project answers
    from memory without admission; one that has never loaded is admitted (and
    loads) first."""
    slug = request.path_params["slug"]
    op_id = request.path_params["op"]
    log_fields = request.scope["state"]["log"]
    log_fields.update(project=slug, op_id=op_id)

    async def run(token: TokenRecord) -> Response:
        project = resolve_project(state, token, slug)
        check_op_id(op_id)
        if project.state in (UNLOADED, LOADING):
            async with state.registry.admitted(project):
                pass
        project.require_loaded()
        data = await in_worker(lambda: project.op_status(token.id, op_id))
        if data["state"] == "not_found":
            if (slug, token.id, op_id) in state.pending_ops:
                data = {"state": "in_flight"}
            else:
                # It may have committed and left the set between the two checks
                # (it leaves only after its commit is recorded): look again.
                data = await in_worker(lambda: project.op_status(token.id, op_id))
        return envelope_ok(data)

    return await _with_token(request, state, run)


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
    from lattice.core.visibility import visible

    active, archived_rows = load_all_snapshots(board)
    # Erased (tombstoned) tasks are left out of lists by default (SPEC §7).
    rows = visible(active + (archived_rows if archived else []))
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


# ---------------------------------------------------------------------------
# Sync and files (SPEC §8.8)
# ---------------------------------------------------------------------------


def _query_int(request: Request, name: str) -> int:
    raw = request.query_params.get(name)
    if raw is None or raw == "":
        return 0
    if not raw.isdigit():
        raise OpError("VALIDATION_ERROR", f"{name} must be a non-negative integer.")
    return int(raw)


def _json_bytes(data: Any) -> Response:
    """An ``ok`` envelope serialized off the event loop (a reset can be large)."""
    body = json.dumps({"ok": True, "data": data}, separators=(",", ":")).encode("utf-8")
    return Response(body, media_type="application/json")


async def sync(request: Request, state: ServerState) -> Response:
    """``GET /v1/projects/{slug}/sync?since=N&epoch=E&hash=H[&manifest=1]``."""
    slug = request.path_params["slug"]
    log_fields = request.scope["state"]["log"]
    log_fields["project"] = slug

    async def run(token: TokenRecord) -> Response:
        project = resolve_project(state, token, slug)
        since = _query_int(request, "since")
        epoch = request.query_params.get("epoch") or None
        client_hash = request.query_params.get("hash") or None
        manifest = request.query_params.get("manifest") == "1"
        journal = project.journal
        if not manifest and project.state == LOADED and journal is not None:
            # SPEC §8.5: a sync at the head reads only memory, so it skips admission.
            body = fast_path_body(journal.head, since, epoch, client_hash)
            if body is not None:
                log_fields["_level"] = "debug"
                return envelope_ok(body)
        limits = state.config.limits

        def assemble(may_reset: bool) -> Response | None:
            current = project.journal
            if current is None or project.manifest is None:
                project.require_loaded()
                raise OpError("BOARD_UNAVAILABLE", f"project {slug} is not loaded")
            if manifest:
                return _json_bytes(manifest_body(current, project.manifest))
            if needs_reset(current, since, epoch, client_hash):
                if not may_reset:
                    return None  # take the reset gate first, then come back
                data = reset_body(
                    project.board, current, project.manifest, slug, limits.inline_file_bytes
                )
                return _json_bytes(data)
            data = delta_body(
                project.board, current, project.manifest, slug, since, limits.inline_file_bytes
            )
            if not data["files"] and not data["removed"]:
                log_fields["_level"] = "debug"
            return _json_bytes(data)

        predicted_reset = not manifest and (
            journal is None or needs_reset(journal, since, epoch, client_hash)
        )
        if not predicted_reset:
            response = await state.registry.run_locked(project, lambda: assemble(False))
            if response is not None:
                return response
        # SPEC §8.8: a project assembles one reset at a time; a second waits here,
        # before it seeks admission.
        async with project.reset_gate:
            response = await state.registry.run_locked(project, lambda: assemble(True))
        assert response is not None
        return response

    return await _with_token(request, state, run)


async def board_file(request: Request, state: ServerState) -> Response:
    """``GET /v1/projects/{slug}/files/{path}?sha256=H``: one board file's raw bytes."""
    slug = request.path_params["slug"]
    request.scope["state"]["log"]["project"] = slug

    async def run(token: TokenRecord) -> Response:
        project = resolve_project(state, token, slug)
        rel = check_file_path(request.path_params["path"])
        pinned = request.query_params.get("sha256") or None
        data = await state.registry.run_locked(
            project, lambda: read_board_file(project.board, rel, pinned)
        )
        return Response(data, media_type="application/octet-stream")

    return await _with_token(request, state, run)


# ---------------------------------------------------------------------------
# Stream (SPEC §8.9)
# ---------------------------------------------------------------------------


def _resume_point(request: Request) -> tuple[bool, str | None, int, str | None]:
    """``(given, epoch, seq, hash)`` from ``Last-Event-ID`` (which wins) or the query.
    A malformed ``Last-Event-ID`` is a resume point that matches nothing (a reset)."""
    header = request.headers.get("last-event-id")
    if header is not None and header.strip():
        parsed = parse_entry_id(header)
        if parsed is None:
            return True, None, -1, None
        return True, parsed[0], parsed[1], parsed[2]
    if "since" not in request.query_params:
        return False, None, 0, None
    since = _query_int(request, "since")
    return (
        True,
        request.query_params.get("epoch") or None,
        since,
        (request.query_params.get("hash") or None),
    )


def _stream_start(
    project: Project, limits: Any, subscriber: Subscriber, resume: tuple
) -> tuple[list[bytes], int]:
    """Under the work lock: subscribe, then build the replay (or one ``reset``).

    Publication happens only under this lock, so the subscriber's queue starts
    exactly after the head the replay reads up to: no gap, no duplicate.
    """
    journal = project.journal
    if journal is None:
        project.require_loaded()
        raise OpError("BOARD_UNAVAILABLE", f"project {project.slug} is not loaded")
    if not project.broadcaster.subscribe(subscriber, limits.max_stream_subscribers_per_project):
        raise _too_many_streams(project)
    try:
        given, epoch, since, client_hash = resume
        if not given:
            return [], journal.head_seq  # live only, from the current head
        if (
            epoch != journal.epoch
            or since < 0
            or since > journal.head_seq
            or (since > 0 and client_hash != journal.hash_at(since))
            or journal.head_seq - since > limits.replay_reset_entries
        ):
            return [reset_frame(journal.epoch)], journal.head_seq
        frames = []
        for seq, raw in journal.read_lines(since):
            line = json.loads(raw)
            digest = journal.hash_at(seq)
            assert digest is not None
            events = entry_events(project.board, journal, line)
            frames.append(journal_frame(journal.epoch, line, digest, events))
        return frames, journal.head_seq
    except BaseException:
        project.broadcaster.unsubscribe(subscriber)
        raise


def _too_many_streams(project: Project) -> OpError:
    return OpError(
        "RATE_LIMITED",
        f"project {project.slug} already has the most open streams it allows; retry later",
        {"retry_after": 1},
    )


async def stream(request: Request, state: ServerState) -> Response:
    """``GET /v1/projects/{slug}/stream``: SSE. Subscribe, replay after the resume
    point (or ``reset``), then live entries; a heartbeat at once and every
    ``heartbeat_seconds``, each after rechecking the credential."""
    slug = request.path_params["slug"]
    request.scope["state"]["log"]["project"] = slug
    check_protocol(request)
    authorization = request.headers.get("authorization")
    cookie = session_cookie(request) if authorization is None else None
    if cookie is not None:
        # A browser's EventSource (SPEC §10): the session authenticates the
        # stream only when no Authorization header is sent; an Origin, when a
        # browser sends one, must be this server's.
        if request.headers.get("origin") is not None:
            require_origin(request, state)
        _session, token = session_auth(request, state)
    else:
        token = authenticate(request, state)  # open streams are not in the in-flight limit
    project = resolve_project(state, token, slug)
    resume = _resume_point(request)
    limits = state.config.limits
    if project.broadcaster.count() >= limits.max_stream_subscribers_per_project:
        raise _too_many_streams(project)
    subscriber = Subscriber(asyncio.get_running_loop(), limits.stream_queue_entries)
    initial, sent_seq = await state.registry.run_locked(
        project, lambda: _stream_start(project, limits, subscriber, resume)
    )

    def alive() -> bool:
        try:
            if cookie is not None:
                _session, current = state.sessions.authenticate(cookie)
            else:
                current = state.tokens.authenticate(authorization)
        except OpError:
            return False
        return current.permits_project(slug) and project.state == LOADED

    def closed(sub: Subscriber) -> None:
        project.broadcaster.unsubscribe(sub)
        if sub.overflowed:
            state.log.warning("stream_overflow", project=slug, token_id=token.id)

    return EventStream(
        subscriber,
        initial=initial,
        sent_seq=sent_seq,
        alive=alive,
        announced=lambda: project.broadcaster.announced,
        heartbeat_seconds=state.heartbeat_seconds,
        on_close=closed,
    )


async def not_found(request: Request, state: ServerState) -> Response:
    """No such route. Under ``/v1`` the caller is authenticated first (protocol,
    credential, in-flight limit), so an unauthenticated request learns nothing
    about which paths exist (AC-11)."""

    async def missing(_token: TokenRecord | None = None) -> Response:
        raise OpError("NOT_FOUND", f"no route {request.method} {request.url.path}")

    if request.url.path == "/v1" or request.url.path.startswith("/v1/"):
        return await _with_token(request, state, missing)
    return await missing()


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
        state.loop = asyncio.get_running_loop()
        state.log.info(
            "startup",
            version=state.version,
            protocol=PROTOCOL,
            server_id=state.server_id,
            projects=len(state.registry.slugs()),
            **({"fd_limit": state.fd_limit} if state.fd_limit is not None else {}),
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
        Route("/v1/projects/{slug}/ops/{op}", endpoint(op_status), methods=["GET"]),
        Route("/v1/projects/{slug}/sync", endpoint(sync), methods=["GET"]),
        Route("/v1/projects/{slug}/stream", endpoint(stream), methods=["GET"]),
        Route("/v1/projects/{slug}/files/{path:path}", endpoint(board_file), methods=["GET"]),
        Route("/v1/projects/{slug}/tasks", endpoint(task_list), methods=["GET"]),
        Route("/v1/projects/{slug}/tasks/{task_id}", endpoint(task_read), methods=["GET"]),
        Route("/", endpoint(web.index), methods=["GET"]),
        Route("/login", endpoint(web.login_page), methods=["GET"]),
        Route("/login", endpoint(web.login), methods=["POST"]),
        Route("/logout", endpoint(web.logout), methods=["POST"]),
        Route("/web/{name}", endpoint(web.web_asset), methods=["GET"]),
        Route("/p/{slug}", endpoint(dashboard.bare_slug), methods=["GET"]),
        Route("/p/{slug}/", endpoint(dashboard.page), methods=["GET"]),
        Route("/p/{slug}/favicon.ico", endpoint(dashboard.static), methods=["GET"]),
        Route(
            "/p/{slug}/static/{path:path}",
            endpoint(dashboard.dashboard_endpoint(dashboard.static)),
            methods=["GET"],
        ),
        Route(
            "/p/{slug}/api/{path:path}",
            endpoint(dashboard.dashboard_endpoint(dashboard.api_get)),
            methods=["GET"],
        ),
        Route(
            "/p/{slug}/api/{path:path}",
            endpoint(dashboard.dashboard_endpoint(dashboard.api_post)),
            methods=["POST"],
        ),
        Route(
            "/{path:path}", endpoint(not_found), methods=["GET", "POST", "PUT", "DELETE", "PATCH"]
        ),
    ]
    app = Starlette(routes=routes, lifespan=lifespan)
    app.state.lattice = state
    return HeadersMiddleware(app, state)
