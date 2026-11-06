"""Server root and ``server.json`` (SPEC §8.1).

Every key of ``server.json`` is optional; a missing key takes the default
shown in SPEC §8.1. An unknown key or a value of the wrong type is refused
when the server starts, so a typo never silently falls back to a default.
"""

from __future__ import annotations

import ipaddress
import json
import math
import os
import re
from dataclasses import dataclass, field, fields, replace
from pathlib import Path
from typing import Any

SERVER_JSON = "server.json"
TOKENS_JSON = "tokens.json"
WEB_SESSIONS_JSON = "web_sessions.json"
PROJECTS_DIR = "projects"
ADMIN_LOCK = "admin.lock"
SERVER_LOCK = "server.lock"
STATUS_JSON = "server_status.json"
ROOT_ENV = "LATTICE_SERVER_ROOT"

#: SPEC §8.1: ``limits.lock_timeout_seconds`` may not exceed this.
MAX_LOCK_TIMEOUT_SECONDS = 60

LOG_LEVELS = ("debug", "info", "warning")


class ServerConfigError(Exception):
    """``server.json`` is unreadable or holds an invalid key or value."""


def resolve_root(root: str | Path | None) -> Path:
    """``--root``, else ``$LATTICE_SERVER_ROOT``, else ``$XDG_DATA_HOME/lattice-server``."""
    if root:
        return Path(root).expanduser().absolute()
    env = os.environ.get(ROOT_ENV)
    if env:
        return Path(env).expanduser().absolute()
    data_home = os.environ.get("XDG_DATA_HOME") or str(Path.home() / ".local" / "share")
    return Path(data_home).expanduser().absolute() / "lattice-server"


@dataclass(frozen=True)
class Limits:
    max_body_bytes: int = 16 * 1024 * 1024
    inline_file_bytes: int = 1024 * 1024
    lock_timeout_seconds: int = 30
    max_inflight_per_token: int = 8
    token_ops_per_minute: int = 600
    token_body_bytes_per_minute: int = 256 * 1024 * 1024
    max_event_data_bytes: int = 64 * 1024
    max_stream_subscribers_per_project: int = 64
    stream_queue_entries: int = 1000
    replay_reset_entries: int = 1000
    min_free_disk_bytes: int = 1024 * 1024 * 1024
    max_issue_media_file_bytes: int = 100 * 1024 * 1024
    max_issue_media_issue_bytes: int = 250 * 1024 * 1024
    max_issue_media_project_bytes: int = 10 * 1024 * 1024 * 1024


@dataclass(frozen=True)
class AuditConfig:
    enabled: bool = True
    #: Seconds; fractions are allowed (tests use short ones).
    debounce_seconds: float = 5.0
    max_interval_seconds: float = 60.0
    push: dict | None = None


@dataclass(frozen=True)
class StreamConfig:
    heartbeat_seconds: int = 2


@dataclass(frozen=True)
class TailnetGrant:
    """One Tailscale node whose browser signs in as *token* (SPEC §10)."""

    #: The node's MagicDNS name (no trailing dot) or its StableID.
    node: str
    #: The node owner's Tailscale login name; it must match too.
    login: str
    #: The id (``tok_…``) of the token the visit authenticates as.
    token: str


@dataclass(frozen=True)
class TailnetConfig:
    """``server.json`` ``tailnet``: dashboard sign-in by Tailscale identity."""

    #: ``Host`` header values (``name`` or ``name:port``) the identity applies on.
    hosts: tuple[str, ...]
    grants: tuple[TailnetGrant, ...]
    #: The ``tailscale`` CLI that answers ``whois``.
    tailscale: str = "tailscale"
    #: How long one address's answer is kept, in seconds.
    cache_seconds: float = 60.0
    #: Identify requests that arrive through a ``trusted_proxies`` peer, by the
    #: client address it forwards. Off: every process able to connect from a
    #: listed proxy's address could then claim any device.
    trust_forwarded: bool = False
    #: Ask tailscaled's LocalAPI in-process when found (else always the CLI).
    localapi: bool = True


