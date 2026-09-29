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

import ipaddress
import json
import math
import os
import re
import stat
import urllib.parse
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from lattice.program import program_name
from lattice.core.errors import OpError
from lattice.remote.http import Remote

DEFAULT_RETRY_SECONDS = 15.0
#: The longest one operation may retry (SPEC §8.6: retries are bounded).
MAX_RETRY_SECONDS = 3600.0


def remotes_path() -> Path:
    base = os.environ.get("XDG_CONFIG_HOME") or str(Path.home() / ".config")
    return Path(base) / "lattice" / "remotes.json"


def env_prefix(alias: str) -> str:
    return "LATTICE_REMOTE_" + re.sub(r"[^A-Za-z0-9]", "_", alias).upper() + "_"


def _not_configured(alias: str) -> OpError:
    return OpError(
        "REMOTE_NOT_CONFIGURED",
        f"no remote named '{alias}' is configured on this machine. Run: "
        f"{program_name()} remote add {alias} <url> --token-env <VAR> "
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
    entry = _read_file()["remotes"].get(alias)
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


def _flag(value: Any, alias: str, key: str, default: bool) -> bool:
    if value is None:
        return default
    if not isinstance(value, bool):
        raise OpError("VALIDATION_ERROR", f"remote '{alias}': {key} must be true or false")
    return value


def _retry_seconds(value: Any, alias: str) -> float:
    if value is None:
        return DEFAULT_RETRY_SECONDS
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(value)
        or not 0 <= value <= MAX_RETRY_SECONDS
    ):
        raise OpError(
            "VALIDATION_ERROR",
            f"remote '{alias}': retry_seconds must be a number from 0 to "
            f"{MAX_RETRY_SECONDS:g} (seconds)",
        )
    return float(value)


def is_loopback_host(host: str | None) -> bool:
    """``localhost``, ``127.0.0.0/8``, or ``::1`` (SPEC §9.1)."""
    if not host:
        return False
    if host.lower() == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def check_url(alias: str, url: str, *, allow_plaintext: bool) -> None:
    """Refuse a URL the transport must not use: not http(s), or ``http://`` to a
    host that is not loopback without ``allow_plaintext`` (``INSECURE_URL``)."""
    parts = urllib.parse.urlsplit(url)
    scheme = parts.scheme.lower()
    if scheme not in ("http", "https") or not parts.hostname:
        raise OpError(
            "VALIDATION_ERROR",
            f"remote '{alias}': {url!r} is not an http:// or https:// URL",
            {"remote": alias},
        )
    if scheme == "http" and not allow_plaintext and not is_loopback_host(parts.hostname):
        raise OpError(
            "INSECURE_URL",
            f"remote '{alias}' uses plaintext http:// to {parts.hostname}, which is not "
            "loopback; the token would cross the network unencrypted. Use https://, or, "
            "for a server reached over an encrypted private network, allow it with "
            f"'{program_name()} remote add {alias} {url} --allow-plaintext' (or "
            f"{env_prefix(alias)}ALLOW_PLAINTEXT=1).",
            {"remote": alias, "host": parts.hostname},
        )


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
        entry["headers"] = _env_headers(prefix, env_headers)
    env_plaintext = os.environ.get(prefix + "ALLOW_PLAINTEXT")
    if env_plaintext:
        entry["allow_plaintext"] = env_plaintext == "1"
    url = entry.get("url")
    if not isinstance(url, str) or not url:
        raise _not_configured(alias)
    allow_plaintext = _flag(entry.get("allow_plaintext"), alias, "allow_plaintext", False)
    check_url(alias, url, allow_plaintext=allow_plaintext)
    return Remote(
        alias=alias,
        url=url,
        token=_token(entry.get("token"), alias),
        headers=_headers(entry.get("headers"), alias),
        run_board_hooks=_flag(entry.get("run_board_hooks"), alias, "run_board_hooks", False),
        run_auto_reviews=_flag(entry.get("run_auto_reviews"), alias, "run_auto_reviews", True),
        allow_plaintext=allow_plaintext,
        retry_seconds=_retry_seconds(entry.get("retry_seconds"), alias),
    )


def _env_headers(prefix: str, raw: str) -> dict:
    try:
        parsed = json.loads(raw)
    except ValueError:
        parsed = None
    if not isinstance(parsed, dict):
        raise OpError(
            "VALIDATION_ERROR",
            f"{prefix}HEADERS must be a JSON object of header name to variable name",
        )
    return parsed


# ---------------------------------------------------------------------------
# Write side: lattice remote add / list
# ---------------------------------------------------------------------------


