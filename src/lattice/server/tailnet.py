"""Dashboard sign-in by Tailscale identity (SPEC §10, ``server.json`` ``tailnet``).

A browser on a granted Tailscale node opens the dashboard with no token and no
cookie. :class:`TailnetMiddleware` resolves the identity before routing, for
requests without ``Authorization`` on the routes a session authenticates;
``web.session_auth`` uses it when no live session cookie came with the request.

A request is identified only when all of these hold:

1. Its ``Host`` header is listed in ``tailnet.hosts``. The identity follows the
   connection, not the page that made the request, so without this check a
   site whose name was rebound to the server's address (DNS rebinding) could
   read the dashboard with the visitor's identity; that site's requests carry
   its own name as ``Host``.
2. It is not a cross-site subresource request: with ``Sec-Fetch-Site`` other
   than ``same-origin`` or ``none``, only a top-level navigation
   (``Sec-Fetch-Mode: navigate`` and ``Sec-Fetch-Dest: document``) is
   identified, so another site can link to a board but cannot frame, embed or
   fetch from it with the visitor's identity. Browsers that send no Fetch
   Metadata are held back by ``Cross-Origin-Resource-Policy: same-origin`` on
   ``/p/`` and ``frame-ancestors 'none'``.
3. It came straight from the client: a request carrying ``X-Forwarded-For``
   is identified only with ``trust_forwarded``, because every process able to
   connect from a ``trusted_proxies`` address can write that header.
4. Its client address is a Tailscale address (``100.64.0.0/10`` or
   ``fd7a:115c:a1e0::/48``): the peer, or with ``trust_forwarded`` what a
   listed proxy forwarded (SPEC §8.1).
5. ``tailscale whois`` names a node and login that a grant lists. The grant's
   token decides projects, the browser actor and revocation, exactly as a
   session minted from that token would.

The device is the principal: every process on a granted node (local agents,
containers whose traffic leaves through its tailnet address) is identified.

Answers are cached per address for ``cache_seconds`` (a failed lookup for at
most :data:`FAILURE_SECONDS`), and concurrent misses for one address share
one lookup, so a page load's many requests cost one ``whois``.
"""

from __future__ import annotations

import asyncio
import ipaddress
import json
import re
import subprocess
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from starlette.types import ASGIApp, Receive, Scope, Send

from lattice.server.config import TailnetConfig, TailnetGrant
from lattice.server.log import describe_error
from lattice.server.registry import in_worker

#: The ``scope["state"]`` key holding a request's :class:`TailnetIdentity`.
TAILNET_STATE_KEY = "lattice_tailnet_identity"

TAILNET_V4 = ipaddress.ip_network("100.64.0.0/10")
TAILNET_V6 = ipaddress.ip_network("fd7a:115c:a1e0::/48")
WHOIS_TIMEOUT_SECONDS = 3.0
#: The longest a failed lookup (tailscaled down, a timeout) is remembered.
FAILURE_SECONDS = 10.0
_CACHE_MAX = 1024
_SAME_ORIGIN_FETCH = frozenset({"same-origin", "none"})

#: The routes a session authenticates (SPEC §10), the only ones resolved.
_BROWSER_PATH_RE = re.compile(r"^(/|/login|/logout|/p/.*|/v1/projects/[^/]+/stream)$")


class WhoisError(Exception):
    """``tailscale whois`` could not answer (as opposed to "no such node")."""


@dataclass(frozen=True)
class WhoisAnswer:
    """What tailscaled says about one address."""

    #: The node's MagicDNS name, lowercased, without the trailing dot.
    node: str
    stable_id: str
    login: str


@dataclass(frozen=True)
class TailnetIdentity:
    """A request's resolved identity: the node and the grant it matched."""

    node: str
    grant: TailnetGrant