@dataclass(frozen=True)
class ServerConfig:
    bind: str = "127.0.0.1"
    port: int = 8740
    #: Proxy addresses or CIDRs whose ``X-Forwarded-*`` headers count.
    trusted_proxies: tuple[str, ...] = ()
    public_origins: tuple[str, ...] = ()
    log_level: str = "info"
    audit: AuditConfig = field(default_factory=AuditConfig)
    limits: Limits = field(default_factory=Limits)
    stream: StreamConfig = field(default_factory=StreamConfig)
    tailnet: TailnetConfig | None = None

    def with_limits(self, **changes: Any) -> ServerConfig:
        """A copy with some limits changed (tests and callers that tune one knob)."""
        return replace(self, limits=replace(self.limits, **changes))


_REMOTE_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,99}")
_BRANCH_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._/-]{0,199}")


def _valid_branch(branch: str) -> bool:
    """``git check-ref-format refs/heads/<branch>``'s rules, over a character set
    that already excludes space, controls, ``~^:?*[\\`` and ``@``: no empty
    component (so no leading, trailing, or doubled ``/``), no component that
    starts with ``.`` or ends with ``.lock``, no ``..``, no trailing ``.``, and
    (stricter than git) a first character that is a letter or digit."""
    if not _BRANCH_RE.fullmatch(branch) or ".." in branch or branch.endswith("."):
        return False
    return all(
        part and not part.startswith(".") and not part.endswith(".lock")
        for part in branch.split("/")
    )


def check_push(push: Any) -> dict:
    """Validate an audit push target ``{"remote": NAME, "branch": B}`` (SPEC §8.1).

    The one validator for ``server.json``'s ``audit.push``, ``lattice server
    project audit``, and a project's ``hosted/audit.json``. A remote name is a
    plain git remote name and a branch a plain ref name, so neither can be read
    as a git option, a URL, or a refspec. Raises ``ValueError``.
    """
    if not isinstance(push, dict) or set(push) != {"remote", "branch"}:
        raise ValueError('must be null or {"remote": NAME, "branch": BRANCH}')
    remote, branch = push["remote"], push["branch"]
    if not isinstance(remote, str) or not _REMOTE_RE.fullmatch(remote):
        raise ValueError(
            f"invalid git remote name {remote!r} (letters, digits, '.', '_', '-'; "
            "starting with a letter or digit)"
        )
    if not isinstance(branch, str) or not _valid_branch(branch):
        raise ValueError(f"invalid branch name {branch!r}")
    return {"remote": remote, "branch": branch}


def check_trusted_proxies(value: Any) -> tuple[str, ...]:
    """``trusted_proxies``: IPv4 or IPv6 addresses or CIDR ranges (SPEC §8.1).

    Every entry must parse, so a typo is refused rather than silently trusting
    nothing (uvicorn would keep an unparseable entry as a literal host name).
    ``*`` and a ``/0`` range are refused because the list names proxies, never
    "everyone". A range with host bits set is refused too: uvicorn would read
    it as a literal and match nothing.
    """
    if not isinstance(value, list):
        raise ServerConfigError("trusted_proxies must be a list of addresses or CIDR ranges")
    entries: list[str] = []
    for entry in value:
        if not isinstance(entry, str):
            raise ServerConfigError(
                f"trusted_proxies entries must be strings (an address or CIDR), got {entry!r}"
            )
        entry = entry.strip()
        everyone = ServerConfigError(
            f"trusted_proxies entry {entry!r} matches every address; the list names the "
            "proxies themselves (an address, or the narrowest range that holds them)"
        )
        if entry == "*":
            raise everyone
        try:
            if "/" in entry:
                if ipaddress.ip_network(entry).prefixlen == 0:
                    raise everyone
            else:
                ipaddress.ip_address(entry)
        except ValueError as exc:
            raise ServerConfigError(
                f"trusted_proxies entry {entry!r} is not an IPv4 or IPv6 address or CIDR "
                f"range: {exc}"
            ) from exc
        entries.append(entry)
    return tuple(entries)


_TOKEN_ID_RE = re.compile(r"^tok_[0-9A-Za-z]{1,64}$")
_TAILNET_KEYS = {"hosts", "nodes", "tailscale", "cache_seconds", "trust_forwarded", "localapi"}
_GRANT_KEYS = {"node", "login", "token"}


