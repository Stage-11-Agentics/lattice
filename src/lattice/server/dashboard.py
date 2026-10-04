"""The hosted dashboard: ``/p/<slug>/`` over the authoritative board (SPEC §10).

- ``/p/<slug>/`` serves the dashboard page, and ``/p/<slug>/static/*`` its
  assets, exactly as the local dashboard does; the page derives its base path
  from its own URL.
- ``GET /p/<slug>/api/*`` answers with ``dashboard/api.route_get`` on the
  project's board. Like every board read it passes admission and runs under
  the work lock (SPEC §8.5), so external changes, unloads, and quarantine are
  seen first. Inside the lock a per-project memo keyed by the
  load, ``(epoch, head_seq)``, and ``(path, query)`` saves the computation and serialization,
  so any number of viewers at one head cost one computation per endpoint.
  ``If-None-Match`` is applied to the memoized response.
- ``POST /p/<slug>/api/*`` translates through ``dashboard/api.translate_post``
  and runs the operation as a server transaction with the token's browser
  actor (SPEC §8.3); any actor in the body is ignored. A session POST needs a
  same-origin ``Origin`` (checked first) and JSON. The page names each
  logical write with a ``Lattice-Op-Id`` header, reused on retry, so a retry
  after a lost response applies once (SPEC §8.6); without one the server
  mints an ID and does not deduplicate.
- Credentials: ``Authorization: Bearer`` when the header is present (never
  falling back to a cookie), else the session cookie.
"""

from __future__ import annotations

import dataclasses
import hashlib
import json
import threading
from collections import OrderedDict
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from starlette.requests import Request
from starlette.responses import RedirectResponse, Response

from lattice.core.errors import OpError
from lattice.core.ids import generate_op_id
from lattice.dashboard import api
from lattice.ops.base import Caller, OpResult, check_op_id, get_operation, parse_params
from lattice.server import web
from lattice.server.journal import fingerprint
from lattice.server.project import Project, WriteRequest
from lattice.server.registry import in_worker
from lattice.server.tokens import TokenRecord
from lattice.storage.operations import AuthorityCache, authority_cache

if TYPE_CHECKING:
    from lattice.server.app import ServerState

MEMO_ENTRIES_PER_PROJECT = 256
OP_ID_HEADER = "lattice-op-id"
HOSTED_GIT = {"available": False, "reason": "hosted"}

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
# The read memo
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class CachedRead:
    status: int
    body: bytes
    headers: dict[str, str]


class ReadMemo:
    """One project's memoized dashboard reads at one head. Used under the
    project's work lock only; the lock here guards the memo map itself."""

    def __init__(self, limit: int = MEMO_ENTRIES_PER_PROJECT) -> None:
        self.limit = limit
        #: The journal object of the load the entries were computed under: a
        #: reload can adopt files changed while unloaded at the same seq.
        self.journal: object | None = None
        self.head: tuple[str | None, int] | None = None
        self.entries: OrderedDict[tuple[str, str], CachedRead] = OrderedDict()
        #: Replays reused within one head, and across heads only while their
        #: event logs are byte-for-byte unchanged, so a write costs the next
        #: read one replay, not one per task (AC-42). Scoped to one load and
        #: epoch; every read begins it at the current ``(epoch, head_seq)``.
        self.authorities = AuthorityCache()

    def get(
        self,
        journal: object,
        head: tuple[str | None, int],
        key: tuple[str, str],
        compute: Callable[[], CachedRead],
    ) -> CachedRead:
        if journal is not self.journal or (self.head and head[0] != self.head[0]):
            self.authorities = AuthorityCache()  # a new load or epoch: nothing carries over
        self.authorities.begin(head)
        if journal is not self.journal or head != self.head:
            self.entries.clear()
            self.journal, self.head = journal, head
        hit = self.entries.get(key)
        if hit is not None:
            self.entries.move_to_end(key)
            return hit
        with authority_cache(self.authorities):
            value = compute()
        self.entries[key] = value
        while len(self.entries) > self.limit:
            self.entries.popitem(last=False)
        return value


