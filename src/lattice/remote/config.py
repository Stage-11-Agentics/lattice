"""Per-user remotes, read side (SPEC §9.1): turn a binding's alias into a :class:`Remote`.

Sources, environment first:

- ``LATTICE_REMOTE_<ALIAS>_URL``, ``LATTICE_REMOTE_<ALIAS>_TOKEN``, and
  ``LATTICE_REMOTE_<ALIAS>_HEADERS`` (a JSON object mapping header name to the
  name of the environment variable holding its value), where ``<ALIAS>`` is
  uppercased with non-alphanumerics replaced by ``_``;
- ``$XDG_CONFIG_HOME/lattice/remotes.json`` (default ``~/.config/...``). It can
  hold tokens, so it is read only when private: a file other users can access
  is refused before anything is read from it.

A token is a literal string or ``{"env": "VAR"}``; header values are always
``{"env": "VAR"}``. An alias configured nowhere is ``REMOTE_NOT_CONFIGURED``;
an ``{"env": "VAR"}`` whose variable is unset or empty is ``TOKEN_ENV_UNSET``.
"""

from __future__ import annotations

import json
import os
import re
import stat
from pathlib import Path
from typing import Any

from lattice.core.errors import OpError
from lattice.remote.http import Remote


def remotes_path() -> Path:
    base = os.environ.get("XDG_CONFIG_HOME") or str(Path.home() / ".config")
    return Path(base) / "lattice" / "remotes.json"


def env_prefix(alias: str) -> str:
    return "LATTICE_REMOTE_" + re.sub(r"[^A-Za-z0-9]", "_", alias).upper() + "_"


def _not_configured(alias: str) -> OpError:
    return OpError(
        "REMOTE_NOT_CONFIGURED",
        f"no remote named '{alias}' is configured on this machine. Run: "
        f"lattice remote add {alias} <url> --token-env <VAR> "
        "(ask your server admin for the URL and a token).",
        {"remote": alias},
    )


def _env_value(name: str, what: str, alias: str) -> str:
    value = os.environ.get(name)
    if not value:
        raise OpError(
            "TOKEN_ENV_UNSET",
            f"the {what} for remote '{alias}' comes from the environment variable "
            f"{name}, which is unset or empty.",
            {"remote": alias, "variable": name},
        )
    return value


def _read_private(path: Path) -> str | None:
    """The file's text, or ``None`` if it does not exist.

    It is opened once; the permissions are checked on that descriptor before
    anything is read from it, so no swap between check and read can slip a
    group- or world-readable file past the check.
    """
    try:
        fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise OpError("VALIDATION_ERROR", f"cannot read {path}: {exc}") from None
    with os.fdopen(fd, "rb") as fh:
        info = os.fstat(fh.fileno())
        if not stat.S_ISREG(info.st_mode):
            raise OpError("VALIDATION_ERROR", f"{path} is not a regular file")
        if info.st_mode & (stat.S_IRWXG | stat.S_IRWXO):
            raise OpError(
                "VALIDATION_ERROR",
                f"{path} is accessible to other users (mode "
                f"{stat.S_IMODE(info.st_mode):04o}); it can hold tokens, so Lattice reads it "
                f"only when it is private. Run: chmod 600 {path}",
                {"path": str(path)},
            )
        try:
            return fh.read().decode("utf-8")
        except (OSError, UnicodeDecodeError) as exc:
            raise OpError("VALIDATION_ERROR", f"cannot read {path}: {exc}") from None


def _file_entry(alias: str) -> dict[str, Any] | None:
    """The file's entry for *alias* (the file must be private, 0600)."""
    path = remotes_path()
    raw = _read_private(path)
    if raw is None:
        return None
    try:
        data = json.loads(raw)
    except ValueError as exc:
        raise OpError("VALIDATION_ERROR", f"{path} is not valid JSON: {exc}") from None
    remotes = data.get("remotes") if isinstance(data, dict) else None
    entry = remotes.get(alias) if isinstance(remotes, dict) else None
    return entry if isinstance(entry, dict) else None


def _token(value: Any, alias: str) -> str | None:
    if value is None:
        return None
    if isinstance(value, dict) and isinstance(value.get("env"), str):
        return _env_value(value["env"], "token", alias)
    if isinstance(value, str):
        return value
    raise OpError(
        "VALIDATION_ERROR", f"remote '{alias}': token must be a string or {{\"env\": ...}}"
    )


def _headers(value: Any, alias: str) -> dict[str, str]:
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise OpError("VALIDATION_ERROR", f"remote '{alias}': headers must be an object")
    headers: dict[str, str] = {}
    for name, spec in value.items():
        var = spec.get("env") if isinstance(spec, dict) else spec
        if not isinstance(name, str) or not isinstance(var, str) or not var:
            raise OpError(
                "VALIDATION_ERROR",
                f"remote '{alias}': header {name!r} must name an environment variable",
            )
        headers[name] = _env_value(var, f"header {name}", alias)
    return headers


def resolve_remote(alias: str) -> Remote:
    """The :class:`Remote` for *alias*, environment overriding the file."""
    prefix = env_prefix(alias)
    entry = dict(_file_entry(alias) or {})
    env_url = os.environ.get(prefix + "URL")
    if env_url:
        entry["url"] = env_url
    env_token = os.environ.get(prefix + "TOKEN")
    if env_token:
        entry["token"] = env_token
    env_headers = os.environ.get(prefix + "HEADERS")
    if env_headers:
        try:
            parsed = json.loads(env_headers)
        except ValueError:
            parsed = None
        if not isinstance(parsed, dict):
            raise OpError(
                "VALIDATION_ERROR",
                f"{prefix}HEADERS must be a JSON object of header name to variable name",
            )
        entry["headers"] = parsed
    url = entry.get("url")
    if not isinstance(url, str) or not url:
        raise _not_configured(alias)
    return Remote(
        alias=alias,
        url=url,
        token=_token(entry.get("token"), alias),
        headers=_headers(entry.get("headers"), alias),
    )
