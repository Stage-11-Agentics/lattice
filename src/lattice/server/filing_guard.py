"""The deliberately narrow HTTP surface of filing-only tokens."""

from __future__ import annotations

import re

from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Receive, Scope, Send

from lattice.core.errors import OpError
from lattice.server.tokens import TOKEN_STATE_KEY, TokenRecord, TokenStore

_ISSUE_FILE_PATH = re.compile(r"^/v1/projects/[^/]+/ops/issue\.file$")
_STAGING_PATH = re.compile(r"^/v1/projects/[^/]+/issues/media/staging/[0-9a-f]{64}$")


def filing_route_allowed(
    method: str, path: str, token: TokenRecord, raw_path: bytes | None = None
) -> bool:
    """Whether the exact ASGI method/path representation is one of two filing routes.

    Project authorization remains in ``resolve_project`` so a well-shaped request
    against another project keeps the ordinary ``FORBIDDEN`` response.
    """
    if raw_path is not None and raw_path != path.encode("utf-8"):
        return False
    return (method == "POST" and _ISSUE_FILE_PATH.fullmatch(path) is not None) or (
        method == "PUT" and _STAGING_PATH.fullmatch(path) is not None
    )


def require_filing_route(
    method: str, path: str, token: TokenRecord, raw_path: bytes | None = None
) -> None:
    if token.filing_only and not filing_route_allowed(method, path, token, raw_path):
        raise OpError(
            "TOKEN_RESTRICTED",
            "this token may only file issues and upload their staged media",
            {"only": ["issue.file"]},
        )


class FilingTokenGuard:
    """Authenticate bearer credentials once and fail closed on restricted routes."""

    def __init__(self, app: ASGIApp, tokens: TokenStore) -> None:
        self.app = app
        self.tokens = tokens

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        # Reporter links authenticate from their server-root secret and always
        # own their generic 404 surface. Do not inspect bearer or session state.
        if scope.get("path", "") == "/r" or scope.get("path", "").startswith("/r/"):
            await self.app(scope, receive, send)
            return
        state = scope.setdefault("state", {})
        authorization = next(
            (
                value
                for name, value in scope.get("headers", ())
                if name.lower() == b"authorization"
            ),
            None,
        )
        token = None
        if authorization is not None:
            try:
                token = self.tokens.authenticate(authorization.decode("latin-1"))
            except OpError:
                # Protected handlers retain their existing uniform 401 path.
                token = None
        if token is not None:
            state[TOKEN_STATE_KEY] = token
            if token.filing_only:
                try:
                    require_filing_route(
                        scope.get("method", ""),
                        scope.get("path", ""),
                        token,
                        scope.get("raw_path"),
                    )
                except OpError as exc:
                    state.setdefault("log", {})["token_id"] = token.id
                    response = JSONResponse(
                        {"ok": False, "error": exc.to_dict()}, status_code=exc.http_status
                    )
                    await response(scope, receive, send)
                    return
        await self.app(scope, receive, send)