class ReadMemos:
    def __init__(self) -> None:
        self._by_slug: dict[str, ReadMemo] = {}
        self._lock = threading.Lock()

    def for_project(self, slug: str) -> ReadMemo:
        with self._lock:
            return self._by_slug.setdefault(slug, ReadMemo())


# ---------------------------------------------------------------------------
# Responses and credentials
# ---------------------------------------------------------------------------


def _json_response(response: api.ApiResponse) -> Response:
    if response.envelope is None:
        return Response(status_code=response.status, headers=response.headers)
    return Response(
        response.body(),
        status_code=response.status,
        headers=response.headers,
        media_type="application/json; charset=utf-8",
    )


def _api_error(exc: api.ApiError) -> Response:
    return _json_response(api.ApiResponse(exc.status, exc.envelope()))


def dashboard_endpoint(
    handler: Callable[[Request, ServerState], Awaitable[Response]],
) -> Callable[[Request, ServerState], Awaitable[Response]]:
    """``ApiError`` → the dashboard's own envelope; ``OpError`` and crashes are
    left to the app's ``endpoint`` wrapper."""

    async def wrapped(request: Request, state: ServerState) -> Response:
        try:
            return await handler(request, state)
        except api.ApiError as exc:
            request.scope["state"]["log"]["error_code"] = exc.code
            return _api_error(exc)

    return wrapped


def credential(request: Request, state: ServerState) -> tuple[TokenRecord, bool]:
    """``(token, via_session)``. A present ``Authorization`` header decides alone."""
    from lattice.server.app import authenticate

    if request.headers.get("authorization") is not None:
        return authenticate(request, state), False
    _session, token = web.session_auth(request, state)
    return token, True


def _project(request: Request, state: ServerState, token: TokenRecord) -> Project:
    from lattice.server.app import resolve_project

    slug = request.path_params["slug"]
    request.scope["state"]["log"]["project"] = slug
    return resolve_project(state, token, slug)


async def _limited(state: ServerState, token: TokenRecord, fn: Callable[[], Awaitable[Response]]):
    state.limits.enter(token.id)
    try:
        return await fn()
    finally:
        state.limits.leave(token.id)


# ---------------------------------------------------------------------------
# The page and its assets
# ---------------------------------------------------------------------------


async def bare_slug(request: Request, state: ServerState) -> Response:
    return RedirectResponse(f"/p/{request.path_params['slug']}/", status_code=308)


async def page(request: Request, state: ServerState) -> Response:
    slug = request.path_params["slug"]
    try:
        token, _ = credential(request, state)
    except OpError:
        return RedirectResponse(f"/login?next=/p/{slug}/", status_code=303)
    _project(request, state, token)
    return Response(state.web.page, media_type="text/html; charset=utf-8")


def _static_bytes(state: ServerState, rel: str) -> tuple[bytes, str]:
    base = state.web.static_dir.resolve()
    target = (base / rel).resolve()
    if ".." in rel.split("/") or not target.is_relative_to(base) or not target.is_file():
        raise api.ApiError(404, "NOT_FOUND", f"Static file not found: {rel}")
    ext = "." + rel.rsplit(".", 1)[-1] if "." in rel else ""
    return target.read_bytes(), _STATIC_TYPES.get(ext, "application/octet-stream")


async def static(request: Request, state: ServerState) -> Response:
    token, _ = credential(request, state)
    _project(request, state, token)
    rel = request.path_params.get("path", "favicon.ico")
    data, media = await in_worker(lambda: _static_bytes(state, rel))
    return Response(data, media_type=f"{media}; charset=utf-8")


# ---------------------------------------------------------------------------
# Reads
# ---------------------------------------------------------------------------


def _api_path(request: Request) -> str:
    return ("/api/" + request.path_params["path"]).rstrip("/")


