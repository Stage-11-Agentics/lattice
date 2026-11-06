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
2. Its client address is a Tailscale address (``100.64.0.0/10`` or
   ``fd7a:115c:a1e0::/48``). The client address is the peer, or what a listed
   ``trusted_proxies`` peer forwarded (SPEC §8.1).
3. ``tailscale whois`` names a node and login that a grant lists. The grant's
   token decides projects, the browser actor and revocation, exactly as a
   session minted from that token would.

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
_CACHE_PRUNE_AT = 1024

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
        for name, value in scope.get("headers") or ():
            if name.lower() == b"host":
                return value.decode("latin-1").strip().lower() in self.hosts
        return False

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
        if not self.host_allowed(scope):
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
        now = self._clock()
        if len(self._cache) >= _CACHE_PRUNE_AT:
            self._cache = {k: v for k, v in self._cache.items() if v[0] > now}
        if ttl > 0:
            self._cache[address] = (now + ttl, answer)


def _has_authorization(scope: Scope) -> bool:
    return any(name.lower() == b"authorization" for name, _ in scope.get("headers") or ())


class TailnetMiddleware:
    """Resolve the Tailscale identity of a browser request into its scope."""

    def __init__(self, app: ASGIApp, resolver: TailnetResolver | None, log: Any) -> None:
        self.app = app
        self.resolver = resolver
        self.log = log

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if (
            self.resolver is not None
            and scope["type"] == "http"
            and _BROWSER_PATH_RE.fullmatch(scope.get("path", ""))
            and not _has_authorization(scope)
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
