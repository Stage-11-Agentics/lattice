"""The browser side of the server: login, sessions, the index, and the page
headers (SPEC §10).

- ``GET /login`` serves a form, or redirects to ``next`` when the request
  already authenticates; ``POST /login`` (a form post) checks the ``Origin``
  first, then the submitted token, and sets ``lattice_session`` (``HttpOnly;
  SameSite=Lax; Path=/``, plus ``Secure`` over HTTPS). ``Lax`` keeps the
  session on a link followed from another site; every cookie-authenticated
  write checks ``Origin`` before the session, and no such GET writes.
- ``POST /logout`` is a JSON post (``{}``) from ``/web/logout.js``; the
  ``Origin`` is checked before the session is looked up or the body read.
- ``GET /`` is the projects overview (:mod:`lattice.server.overview`): the
  projects the session's token may see, one row each.
- Every response outside ``/v1`` carries ``nosniff`` and the CSP below, whose
  script hashes are computed from the dashboard page's inline ``<script>``
  blocks when the app is built.

A session cookie authenticates only ``/``, ``/logout``, ``/p/<slug>/...``, and
the stream. No other ``/v1`` handler reads cookies: they take ``Authorization:
Bearer`` alone. On those same routes, a request with no live cookie and no
``Authorization`` may instead be identified by Tailscale (``server.json``
``tailnet``, :mod:`lattice.server.tailnet`), as the grant's token.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import html
import re
import time
import urllib.parse
from pathlib import Path
from typing import TYPE_CHECKING, Any

from starlette.requests import Request
from starlette.responses import JSONResponse, RedirectResponse, Response

from lattice.core.errors import OpError
from lattice.dashboard.server import STATIC_DIR
from lattice.server import overview
from lattice.server.project import LOADED
from lattice.server.registry import in_worker
from lattice.server.sessions import COOKIE_NAME, SESSION_SECONDS, Session, unauthenticated
from lattice.server.filing_guard import require_filing_route
from lattice.server.tailnet import TAILNET_STATE_KEY
from lattice.server.tokens import TOKEN_STATE_KEY, TokenRecord

if TYPE_CHECKING:
    from lattice.server.app import ServerState

CSP_TEMPLATE = (
    "default-src 'self'; script-src 'self'{hashes}; style-src 'self' 'unsafe-inline'; "
    "img-src 'self' blob: data:; media-src 'self' blob:; connect-src 'self'; "
    "frame-ancestors 'none'; base-uri 'none'; "
    "form-action 'self'"
)

_INLINE_SCRIPT_RE = re.compile(r"<script>(.*?)</script>", re.S)
_STYLE_RE = re.compile(r"<style>(.*?)</style>", re.S)
_NEXT_RE = re.compile(r"^/p/[a-z0-9][a-z0-9-]{0,62}/$")
_LOGIN_BODY_LIMIT = 4096

LOGOUT_JS = """\
// Logout: a same-origin JSON POST (SPEC §10), then back to the login form.
// Loaded by the index page and injected by a hosted dashboard page, so it
// attaches whether or not the document has finished loading.
(function () {
  function attach() {
    var button = document.getElementById("logout");
    if (!button) return;
    button.addEventListener("click", function () {
      fetch("/logout", {
        method: "POST",
        credentials: "same-origin",
        headers: { "Content-Type": "application/json" },
        body: "{}",
      }).then(function () {
        window.location.href = "/login";
      });
    });
  }
  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", attach);
  } else {
    attach();
  }
})();
"""


# ---------------------------------------------------------------------------
# Headers
# ---------------------------------------------------------------------------


def inline_script_hashes(page: str) -> list[str]:
    """One ``'sha256-…'`` source per inline ``<script>`` block of *page*."""
    out = []
    for match in _INLINE_SCRIPT_RE.finditer(page):
        digest = hashlib.sha256(match.group(1).encode("utf-8")).digest()
        out.append(f"'sha256-{base64.b64encode(digest).decode('ascii')}'")
    return out


def build_csp(page: str) -> str:
    hashes = "".join(f" {h}" for h in inline_script_hashes(page))
    return CSP_TEMPLATE.format(hashes=hashes)


class WebAssets:
    """The dashboard page and what the server derives from it, read once."""

    def __init__(self, static_dir: Path = STATIC_DIR) -> None:
        self.static_dir = static_dir
        self.page = (static_dir / "index.html").read_text(encoding="utf-8")
        self.csp = build_csp(self.page)
        match = _STYLE_RE.search(self.page)
        self.stylesheet = match.group(1) if match else ""
        icon = static_dir / "favicon.svg"
        self.favicon: bytes | None = icon.read_bytes() if icon.is_file() else None


def is_hosted_api(path: str) -> bool:
    """``/p/<slug>/api/...``: a hosted dashboard API path (``no-store``)."""
    parts = path.split("/", 4)
    return len(parts) >= 4 and parts[1] == "p" and parts[3] == "api"


def page_headers(path: str, csp: str) -> list[tuple[bytes, bytes]]:
    """The headers SPEC §10 adds outside ``/v1``: ``nosniff`` and the CSP,
    ``no-store`` on hosted API and bearer-secret reporter routes, and
    ``Cross-Origin-Resource-Policy: same-origin`` on ``/p/`` (no other site may
    embed a dashboard's responses, whatever authenticated them)."""
    if path == "/v1" or path.startswith("/v1/"):
        return []
    extra = [
        (b"x-content-type-options", b"nosniff"),
        (b"content-security-policy", csp.encode("ascii")),
    ]
    if is_hosted_api(path) or path == "/r" or path.startswith("/r/"):
        extra.append((b"cache-control", b"no-store"))
    if path == "/r" or path.startswith("/r/"):
        extra.append((b"referrer-policy", b"no-referrer"))
    if path.startswith("/p/"):
        extra.append((b"cross-origin-resource-policy", b"same-origin"))
    return extra


# ---------------------------------------------------------------------------
# Origin and scheme
# ---------------------------------------------------------------------------


def request_scheme(request: Request, state: ServerState) -> str:
    """``https`` or ``http``. uvicorn has already applied ``X-Forwarded-Proto``
    when, and only when, the peer is in ``trusted_proxies`` (SPEC §8.1)."""
    return "https" if request.url.scheme == "https" else "http"


def origin_allowed(request: Request, state: ServerState) -> bool:
    """SPEC §10: ``Origin`` equals the request's own origin or is listed in
    ``public_origins``. A missing or ``null`` origin is refused."""
    origin = request.headers.get("origin")
    if not origin or origin == "null":
        return False
    if origin in state.config.public_origins:
        return True
    host = request.headers.get("host")
    return bool(host) and origin == f"{request_scheme(request, state)}://{host}"


def require_origin(request: Request, state: ServerState) -> None:
    if not origin_allowed(request, state):
        origin = request.headers.get("origin")
        raise OpError(
            "FORBIDDEN",
            f"cross-origin request refused: Origin {origin!r} is not this server.",
        )


def content_type(request: Request) -> str:
    return request.headers.get("content-type", "").split(";")[0].strip().lower()


def require_json(request: Request) -> None:
    if content_type(request) != "application/json":
        raise OpError("VALIDATION_ERROR", "POST requires Content-Type: application/json")


# ---------------------------------------------------------------------------
# Sessions
# ---------------------------------------------------------------------------


def session_cookie(request: Request) -> str | None:
    return request.cookies.get(COOKIE_NAME)


#: Set on a request whose session cookie failed; the headers middleware then
#: answers with a clearing ``Set-Cookie`` whatever the response is.
CLEAR_COOKIE = "lattice_clear_session"


def tailnet_session(request: Request, state: ServerState) -> tuple[Session, TokenRecord] | None:
    """The request's Tailscale identity as an unstored session, or ``None``.
    The grant's token must pass the bearer rule (``TokenStore.live``) and not
    be filing-only."""
    if request.headers.get("authorization") is not None:
        return None
    identity = request.scope.get("state", {}).get(TAILNET_STATE_KEY)
    if identity is None:
        return None
    token = state.tokens.live(identity.grant.token)
    if token is None or token.filing_only:
        return None
    session = Session(
        sha256="", token_id=token.id, created_at="", expires_at="", tailnet_node=identity.node
    )
    return session, token


def session_auth(request: Request, state: ServerState) -> tuple[Session, TokenRecord]:
    """The request's session and its live token, or ``UNAUTHENTICATED``. A live
    cookie comes first, then the request's Tailscale identity. A cookie that
    names no live session (expired, revoked, deleted, malformed) is cleared on
    the response, unless the request sent ``Authorization`` (which alone
    decides, so the cookie was never the credential in question)."""
    cookie = session_cookie(request)
    try:
        session, token = state.sessions.authenticate(cookie)
    except OpError:
        if cookie is not None and request.headers.get("authorization") is None:
            secure = request_scheme(request, state) == "https"
            request.scope["state"][CLEAR_COOKIE] = _cookie_header("", secure=secure, max_age=0)
        via_tailnet = tailnet_session(request, state)
        if via_tailnet is None:
            raise
        session, token = via_tailnet
        request.scope["state"]["log"]["tailnet"] = session.tailnet_node
    cached = request.scope.setdefault("state", {}).get(TOKEN_STATE_KEY)
    if cached is not None and cached.id == token.id:
        token = cached
    require_filing_route(
        request.method,
        request.scope.get("path", ""),
        token,
        request.scope.get("raw_path"),
    )
    if token.filing_only:
        raise OpError("TOKEN_RESTRICTED", "filing-only tokens cannot create dashboard sessions")
    request.scope["state"]["log"]["token_id"] = token.id
    return session, token


def _cookie_header(value: str, *, secure: bool, max_age: int) -> str:
    parts = [f"{COOKIE_NAME}={value}", "HttpOnly", "SameSite=Lax", "Path=/"]
    parts.append(f"Max-Age={max_age}")
    if secure:
        parts.append("Secure")
    return "; ".join(parts)


def set_session_cookie(response: Response, value: str, *, secure: bool) -> None:
    response.headers.append(
        "set-cookie", _cookie_header(value, secure=secure, max_age=SESSION_SECONDS)
    )


def clear_session_cookie(response: Response, *, secure: bool) -> None:
    response.headers.append("set-cookie", _cookie_header("", secure=secure, max_age=0))


# ---------------------------------------------------------------------------
# Pages
# ---------------------------------------------------------------------------


def _page(title: str, body: str) -> str:
    return (
        '<!DOCTYPE html>\n<html lang="en">\n<head>\n<meta charset="utf-8">\n'
        '<meta name="viewport" content="width=device-width, initial-scale=1">\n'
        f"<title>{html.escape(title)}</title>\n"
        '<link rel="stylesheet" href="/web/dashboard.css">\n'
        "<style>\n"
        "body{margin:0;padding:2rem;background:var(--bg-base);color:var(--text-primary);"
        "font-family:system-ui,sans-serif}\n"
        ".web-box{max-width:32rem;margin:3rem auto;padding:1.5rem;background:var(--surface);"
        "border:1px solid var(--border);border-radius:8px}\n"
        ".web-box input[type=password]{width:100%;box-sizing:border-box;padding:.5rem;"
        "margin:.5rem 0 1rem}\n"
        ".web-box ul{list-style:none;padding:0}.web-box li{padding:.4rem 0}\n"
        ".web-box a{color:var(--accent-primary)}\n"
        ".web-muted{color:var(--text-muted)}\n"
        "</style>\n</head>\n<body>\n"
        f'<div class="web-box">\n{body}\n</div>\n</body>\n</html>\n'
    )


def _html(text: str, status: int = 200) -> Response:
    return Response(text, status_code=status, media_type="text/html; charset=utf-8")


def _safe_next(value: str | None) -> str:
    return value if value and (value == "/" or _NEXT_RE.fullmatch(value)) else "/"


def login_form(next_path: str = "/", message: str | None = None) -> str:
    note = f'<p class="web-muted">{html.escape(message)}</p>\n' if message else ""
    return _page(
        "Lattice login",
        "<h1>Lattice</h1>\n"
        f"{note}"
        '<form method="post" action="/login">\n'
        '<label for="token">Token</label>\n'
        '<input type="password" id="token" name="token" autocomplete="off" required>\n'
        f'<input type="hidden" name="next" value="{html.escape(_safe_next(next_path))}">\n'
        '<button type="submit">Log in</button>\n'
        "</form>",
    )


async def login_page(request: Request, state: ServerState) -> Response:
    """The form, unless the request already authenticates (a live cookie or a
    tailnet identity): then straight on to ``next``."""
    next_path = request.query_params.get("next")
    try:
        session_auth(request, state)
    except OpError:
        return _html(login_form(next_path))
    return RedirectResponse(_safe_next(next_path), status_code=303)


async def login(request: Request, state: ServerState) -> Response:
    """``POST /login``: Origin (403), then the form (``token``, ``next``), then the
    token (401). Nothing is written unless all three pass."""
    require_origin(request, state)
    if content_type(request) != "application/x-www-form-urlencoded":
        raise OpError("VALIDATION_ERROR", "login is a form post")
    declared = request.headers.get("content-length")
    if declared is not None and (not declared.isdigit() or int(declared) > _LOGIN_BODY_LIMIT):
        raise OpError("PAYLOAD_TOO_LARGE", "login form is too large")
    raw = bytearray()
    async for chunk in request.stream():
        raw.extend(chunk)
        if len(raw) > _LOGIN_BODY_LIMIT:
            raise OpError("PAYLOAD_TOO_LARGE", "login form is too large")
    # The 4 KiB cap above bounds the parse; a field-count cap would raise.
    form = urllib.parse.parse_qs(raw.decode("utf-8", errors="replace"))
    submitted = (form.get("token") or [""])[0].strip()
    next_path = _safe_next((form.get("next") or ["/"])[0])
    try:
        token = state.tokens.authenticate(f"Bearer {submitted}")
    except OpError:
        return _html(login_form(next_path, "That token is not valid."), status=401)
    if token.filing_only:
        raise OpError("TOKEN_RESTRICTED", "filing-only tokens cannot create dashboard sessions")
    request.scope["state"]["log"]["token_id"] = token.id
    cookie = await in_worker(lambda: state.sessions.create(token))
    state.log.info("login", token_id=token.id)
    response = RedirectResponse(next_path, status_code=303)
    set_session_cookie(response, cookie, secure=request_scheme(request, state) == "https")
    return response


async def logout(request: Request, state: ServerState) -> Response:
    """``POST /logout`` with ``{}``: Origin, then JSON, then the session (401)."""
    require_origin(request, state)
    require_json(request)
    session, token = session_auth(request, state)
    if session.tailnet_node is None:  # a tailnet visit has no stored session to end
        await in_worker(lambda: state.sessions.delete(session))
        state.log.info("logout", token_id=token.id)
    response = JSONResponse({"ok": True, "data": {"logged_out": True}})
    clear_session_cookie(response, secure=request_scheme(request, state) == "https")
    return response


def _summarize(
    state: ServerState, project: Any
) -> tuple[str | None, overview.BoardSummary | None]:
    """Under the project's work lock, as every board read (SPEC §8.5): the project
    code, and the board's summary at its head (memoized with the dashboard's
    reads), or ``None`` when the board is unavailable."""
    loaded = project.state == LOADED
    if loaded:
        try:
            project.admit()  # hand edits journaled first, as /v1/projects does
        except OpError as exc:
            state.log.warning("overview_board_skipped", project=project.slug, error_code=exc.code)
            loaded = False
    code = overview.project_code(project.board)
    journal = project.journal
    if not loaded or journal is None:
        return code, None
    memo = state.dashboard_memos.for_project(project.slug)
    head = (journal.epoch, journal.head_seq)
    try:
        summary = memo.get(
            journal, head, overview.MEMO_KEY, lambda: overview.summarize_board(project.board)
        )
    except Exception as exc:  # noqa: BLE001 - one unreadable board never hides the rest
        state.log.error("overview_board_failed", project=project.slug, error=repr(exc))
        return code, None
    return code, summary


async def board_row(state: ServerState, slug: str) -> overview.Row | None:
    """One project's overview row (``None`` when no such project is registered),
    read under its work lock. A lock timeout reads as busy, a load failure as
    unavailable; both are logged."""
    project = state.registry.get(slug)
    if project is None:
        return None
    try:
        code, summary = await state.registry.run_locked(
            project, lambda: _summarize(state, project), admit=False
        )
    except OpError as exc:  # its lock timed out, or it failed to load
        state.log.warning("overview_board_skipped", project=slug, error_code=exc.code)
        busy = "busy" if exc.code == "BOARD_BUSY" else "unavailable"
        return overview.Row(slug=slug, code=None, summary=None, state=busy)
    return overview.Row(slug=slug, code=code, summary=summary)


async def warm_overview(state: ServerState) -> None:
    """Once the startup prewarm has loaded the projects, compute every board's
    overview summary in the background, one board's work lock at a time, so the
    first ``GET /`` after a restart finds them memoized (and each dashboard its
    replays cached). Logs ``overview_warmed``; a failure is logged, never raised."""
    try:
        await state.registry.prewarm_done.wait()
        started = time.monotonic()
        slugs = await in_worker(state.registry.slugs)
        warmed = 0
        for slug in slugs:
            try:
                row = await board_row(state, slug)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - one board never leaves the rest cold
                state.log.error("overview_board_failed", project=slug, error=repr(exc))
                continue
            if row is not None and row.summary is not None:
                warmed += 1
        state.log.info(
            "overview_warmed",
            boards=warmed,
            projects=len(slugs),
            duration_ms=round((time.monotonic() - started) * 1000, 1),
        )
    except asyncio.CancelledError:
        raise
    except Exception as exc:  # noqa: BLE001 - warming is an optimization; never stop the server
        state.log.error("overview_warm_failed", error=repr(exc))
    finally:
        state.overview_warm_done.set()


async def index(request: Request, state: ServerState) -> Response:
    """``GET /``: the projects overview (:mod:`lattice.server.overview`), one row
    per project the session's token may see, each linking to its dashboard."""
    try:
        session, token = session_auth(request, state)
    except OpError:
        return RedirectResponse("/login", status_code=303)
    sort = overview.normalize_sort(request.query_params.get("sort"))

    rows: list[overview.Row] = []
    slugs = await in_worker(state.registry.slugs)
    for slug in slugs:
        if not token.permits_project(slug):
            continue
        row = await board_row(state, slug)
        if row is not None:
            rows.append(row)

    signed_in = f"Signed in as {token.user} ({token.machine})"
    if session.tailnet_node is not None:
        signed_in += f" through Tailscale ({session.tailnet_node})"
    page = overview.render(
        overview.sort_rows(rows, sort),
        sort=sort,
        signed_in=signed_in,
        logout=session.tailnet_node is None,  # a tailnet visit has no session to end
        version=state.version,
        at=overview.now(),
        server_name=state.config.name,
    )
    return _html(page)


async def web_asset(request: Request, state: ServerState) -> Response:
    name = request.path_params["name"]
    if name == "logout.js":
        return Response(LOGOUT_JS, media_type="application/javascript; charset=utf-8")
    if name == "overview.js":
        return Response(overview.OVERVIEW_JS, media_type="application/javascript; charset=utf-8")
    if name == "dashboard.css":
        return Response(state.web.stylesheet, media_type="text/css; charset=utf-8")
    if name == "favicon.svg" and state.web.favicon is not None:
        return Response(state.web.favicon, media_type="image/svg+xml")
    raise OpError("NOT_FOUND", f"no asset {name}")


__all__ = [
    "CSP_TEMPLATE",
    "WebAssets",
    "build_csp",
    "clear_session_cookie",
    "index",
    "inline_script_hashes",
    "login",
    "login_page",
    "logout",
    "origin_allowed",
    "page_headers",
    "require_origin",
    "session_auth",
    "tailnet_session",
    "unauthenticated",
    "web_asset",
]
