"""The browser side of the server: login, sessions, the index, and the page
headers (SPEC §10).

- ``GET /login`` serves a form; ``POST /login`` (a form post) checks the
  ``Origin`` first, then the submitted token, and sets ``lattice_session``
  (``HttpOnly; SameSite=Strict; Path=/``, plus ``Secure`` over HTTPS).
- ``POST /logout`` is a JSON post (``{}``) from ``/web/logout.js``; the
  ``Origin`` is checked before the session is looked up or the body read.
- ``GET /`` lists the projects the session's token may see.
- Every response outside ``/v1`` carries ``nosniff`` and the CSP below, whose
  script hashes are computed from the dashboard page's inline ``<script>``
  blocks when the app is built.

A session cookie authenticates only ``/``, ``/logout``, ``/p/<slug>/...``, and
the stream. No other ``/v1`` handler reads cookies: they take ``Authorization:
Bearer`` alone.
"""

from __future__ import annotations

import base64
import hashlib
import html
import re
import urllib.parse
from pathlib import Path
from typing import TYPE_CHECKING, Any

from starlette.requests import Request
from starlette.responses import JSONResponse, RedirectResponse, Response

from lattice.core.errors import OpError
from lattice.dashboard.server import STATIC_DIR
from lattice.server.project import LOADED
from lattice.server.registry import in_worker
from lattice.server.sessions import COOKIE_NAME, SESSION_SECONDS, Session, unauthenticated
from lattice.server.tokens import TokenRecord

if TYPE_CHECKING:
    from lattice.server.app import ServerState

CSP_TEMPLATE = (
    "default-src 'self'; script-src 'self'{hashes}; style-src 'self' 'unsafe-inline'; "
    "img-src 'self'; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; "
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


def is_hosted_api(path: str) -> bool:
    """``/p/<slug>/api/...``: a hosted dashboard API path (``no-store``)."""
    parts = path.split("/", 4)
    return len(parts) >= 4 and parts[1] == "p" and parts[3] == "api"


def page_headers(path: str, csp: str) -> list[tuple[bytes, bytes]]:
    """The headers SPEC §10 adds outside ``/v1``: ``nosniff`` and the CSP, and
    ``no-store`` on hosted API responses."""
    if path == "/v1" or path.startswith("/v1/"):
        return []
    extra = [
        (b"x-content-type-options", b"nosniff"),
        (b"content-security-policy", csp.encode("ascii")),
    ]
    if is_hosted_api(path):
        extra.append((b"cache-control", b"no-store"))
    return extra


# ---------------------------------------------------------------------------
# Origin and scheme
# ---------------------------------------------------------------------------


def request_scheme(request: Request, state: ServerState) -> str:
    """``https`` or ``http``; ``X-Forwarded-Proto`` counts only under ``trusted_proxy``."""
    if state.config.trusted_proxy:
        forwarded = request.headers.get("x-forwarded-proto", "")
        first = forwarded.split(",")[0].strip().lower()
        if first in ("http", "https"):
            return first
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


def session_auth(request: Request, state: ServerState) -> tuple[Session, TokenRecord]:
    """The request's session and its live token, or ``UNAUTHENTICATED``. A cookie
    that names no live session (expired, revoked, deleted, malformed) is
    cleared on the response, unless the request sent ``Authorization`` (which
    alone decides, so the cookie was never the credential in question)."""
    cookie = session_cookie(request)
    try:
        session, token = state.sessions.authenticate(cookie)
    except OpError:
        if cookie is not None and request.headers.get("authorization") is None:
            secure = request_scheme(request, state) == "https"
            request.scope["state"][CLEAR_COOKIE] = _cookie_header("", secure=secure, max_age=0)
        raise
    request.scope["state"]["log"]["token_id"] = token.id
    return session, token


def _cookie_header(value: str, *, secure: bool, max_age: int) -> str:
    parts = [f"{COOKIE_NAME}={value}", "HttpOnly", "SameSite=Strict", "Path=/"]
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
    return _html(login_form(request.query_params.get("next")))


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
    await in_worker(lambda: state.sessions.delete(session))
    state.log.info("logout", token_id=token.id)
    response = JSONResponse({"ok": True, "data": {"logged_out": True}})
    clear_session_cookie(response, secure=request_scheme(request, state) == "https")
    return response


async def index(request: Request, state: ServerState) -> Response:
    """``GET /``: the session token's projects, each linking to its dashboard."""
    try:
        _session, token = session_auth(request, state)
    except OpError:
        return RedirectResponse("/login", status_code=303)

    def project_code(project: Any) -> str | None:
        """Under the project's work lock, as every board read (SPEC §8.5)."""
        import json

        if project.state == LOADED:
            try:
                project.admit()  # hand edits journaled first, as /v1/projects does
            except OpError:
                pass
        try:
            config = json.loads((project.board / "config.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        code = config.get("project_code")
        return code if isinstance(code, str) else None

    listed: list[tuple[str, str | None]] = []
    slugs = await in_worker(state.registry.slugs)
    for slug in slugs:
        if not token.permits_project(slug):
            continue
        project = state.registry.get(slug)
        if project is None:
            continue
        try:
            code = await state.registry.run_locked(
                project, lambda p=project: project_code(p), admit=False
            )
        except OpError:
            code = None
        listed.append((slug, code))

    items = []
    for slug, code in listed:
        label = html.escape(slug) + (
            f' <span class="web-muted">{html.escape(code)}</span>' if code else ""
        )
        items.append(f'<li><a href="/p/{html.escape(slug)}/">{label}</a></li>')
    listing = "\n".join(items) if items else '<li class="web-muted">No projects.</li>'
    body = (
        "<h1>Lattice projects</h1>\n"
        f'<p class="web-muted">Signed in as {html.escape(token.user)} '
        f"({html.escape(token.machine)})</p>\n"
        f"<ul>\n{listing}\n</ul>\n"
        '<button type="button" id="logout">Log out</button>\n'
        '<script src="/web/logout.js"></script>'
    )
    return _html(_page("Lattice projects", body))


async def web_asset(request: Request, state: ServerState) -> Response:
    name = request.path_params["name"]
    if name == "logout.js":
        return Response(LOGOUT_JS, media_type="application/javascript; charset=utf-8")
    if name == "dashboard.css":
        return Response(state.web.stylesheet, media_type="text/css; charset=utf-8")
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
    "unauthenticated",
    "web_asset",
]