def _read_file() -> dict:
    """``remotes.json`` parsed, ``{"remotes": {}}`` when absent. The one reader of
    the file: every caller (resolution, ``add``, ``list``, secret discovery) goes
    through :func:`_read_private`'s descriptor check (a regular file, not a
    symlink, owner-only), because the file can hold tokens (SPEC §9.1)."""
    path = remotes_path()
    raw = _read_private(path)
    if raw is None:
        return {"remotes": {}}
    try:
        data = json.loads(raw)
    except ValueError as exc:
        raise OpError("VALIDATION_ERROR", f"{path} is not valid JSON: {exc}") from None
    if not isinstance(data, dict) or not isinstance(data.get("remotes", {}), dict):
        raise OpError("VALIDATION_ERROR", f'{path}: expected {{"remotes": {{...}}}}')
    data.setdefault("remotes", {})
    return data


def add_remote(
    alias: str,
    url: str,
    *,
    token: str | dict | None,
    headers: dict[str, str],
    allow_plaintext: bool,
) -> Path:
    """Write (or replace) *alias* in ``remotes.json``, mode 0600; returns the path.

    *token* is a literal or ``{"env": VAR}``; *headers* map header names to the
    environment variables holding their values. Settings the entry already had
    (``run_board_hooks``, ``run_auto_reviews``, ``retry_seconds``) are kept.
    """
    if not alias or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*", alias):
        raise OpError(
            "VALIDATION_ERROR",
            f"remote alias {alias!r} must start with a letter or digit and hold only "
            "letters, digits, '.', '_', and '-'.",
        )
    check_url(alias, url, allow_plaintext=allow_plaintext)
    data = _read_file()
    previous = data["remotes"].get(alias)
    entry: dict[str, Any] = dict(previous) if isinstance(previous, dict) else {}
    entry["url"] = url.rstrip("/")
    if token is None:
        entry.pop("token", None)
    else:
        entry["token"] = token
    if headers:
        entry["headers"] = {name: {"env": var} for name, var in headers.items()}
    else:
        entry.pop("headers", None)
    if allow_plaintext:
        entry["allow_plaintext"] = True
    else:
        entry.pop("allow_plaintext", None)
    data["remotes"][alias] = entry
    path = remotes_path()
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    text = json.dumps(data, sort_keys=True, indent=2) + "\n"
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(text)
            fh.flush()
            os.fsync(fh.fileno())
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise
    return path


def list_remotes() -> list[dict[str, Any]]:
    """Every alias in ``remotes.json`` with its URL and settings, never a token."""
    rows = []
    for alias, entry in sorted(_read_file()["remotes"].items()):
        if not isinstance(entry, dict):
            continue
        rows.append(
            {
                "alias": alias,
                "url": entry.get("url"),
                "headers": sorted(entry.get("headers") or {}),
                "run_board_hooks": bool(entry.get("run_board_hooks", False)),
                "run_auto_reviews": bool(entry.get("run_auto_reviews", True)),
                "allow_plaintext": bool(entry.get("allow_plaintext", False)),
                "retry_seconds": entry.get("retry_seconds", DEFAULT_RETRY_SECONDS),
            }
        )
    return rows


# ---------------------------------------------------------------------------
# Secrets in the environment (SPEC §3.4)
# ---------------------------------------------------------------------------


def secret_env_names(env: Mapping[str, str] | None = None) -> set[str]:
    """Environment variables that carry a remote's credentials.

    Every ``LATTICE_REMOTE_*`` variable, every variable a ``remotes.json``
    entry names for its token or a header, and every variable a
    ``LATTICE_REMOTE_<ALIAS>_HEADERS`` override names. Hook commands and review
    agents run without them.

    Fails closed: a ``remotes.json`` that is not a private regular file raises
    ``VALIDATION_ERROR`` (its token variables could not be named, so nothing is
    started with them); an absent file names nothing.
    """
    env = os.environ if env is None else env
    names = {name for name in env if name.startswith("LATTICE_REMOTE_")}
    for name in list(names):
        if name.endswith("_HEADERS"):
            try:
                parsed = json.loads(env[name])
            except (ValueError, TypeError):
                continue
            if isinstance(parsed, dict):
                names.update(v for v in parsed.values() if isinstance(v, str))
    for entry in _read_file()["remotes"].values():
        if not isinstance(entry, dict):
            continue
        token = entry.get("token")
        if isinstance(token, dict) and isinstance(token.get("env"), str):
            names.add(token["env"])
        headers = entry.get("headers")
        if isinstance(headers, dict):
            for spec in headers.values():
                var = spec.get("env") if isinstance(spec, dict) else spec
                if isinstance(var, str):
                    names.add(var)
    return names