def _compute(project: Project, path: str, query: str) -> CachedRead:
    response = api.route_get(
        project.board,
        path,
        query,
        None,
        issue_media_route="/issues/media/{issue_id}",
    )
    # The local dashboard's JSON, compact: the same document, serialized by the C
    # encoder (an indented dump runs the pure-Python one, the largest cost per head).
    body = (
        b""
        if response.envelope is None
        else json.dumps(response.envelope, sort_keys=True, separators=(",", ":")).encode("utf-8")
    )
    return CachedRead(response.status, body, dict(response.headers))


async def api_get(request: Request, state: ServerState) -> Response:
    token, _ = credential(request, state)

    async def run() -> Response:
        project = _project(request, state, token)
        path = _api_path(request)
        if path == "/api/git" or path.startswith("/api/git/"):
            return _json_response(api.ok(HOSTED_GIT))  # never inspects the server's repo
        query = request.url.query
        memo = state.dashboard_memos.for_project(project.slug)

        def read() -> CachedRead:
            journal = project.journal
            if journal is None:
                project.require_loaded()
                raise OpError("BOARD_UNAVAILABLE", f"project {project.slug} is not loaded")
            head = (journal.epoch, journal.head_seq)
            return memo.get(journal, head, (path, query), lambda: _compute(project, path, query))

        cached = await state.registry.run_locked(project, read)
        etag = cached.headers.get("ETag")
        if etag and request.headers.get("if-none-match") == etag:
            return Response(status_code=304, headers={"ETag": etag})
        return Response(
            cached.body,
            status_code=cached.status,
            headers=cached.headers,
            media_type="application/json; charset=utf-8",
        )

    return await _limited(state, token, run)


async def issue_media(request: Request, state: ServerState) -> Response:
    """Same-origin, session-cookie path for dashboard media elements."""
    if request.headers.get("authorization") is not None:
        raise OpError("FORBIDDEN", "dashboard issue media uses the browser session cookie.")
    if request.headers.get("origin") is not None:
        web.require_origin(request, state)
    _session, token = web.session_auth(request, state)

    async def run() -> Response:
        project = _project(request, state, token)
        issue_id = request.path_params["issue_id"]
        media_id = request.path_params["media_id"]
        frame = request.path_params.get("frame")

        def plan():
            from lattice.server.app import check_issue_data_version
            from lattice.server.issue_media import plan_media_read

            check_issue_data_version(request, project)
            return plan_media_read(project.board, issue_id, media_id, frame_name_value=frame)

        try:
            from lattice.server.issue_media import open_media

            planned = await state.registry.run_locked(project, plan)
            value = await in_worker(lambda: open_media(planned, request.headers.get("range")))
        except OpError as exc:
            if exc.code != "RANGE_NOT_SATISFIABLE":
                raise
            from lattice.server.app import envelope_error

            response = envelope_error(exc)
            response.headers["Accept-Ranges"] = "bytes"
            response.headers["Content-Range"] = f"bytes */{exc.details.get('size_bytes', 0)}"
            return response
        from lattice.server.app import media_response

        return media_response(value)

    return await _limited(state, token, run)


def _stage_prepared_media(project: Project, filename: str, content: bytes) -> dict:
    """Prepare one browser file and stage its stored original and video frames."""
    from lattice.core.issue_media import clean_original_name
    from lattice.dashboard.media_prep import prepare_issue_media
    from lattice.ops.issue_common import check_media_items
    from lattice.ops.task_attach import decode_payload, encode_payload

    filename = clean_original_name(filename) or "attachment"
    prepared_items = prepare_issue_media(
        [{"payload": encode_payload(filename, content)}], refuse_video_without_ffmpeg=True
    )
    if len(prepared_items) != 1:
        raise OpError("WRITE_ERROR", "media preparation returned an invalid item count.")
    prepared = prepared_items[0]
    check_media_items((prepared,))

    def stage(payload: dict) -> dict:
        item_name, data = decode_payload(payload)
        digest = hashlib.sha256(data).hexdigest()
        upload = project.issue_media.begin_upload(digest, len(data), dashboard=True)
        try:
            upload.write(data)
            result = upload.finish()
        except BaseException:
            upload.abort()
            raise
        return {
            "filename": item_name,
            "sha256": result["sha256"],
            "size": result["size_bytes"],
            "staged": True,
        }

    staged = {key: value for key, value in prepared.items() if key not in ("payload", "frames")}
    staged["payload"] = stage(prepared["payload"])
    staged["frames"] = [
        {"t_ms": frame["t_ms"], "payload": stage(frame["payload"])}
        for frame in prepared.get("frames", [])
    ]
    if not staged["frames"]:
        staged.pop("frames")
    return staged