def _nonempty_str(where: str, value: Any) -> str:
    if not isinstance(value, str) or not value.strip() or value != value.strip():
        raise ServerConfigError(f"{where} must be a non-empty string without surrounding spaces")
    return value


def check_tailnet(raw: Any) -> TailnetConfig | None:
    """``tailnet`` (SPEC §10): ``null``, or the hosts and grants for dashboard
    sign-in by Tailscale identity. Both lists must be non-empty: an empty host
    list would turn the feature on nowhere, and an empty grant list would
    authenticate nobody, so either is a mistake worth refusing."""
    if raw is None:
        return None
    if not isinstance(raw, dict):
        raise ServerConfigError("tailnet must be null or an object")
    for key in raw:
        if key not in _TAILNET_KEYS:
            raise ServerConfigError(f"unknown key tailnet.{key} in {SERVER_JSON}")
    hosts = raw.get("hosts")
    if not isinstance(hosts, list) or not hosts:
        raise ServerConfigError(
            'tailnet.hosts must list the Host header values it applies on, e.g. ["board:8740"]'
        )
    host_values = tuple(_nonempty_str("tailnet.hosts entries", h).lower() for h in hosts)
    nodes = raw.get("nodes")
    if not isinstance(nodes, list) or not nodes:
        raise ServerConfigError(
            'tailnet.nodes must list grants {"node": ..., "login": ..., "token": "tok_..."}'
        )
    grants = []
    for entry in nodes:
        if not isinstance(entry, dict) or set(entry) != _GRANT_KEYS:
            raise ServerConfigError(
                'each tailnet.nodes entry must be {"node": ..., "login": ..., "token": "tok_..."}'
            )
        node = _nonempty_str("tailnet.nodes[].node", entry["node"]).rstrip(".")
        if not node:
            raise ServerConfigError("tailnet.nodes[].node must name a node")
        login = _nonempty_str("tailnet.nodes[].login", entry["login"])
        token = _nonempty_str("tailnet.nodes[].token", entry["token"])
        if not _TOKEN_ID_RE.fullmatch(token):
            raise ServerConfigError(
                f"tailnet.nodes[].token must be a token id (tok_...), got {token!r}; "
                "lattice server token list shows them"
            )
        grants.append(TailnetGrant(node=node, login=login, token=token))
    kwargs: dict[str, Any] = {}
    if "tailscale" in raw:
        kwargs["tailscale"] = _nonempty_str("tailnet.tailscale", raw["tailscale"])
    if "cache_seconds" in raw:
        value = raw["cache_seconds"]
        if (
            not isinstance(value, (int, float))
            or isinstance(value, bool)
            or not math.isfinite(value)
            or value < 0
        ):
            raise ServerConfigError(f"tailnet.cache_seconds must be a number >= 0, got {value!r}")
        kwargs["cache_seconds"] = float(value)
    for flag in ("trust_forwarded", "localapi"):
        if flag in raw:
            if not isinstance(raw[flag], bool):
                raise ServerConfigError(f"tailnet.{flag} must be true or false")
            kwargs[flag] = raw[flag]
    return TailnetConfig(hosts=host_values, grants=tuple(grants), **kwargs)


def _check_int(section: str, key: str, value: Any, *, minimum: int = 0) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < minimum:
        raise ServerConfigError(f"{section}{key} must be an integer >= {minimum}, got {value!r}")
    return value


def _section(cls: type, section: str, raw: Any) -> Any:
    if raw is None:
        return cls()
    if not isinstance(raw, dict):
        raise ServerConfigError(f"{section} must be an object")
    known = {f.name: f for f in fields(cls)}
    kwargs: dict[str, Any] = {}
    for key, value in raw.items():
        if key not in known:
            raise ServerConfigError(f"unknown key {section}.{key} in {SERVER_JSON}")
        default = known[key].default
        if isinstance(default, bool):
            if not isinstance(value, bool):
                raise ServerConfigError(f"{section}.{key} must be true or false")
            kwargs[key] = value
        elif isinstance(default, int):
            kwargs[key] = _check_int(f"{section}.", key, value, minimum=0)
        elif isinstance(default, float):
            if (
                not isinstance(value, (int, float))
                or isinstance(value, bool)
                or not math.isfinite(value)
                or value < 0
            ):
                raise ServerConfigError(f"{section}.{key} must be a number >= 0, got {value!r}")
            kwargs[key] = float(value)
        else:
            kwargs[key] = value
    return cls(**kwargs)


