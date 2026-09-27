"""Operation requests from a hosted checkout (SPEC §8.4, §8.6, §9.5, §15).

- :func:`wire_params` checks an operation's params locally (the same
  ``VALIDATION_ERROR``\\ s as local mode) and omits every parameter equal to its
  declared default, so a newer client keeps working against an older server
  until a new option is actually used.
- :func:`post_operation` sends one operation call, retrying with the **same**
  ``op_id`` for up to the remote's ``retry_seconds`` on a connection error, a
  read timeout, HTTP 429, 502, or 504, and HTTP 503 unless the envelope says
  ``BOARD_UNAVAILABLE``. It waits ``Retry-After`` when given, else backs off
  from 0.5 s doubling to 5 s, and says so on stderr (never silently). When it
  gives up, ``SERVER_UNREACHABLE`` means no attempt ever reached the server
  (nothing was written); ``OUTCOME_UNKNOWN`` means one may have been applied.
  A write started inside the offline window gives up at once when its first
  attempt cannot connect, so a stopped server costs one wait per outage.
"""

from __future__ import annotations

import dataclasses
import sys
import time
import urllib.parse
from collections.abc import Callable
from typing import Any

from lattice.core.errors import OpError
from lattice.remote import http

#: Operation requests: the answer may wait behind the project's admission lock
#: (at most ``lock_timeout_seconds``) plus the work, so the read timeout sits
#: above both and a slow admission is never mistaken for a lost request.
OP_POLICY = http.Policy(connect_seconds=5.0, response_seconds=90.0)
FIRST_BACKOFF_SECONDS = 0.5
MAX_BACKOFF_SECONDS = 5.0
#: A retrying write reports progress this often (SPEC §8.6).
PROGRESS_SECONDS = 5.0

#: Test seams: the clock the retry budget runs on, and the sleep between attempts.
_now: Callable[[], float] = time.monotonic
_sleep: Callable[[float], None] = time.sleep


def _jsonable(value: Any) -> Any:
    if isinstance(value, tuple | list):
        return [_jsonable(v) for v in value]
    if isinstance(value, dict):
        return {k: _jsonable(v) for k, v in value.items()}
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        return {k: _jsonable(v) for k, v in dataclasses.asdict(value).items()}
    return value


def wire_params(op_name: str, params: Any) -> dict[str, Any]:
    """*params* (a mapping or the operation's ``Params``) as the request's
    ``params``: checked locally, every default omitted (SPEC §8.4, §15)."""
    from lattice.ops import get_operation, parse_params

    params_cls = get_operation(op_name).Params
    parsed = parse_params(params_cls, params, op_name=op_name)
    wire: dict[str, Any] = {}
    for f in dataclasses.fields(parsed):
        if not f.init:
            continue
        value = getattr(parsed, f.name)
        if f.default is not dataclasses.MISSING:
            if value == f.default:
                continue
        elif f.default_factory is not dataclasses.MISSING and value == f.default_factory():
            continue
        wire[f.name] = _jsonable(value)
    return wire


def result_from_json(data: dict) -> Any:
    """The ``OpResult`` a server returned (``result`` of an op response)."""
    from lattice.ops import OpResult

    known = {f.name for f in dataclasses.fields(OpResult)} - {"paths"}
    fields = {k: v for k, v in data.items() if k in known}
    fields["events"] = list(fields.get("events") or [])
    return OpResult(**fields)


def _retryable(exc: http.ServerError) -> bool:
    if exc.status in (429, 502, 504):
        return True
    return exc.status == 503 and exc.code != "BOARD_UNAVAILABLE"


def outcome_unknown(remote: http.Remote, op_id: str, detail: str) -> OpError:
    return OpError(
        "OUTCOME_UNKNOWN",
        f"the server may have applied this write (operation {op_id}); check with "
        f"`lattice remote op-status {op_id}` before retrying. ({remote.alias}: {detail})",
        {"op_id": op_id, "remote": remote.alias},
    )


def server_unreachable(remote: http.Remote, detail: str) -> OpError:
    return OpError(
        "SERVER_UNREACHABLE",
        f"cannot reach {remote.alias} ({remote.url}): {detail}. Nothing was written; "
        "run the command again when the server is reachable.",
        {"remote": remote.alias},
    )