async def issue_media_stage(request: Request, state: ServerState) -> Response:
    """Same-origin session upload; preparation and raw staging are project scoped."""
    from lattice.server.app import _UploadBody, _close_after_answer, _issue_log_state
    from lattice.server.issue_media import validate_sha256

    if request.headers.get("authorization") is not None:
        raise OpError(
            "FORBIDDEN", "dashboard issue media staging uses the browser session cookie."
        )
    web.require_origin(request, state)  # before looking up the session
    _session, token = web.session_auth(request, state)
    project = _project(request, state, token)
    request.scope["state"]["log"].update(op="issue.media_stage")
    body = _UploadBody(request, state.log, project.slug, token.id)
    upload_size: int | None = None
    try:
        if (
            request.headers.get("content-type", "").split(";", 1)[0].strip().lower()
            != "application/octet-stream"
        ):
            raise OpError(
                "VALIDATION_ERROR",
                "issue media upload needs Content-Type: application/octet-stream.",
            )
        declared = request.headers.get("content-length")
        if (
            declared is None
            or len(declared) > 20
            or not declared.isascii()
            or not declared.isdigit()
        ):
            raise OpError(
                "VALIDATION_ERROR", "issue media upload needs a non-negative Content-Length."
            )
        upload_size = int(declared)
        if upload_size > state.config.limits.max_issue_media_file_bytes:
            raise OpError(
                "PAYLOAD_TOO_LARGE",
                f"media object is over the {state.config.limits.max_issue_media_file_bytes} byte per-file limit.",
                {"limit_bytes": state.config.limits.max_issue_media_file_bytes},
            )
        digest = validate_sha256(request.path_params["sha256"])
        state.limits.enter(token.id)
        try:
            state.limits.take_op(token.id)
            state.limits.take_bytes(token.id, upload_size)
            state.disk.check()
            async with state.registry.admitted(project):
                project.require_loaded()
            enabled, existing = await state.registry.run_locked(
                project, lambda: _issue_log_state(project), admit=False
            )
            if not enabled:
                from lattice.core.issues import hosted_issues_disabled_message

                raise OpError(
                    "ISSUES_DISABLED", hosted_issues_disabled_message(existing, project.slug)
                )

            received = bytearray()
            while (chunk := await body.next()) is not None:
                if body.received > upload_size:
                    raise OpError("VALIDATION_ERROR", "media upload exceeded Content-Length.")
                received.extend(chunk)
            if len(received) != upload_size:
                raise OpError("VALIDATION_ERROR", "media upload did not match Content-Length.")
            if hashlib.sha256(received).hexdigest() != digest:
                raise OpError("VALIDATION_ERROR", "media bytes do not match the supplied sha256.")
            filename = request.query_params.get("filename", "attachment") or "attachment"
            staged = await in_worker(
                lambda: _stage_prepared_media(project, filename, bytes(received))
            )
            return _json_response(api.ok(staged, 201))
        finally:
            state.limits.leave(token.id)
    except OpError as refusal:
        drain_cap = state.config.limits.max_issue_media_file_bytes * 2
        if upload_size is not None and upload_size <= drain_cap:
            try:
                state.limits.enter(token.id)
            except OpError:
                _close_after_answer(request, body)
                raise refusal from None
            try:
                await body.drain()
            finally:
                state.limits.leave(token.id)
        _close_after_answer(request, body)
        raise
    except Exception as exc:  # noqa: BLE001 - answer an envelope, not a reset
        raise api.ApiError(500, "WRITE_ERROR", f"Could not prepare the media: {exc}") from exc