def parse_config(raw: Any) -> ServerConfig:
    """Validate a ``server.json`` object and fill in defaults."""
    if raw is None:
        raw = {}
    if not isinstance(raw, dict):
        raise ServerConfigError(f"{SERVER_JSON} must hold a JSON object")
    if "trusted_proxy" in raw:
        raise ServerConfigError(
            f"trusted_proxy is no longer accepted in {SERVER_JSON}; list the proxy addresses "
            'or CIDRs in trusted_proxies instead (for example "trusted_proxies": ["127.0.0.1"])'
        )
    known = {f.name for f in fields(ServerConfig)}
    for key in raw:
        if key not in known:
            raise ServerConfigError(f"unknown key {key!r} in {SERVER_JSON}")
    kwargs: dict[str, Any] = {}
    if "bind" in raw:
        if not isinstance(raw["bind"], str) or not raw["bind"]:
            raise ServerConfigError("bind must be a host name or address")
        kwargs["bind"] = raw["bind"]
    if "port" in raw:
        kwargs["port"] = _check_int("", "port", raw["port"])
        if kwargs["port"] > 65535:
            raise ServerConfigError("port must be at most 65535")
    if "trusted_proxies" in raw:
        kwargs["trusted_proxies"] = check_trusted_proxies(raw["trusted_proxies"])
    if "public_origins" in raw:
        origins = raw["public_origins"]
        if not isinstance(origins, list) or not all(isinstance(o, str) for o in origins):
            raise ServerConfigError("public_origins must be a list of origins")
        kwargs["public_origins"] = tuple(origins)
    if "log_level" in raw:
        if raw["log_level"] not in LOG_LEVELS:
            raise ServerConfigError(f"log_level must be one of {', '.join(LOG_LEVELS)}")
        kwargs["log_level"] = raw["log_level"]
    audit = _section(AuditConfig, "audit", raw.get("audit"))
    if audit.push is not None:
        try:
            check_push(audit.push)
        except ValueError as exc:
            raise ServerConfigError(f"audit.push: {exc}") from exc
    kwargs["audit"] = audit
    limits = _section(Limits, "limits", raw.get("limits"))
    if limits.lock_timeout_seconds > MAX_LOCK_TIMEOUT_SECONDS:
        raise ServerConfigError(
            f"limits.lock_timeout_seconds may not exceed {MAX_LOCK_TIMEOUT_SECONDS}"
        )
    for name in ("max_inflight_per_token", "token_ops_per_minute", "lock_timeout_seconds"):
        if getattr(limits, name) < 1:
            raise ServerConfigError(f"limits.{name} must be at least 1")
    if limits.token_body_bytes_per_minute < max(1, limits.max_body_bytes):
        raise ServerConfigError(
            "limits.token_body_bytes_per_minute must be at least limits.max_body_bytes, "
            "or a body the server accepts could never fit a token's budget"
        )
    kwargs["limits"] = limits
    stream = _section(StreamConfig, "stream", raw.get("stream"))
    if stream.heartbeat_seconds < 1:
        raise ServerConfigError("stream.heartbeat_seconds must be at least 1")
    kwargs["stream"] = stream
    if "tailnet" in raw:
        kwargs["tailnet"] = check_tailnet(raw["tailnet"])
        if (
            kwargs["tailnet"]
            and kwargs["tailnet"].trust_forwarded
            and not kwargs.get("trusted_proxies")
        ):
            raise ServerConfigError(
                "tailnet.trust_forwarded needs trusted_proxies: no proxy is trusted to forward"
            )
    return ServerConfig(**kwargs)


def load_config(root: Path) -> ServerConfig:
    """Read ``<root>/server.json`` (defaults when absent)."""
    path = Path(root) / SERVER_JSON
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return ServerConfig()
    except OSError as exc:
        raise ServerConfigError(f"cannot read {path}: {exc}") from exc
    try:
        raw = json.loads(text)
    except ValueError as exc:
        raise ServerConfigError(f"{path} is not valid JSON: {exc}") from exc
    return parse_config(raw)