def write_unreachable(remote: http.Remote, os_error: str, waited: float) -> OpError:
    """A write that never reached the server (SPEC §8.6): plain words, with the
    raw OS error only in ``details``."""
    return OpError(
        "SERVER_UNREACHABLE",
        f"server {remote.alias} ({remote.url}) is not available. Nothing was written; "
        "run the command again when it is back.",
        {
            "remote": remote.alias,
            "url": remote.url,
            "os_error": os_error,
            "waited_seconds": round(max(0.0, waited), 1),
        },
    )


def _progress(line: str) -> None:
    print(f"lattice: {line}", file=sys.stderr)


def post_operation(
    remote: http.Remote,
    project: str,
    op_name: str,
    body: dict,
    *,
    offline: bool = False,
) -> dict:
    """``POST /v1/projects/<project>/ops/<op_name>`` with retries; returns the
    response's ``data`` (``{result, seq, op_id}``). See the module docstring.

    *offline*: the offline window was already open when the command started, so
    a first attempt that cannot connect gives up at once (no repeated wait).
    While it retries it writes progress lines to stderr (SPEC §8.6): at once,
    then every 5 seconds; they name no operation ID and no raw OS error.
    """
    op_id = body["op_id"]
    path = (
        f"/v1/projects/{urllib.parse.quote(project, safe='')}/ops/"
        f"{urllib.parse.quote(op_name, safe='.')}"
    )
    started = _now()
    deadline = started + remote.retry_seconds
    backoff = FIRST_BACKOFF_SECONDS
    reached = False
    next_progress: float | None = None
    first = True
    while True:
        wait: float | None = None
        try:
            response = http.request(
                remote,
                "POST",
                path,
                json_body=body,
                policy=OP_POLICY,
                what=f"operation {op_name}",
            )
            return response.data()
        except http.Unreachable as exc:
            reached = reached or exc.sent
            detail = exc.reason
            state = "not available"
            if first and offline and not exc.sent:
                raise write_unreachable(remote, detail, _now() - started) from None
        except http.ServerError as exc:
            if not _retryable(exc):
                raise OpError(exc.code, exc.message, exc.details) from None
            reached = True
            detail = f"HTTP {exc.status} {exc.code}"
            state = "busy"
            wait = exc.retry_after
        first = False
        now = _now()
        if wait is None:
            # The last backoff ends at the deadline, so the retries fill the
            # whole budget; a server's Retry-After past it is honored by giving up.
            wait = min(backoff, max(0.0, deadline - now))
            backoff = min(backoff * 2, MAX_BACKOFF_SECONDS)
        if now >= deadline or now + wait > deadline:
            if reached:
                raise outcome_unknown(remote, op_id, detail)
            raise write_unreachable(remote, detail, now - started)
        if next_progress is None:
            # No silent wait: say so at once, in plain words.
            _progress(
                f"server {remote.alias} ({remote.url}) is {state}; retrying for up to "
                f"{remote.retry_seconds:g} s"
            )
            next_progress = started + PROGRESS_SECONDS
        # Sleep in slices so a progress line lands every PROGRESS_SECONDS.
        until = now + wait
        while True:
            now = _now()
            if now >= next_progress:
                if now < deadline:  # at the deadline the error line follows at once
                    _progress(
                        f"{remote.alias} still {state} ({now - started:.0f} s of "
                        f"{remote.retry_seconds:g} s)"
                    )
                while next_progress <= now:
                    next_progress += PROGRESS_SECONDS
            if now >= until:
                break
            _sleep(min(until, next_progress) - now)


def op_status(remote: http.Remote, project: str, op_id: str) -> dict:
    """``GET /v1/projects/<project>/ops/<op_id>`` (SPEC §8.6)."""
    path = (
        f"/v1/projects/{urllib.parse.quote(project, safe='')}/ops/"
        f"{urllib.parse.quote(op_id, safe='')}"
    )
    try:
        return http.request(remote, "GET", path, policy=http.BULK).data()
    except http.Unreachable as exc:
        raise server_unreachable(remote, exc.reason) from None
    except http.ServerError as exc:
        raise OpError(exc.code, exc.message, exc.details) from None


def get_json(remote: http.Remote, path: str, *, policy: http.Policy = http.BULK) -> Any:
    """One GET of a JSON endpoint; ``SERVER_UNREACHABLE`` when it cannot be reached."""
    try:
        return http.request(remote, "GET", path, policy=policy).data()
    except http.Unreachable as exc:
        raise server_unreachable(remote, exc.reason) from None
    except http.ServerError as exc:
        raise OpError(exc.code, exc.message, exc.details) from None