# ---------------------------------------------------------------------------
# Writes
# ---------------------------------------------------------------------------


def _result_from(data: dict) -> OpResult:
    names = {f.name for f in dataclasses.fields(OpResult)}
    return OpResult(**{k: v for k, v in data.items() if k in names and k != "paths"})


def _browser_actor(token: TokenRecord) -> str:
    actor = token.browser_actor
    if actor is None:
        raise OpError(
            "MISSING_ACTOR",
            f"token {token.id} names no actor a browser can write as: its user "
            f"{token.user} is not among its permitted actors, and it has no single "
            "default actor.",
        )
    token.authorize_actor(actor)
    return actor


async def api_post(request: Request, state: ServerState) -> Response:
    from lattice.server.app import read_body

    path = _api_path(request)
    if request.headers.get("authorization") is None:
        web.require_origin(request, state)  # before the session is looked up
    token, _via_session = credential(request, state)
    project = _project(request, state, token)
    if web.content_type(request) != "application/json":
        raise api.ApiError(415, "VALIDATION_ERROR", "POST requires Content-Type: application/json")
    sent_op_id = request.headers.get(OP_ID_HEADER)
    if sent_op_id is not None:
        check_op_id(sent_op_id)
    op_id = sent_op_id or generate_op_id()
    log_fields = request.scope["state"]["log"]
    log_fields["op_id"] = op_id

    async def run() -> Response:
        state.limits.take_op(token.id)
        raw = await read_body(request, state, token)
        try:
            body = json.loads(raw.decode("utf-8")) if raw else None
        except (UnicodeDecodeError, ValueError):
            raise api.ApiError(400, "BAD_REQUEST", "Invalid JSON in request body") from None
        if body is None:
            raise api.ApiError(400, "BAD_REQUEST", "Empty request body")
        if path.startswith("/api/tasks/") and path.rsplit("/", 1)[-1] in (
            "open-notes",
            "open-plans",
        ):
            kind = "notes" if path.endswith("open-notes") else "plan"
            raise api.ApiError(
                400,
                "LOCAL_ONLY",
                f"This board lives on a server; write the {kind} with "
                f"'lattice {kind} write <task> --file <path>'.",
            )
        translated = api.translate_post(path, body)
        log_fields["op"] = translated.op_name
        op_cls = get_operation(translated.op_name)
        params = parse_params(op_cls.Params, translated.params, op_name=translated.op_name)
        actor = _browser_actor(token)  # the body's actor, if any, is ignored
        log_fields["actor"] = actor
        caller = Caller(
            actor=actor,
            origin={
                "op_id": op_id,
                "reported": {"source": "browser"},
                "authenticated": token.authenticated_origin(),
            },
        )
        write = WriteRequest(
            op=translated.op_name,
            params=params,
            caller=caller,
            token_id=token.id,
            fp=fingerprint(translated.op_name, translated.params, actor, None, {}, None),
            authorize=lambda identity, _caller: token.authorize_actor(identity),
            minted=sent_op_id is None,
        )
        state.disk.check()

        def work() -> Any:
            with project.locked():
                project.admit()
                return project.run_write(write)

        try:
            async with state.registry.admitted(project):
                outcome = await in_worker(work)
        except OpError as exc:
            log_fields["error_code"] = exc.code
            refused = api.write_error(translated, exc)
            return _json_response(api.ApiResponse(refused.status, refused.envelope()))
        log_fields["seq"] = outcome.seq
        if outcome.replayed:
            log_fields["replayed"] = True
        result = outcome.result or _result_from(outcome.result_data)
        status, data = translated.render(result)
        return _json_response(api.ok(data, status))

    return await _limited(state, token, run)