def tailnet_address(host: str | None) -> str | None:
    """*host* normalized when it is a Tailscale address, else ``None``. An
    IPv4-mapped IPv6 address (a dual-stack listener's view) counts as IPv4."""
    if not host:
        return None
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        return None
    if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped is not None:
        address = address.ipv4_mapped
    network = TAILNET_V4 if address.version == 4 else TAILNET_V6
    return str(address) if address in network else None


def parse_whois(text: str) -> WhoisAnswer:
    """``tailscale whois --json`` output → :class:`WhoisAnswer`."""
    try:
        raw = json.loads(text)
        node = raw["Node"]
        name = str(node["Name"]).rstrip(".").lower()
        stable_id = str(node.get("StableID") or "")
        login = str(raw["UserProfile"]["LoginName"])
    except (ValueError, KeyError, TypeError) as exc:
        raise WhoisError(f"unreadable whois answer: {describe_error(exc)}") from exc
    if not name or not login:
        raise WhoisError("whois answer names no node or login")
    return WhoisAnswer(node=name, stable_id=stable_id, login=login)


def run_whois(tailscale: str, address: str) -> WhoisAnswer | None:
    """Ask tailscaled about *address*: an answer, ``None`` when no node holds
    it, or :class:`WhoisError` when tailscaled could not be asked."""
    try:
        done = subprocess.run(
            [tailscale, "whois", "--json", address],
            capture_output=True,
            text=True,
            timeout=WHOIS_TIMEOUT_SECONDS,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise WhoisError(describe_error(exc)) from exc
    if done.returncode != 0:
        if "peer not found" in done.stderr.lower() or "no match" in done.stderr.lower():
            return None
        detail = done.stderr.strip().splitlines()[-1:] or [f"exit {done.returncode}"]
        raise WhoisError(detail[0][:200])
    return parse_whois(done.stdout)


class TailnetResolver:
    """The running server's view of ``tailnet``: host check, whois, cache."""

    def __init__(
        self,
        config: TailnetConfig,
        *,
        whois: Callable[[str], WhoisAnswer | None] | None = None,
        on_error: Callable[..., None] | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.config = config
        self.hosts = frozenset(config.hosts)
        self._whois = whois or (lambda address: run_whois(config.tailscale, address))
        self.on_error = on_error
        self._clock = clock
        self._cache: dict[str, tuple[float, WhoisAnswer | None]] = {}
        self._inflight: dict[str, asyncio.Future] = {}

    def host_allowed(self, scope: Scope) -> bool:
        return _header(scope, b"host").strip().lower() in self.hosts

    def request_eligible(self, scope: Scope) -> bool:
        """Rules 1 to 3 of the module docstring: everything but the address."""
        if not self.host_allowed(scope):
            return False
        site = _header(scope, b"sec-fetch-site").strip().lower()
        if site and site not in _SAME_ORIGIN_FETCH:
            mode = _header(scope, b"sec-fetch-mode").strip().lower()
            dest = _header(scope, b"sec-fetch-dest").strip().lower()
            if mode != "navigate" or dest not in ("document", ""):
                return False
        if not self.config.trust_forwarded and _has_header(scope, b"x-forwarded-for"):
            return False
        return True

    def match(self, answer: WhoisAnswer) -> TailnetGrant | None:
        for grant in self.config.grants:
            node_matches = grant.node.lower() == answer.node or (
                bool(answer.stable_id) and grant.node == answer.stable_id
            )
            if node_matches and grant.login == answer.login:
                return grant
        return None

    async def identify(self, scope: Scope) -> TailnetIdentity | None:
        """The request's identity, or ``None`` (never raises for a lookup failure)."""
        if not self.request_eligible(scope):
            return None
        client = scope.get("client")
        address = tailnet_address(client[0] if client else None)
        if address is None:
            return None
        answer = await self._lookup(address)
        if answer is None:
            return None
        grant = self.match(answer)
        return TailnetIdentity(node=answer.node, grant=grant) if grant else None

    async def _lookup(self, address: str) -> WhoisAnswer | None:
        cached = self._cache.get(address)
        if cached is not None and cached[0] > self._clock():
            return cached[1]
        pending = self._inflight.get(address)
        if pending is not None:
            return await asyncio.shield(pending)
        future: asyncio.Future = asyncio.get_running_loop().create_future()
        self._inflight[address] = future
        answer: WhoisAnswer | None = None
        try:
            try:
                answer = await in_worker(lambda: self._whois(address))
                ttl = self.config.cache_seconds
            except Exception as exc:  # noqa: BLE001 - a failed lookup identifies nobody
                ttl = min(FAILURE_SECONDS, self.config.cache_seconds)
                if self.on_error:
                    # tailscaled's own error text, never request data (SPEC §8.11)
                    detail = {"detail": str(exc)} if isinstance(exc, WhoisError) else {}
                    self.on_error(address=address, error=describe_error(exc), **detail)
            self._remember(address, answer, ttl)
            return answer
        finally:
            self._inflight.pop(address, None)
            if not future.done():
                future.set_result(answer)

    def _remember(self, address: str, answer: WhoisAnswer | None, ttl: float) -> None:
        if ttl <= 0:
            return
        now = self._clock()
        if len(self._cache) >= _CACHE_MAX:
            self._cache = {k: v for k, v in self._cache.items() if v[0] > now}
            while len(self._cache) >= _CACHE_MAX:  # still full: drop the soonest to expire
                del self._cache[min(self._cache, key=lambda k: self._cache[k][0])]
        self._cache[address] = (now + ttl, answer)


def _header(scope: Scope, name: bytes) -> str:
    for key, value in scope.get("headers") or ():
        if key.lower() == name:
            return value.decode("latin-1")
    return ""


def _has_header(scope: Scope, name: bytes) -> bool:
    return any(key.lower() == name for key, _ in scope.get("headers") or ())


class TailnetMiddleware:
    """Resolve the Tailscale identity of a browser request into its scope:
    only on the routes a session authenticates (``/login`` for GET only), only
    without ``Authorization``, and only when no live session cookie came along
    (the cookie wins, so the lookup would be wasted)."""

    def __init__(
        self, app: ASGIApp, resolver: TailnetResolver | None, sessions: Any, log: Any
    ) -> None:
        self.app = app
        self.resolver = resolver
        self.sessions = sessions
        self.log = log

    def _live_cookie(self, scope: Scope) -> bool:
        from starlette.requests import cookie_parser

        from lattice.core.errors import OpError
        from lattice.server.sessions import COOKIE_NAME

        cookie = cookie_parser(_header(scope, b"cookie")).get(COOKIE_NAME)
        if cookie is None:
            return False
        try:
            self.sessions.authenticate(cookie)
        except OpError:
            return False
        return True

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        path = scope.get("path", "") if scope["type"] == "http" else ""
        if (
            self.resolver is not None
            and _BROWSER_PATH_RE.fullmatch(path)
            and not (path == "/login" and scope.get("method") != "GET")
            and not _has_header(scope, b"authorization")
            and self.resolver.request_eligible(scope)
            and not self._live_cookie(scope)  # last: the costliest check
        ):
            try:
                identity = await self.resolver.identify(scope)
            except Exception as exc:  # noqa: BLE001 - fail closed: the request goes on unidentified
                self.log.emit("error", "tailnet_identify_failed", error=describe_error(exc))
                identity = None
            if identity is not None:
                scope.setdefault("state", {})[TAILNET_STATE_KEY] = identity
        await self.app(scope, receive, send)


__all__ = [
    "TAILNET_STATE_KEY",
    "TailnetIdentity",
    "TailnetMiddleware",
    "TailnetResolver",
    "WhoisAnswer",
    "WhoisError",
    "parse_whois",
    "run_whois",
    "tailnet_address",
]
