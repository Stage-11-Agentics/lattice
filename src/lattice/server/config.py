"""Server root and ``server.json`` (SPEC §8.1).

Every key of ``server.json`` is optional; a missing key takes the default
shown in SPEC §8.1. An unknown key or a value of the wrong type is refused
when the server starts, so a typo never silently falls back to a default.
"""

from __future__ import annotations

import json
import os
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


@dataclass(frozen=True)
class AuditConfig:
    enabled: bool = True
    debounce_seconds: int = 5
    max_interval_seconds: int = 60
    push: dict | None = None


@dataclass(frozen=True)
class StreamConfig:
    heartbeat_seconds: int = 2


@dataclass(frozen=True)
class ServerConfig:
    bind: str = "127.0.0.1"
    port: int = 8740
    trusted_proxy: bool = False
    public_origins: tuple[str, ...] = ()
    log_level: str = "info"
    audit: AuditConfig = field(default_factory=AuditConfig)
    limits: Limits = field(default_factory=Limits)
    stream: StreamConfig = field(default_factory=StreamConfig)

    def with_limits(self, **changes: Any) -> ServerConfig:
        """A copy with some limits changed (tests and callers that tune one knob)."""
        return replace(self, limits=replace(self.limits, **changes))


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
        else:
            kwargs[key] = value
    return cls(**kwargs)


def parse_config(raw: Any) -> ServerConfig:
    """Validate a ``server.json`` object and fill in defaults."""
    if raw is None:
        raw = {}
    if not isinstance(raw, dict):
        raise ServerConfigError(f"{SERVER_JSON} must hold a JSON object")
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
    if "trusted_proxy" in raw:
        if not isinstance(raw["trusted_proxy"], bool):
            raise ServerConfigError("trusted_proxy must be true or false")
        kwargs["trusted_proxy"] = raw["trusted_proxy"]
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
        push = audit.push
        if (
            not isinstance(push, dict)
            or set(push) != {"remote", "branch"}
            or not all(isinstance(v, str) and v for v in push.values())
        ):
            raise ServerConfigError('audit.push must be null or {"remote": ..., "branch": ...}')
    kwargs["audit"] = audit
    limits = _section(Limits, "limits", raw.get("limits"))
    if limits.lock_timeout_seconds > MAX_LOCK_TIMEOUT_SECONDS:
        raise ServerConfigError(
            f"limits.lock_timeout_seconds may not exceed {MAX_LOCK_TIMEOUT_SECONDS}"
        )
    for name in ("max_inflight_per_token", "token_ops_per_minute", "lock_timeout_seconds"):
        if getattr(limits, name) < 1:
            raise ServerConfigError(f"limits.{name} must be at least 1")
    kwargs["limits"] = limits
    stream = _section(StreamConfig, "stream", raw.get("stream"))
    if stream.heartbeat_seconds < 1:
        raise ServerConfigError("stream.heartbeat_seconds must be at least 1")
    kwargs["stream"] = stream
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
