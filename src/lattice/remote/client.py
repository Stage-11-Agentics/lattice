"""Operation requests from a hosted checkout (SPEC §8.4, §8.6, §9.5, §15).

- :func:`wire_params` checks an operation's params locally (the same
  ``VALIDATION_ERROR``\\ s as local mode) and omits every parameter equal to its
  declared default, so a newer client keeps working against an older server
  until a new option is actually used.
- :func:`post_operation` sends one operation call, retrying with the **same**
  ``op_id`` for up to the remote's ``retry_seconds`` on a connection error, a
  read timeout, a gateway's 502, 503, or 504 (no ``Lattice-Protocol``; it
  counts as unreachable, SPEC §9.1), HTTP 429, 502, or 504, and HTTP 503
  unless the envelope says ``BOARD_UNAVAILABLE``. It waits ``Retry-After``
  when given (never less than the current backoff), else backs off from 0.5 s
  doubling to 5 s, and says so on stderr (never silently). No attempt starts
  after ``retry_seconds``: the last backoff ends just before it, and the
  deadline is checked again after every wait. When it gives up,
  ``SERVER_UNREACHABLE`` means no attempt ever reached the server (nothing was
  written); ``OUTCOME_UNKNOWN`` means one may have been applied (a gateway's
  502, 503, or 504 may have forwarded it).
  A write started inside the offline window gives up at once when its first
  attempt cannot connect, so a stopped server costs one wait per outage.
"""

from __future__ import annotations

import contextlib
import base64
import binascii
import dataclasses
import hashlib
import re
import sys
import threading
import time
import urllib.parse
from collections.abc import Callable, Iterator
from typing import Any

from lattice.program import program_name
from lattice.core.errors import OpError
from lattice.remote import http

#: Operation requests: the answer may wait behind the project's admission lock
#: (at most ``lock_timeout_seconds``) plus the work, so the read timeout sits
#: above both and a slow admission is never mistaken for a lost request.
OP_POLICY = http.Policy(connect_seconds=5.0, response_seconds=90.0)
MEDIA_UPLOAD_POLICY = http.Policy(
    connect_seconds=10.0, response_seconds=60.0, progress="uploading issue media"
)
FIRST_BACKOFF_SECONDS = 0.5
MAX_BACKOFF_SECONDS = 5.0
#: A retrying write reports progress this often (SPEC §8.6).
PROGRESS_SECONDS = 5.0
#: The last attempt starts at least this long before the retry deadline, so a
#: sleep that overshoots cannot push it past ``retry_seconds``.
LAST_ATTEMPT_MARGIN_SECONDS = 0.1

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


_MEDIA_SHA256_RE = re.compile(r"[0-9a-f]{64}\Z")


def stage_issue_media(remote: http.Remote, project: str, params: dict) -> dict:
    """Upload each LAT-366 payload as raw bytes and return hosted staged params."""
    import copy

    result = copy.deepcopy(params)
    objects: dict[str, bytes] = {}

    def stage(payload: dict) -> dict:
        if set(payload) != {"filename", "content_b64", "sha256"}:
            raise OpError(
                "VALIDATION_ERROR",
                "hosted media upload expects the local filename, content_b64, sha256 payload.",
            )
        claimed = payload.get("sha256")
        if not isinstance(claimed, str) or not _MEDIA_SHA256_RE.fullmatch(claimed):
            raise OpError(
                "VALIDATION_ERROR", "media sha256 must be 64 lowercase hexadecimal characters."
            )
        try:
            content = base64.b64decode(payload["content_b64"], validate=True)
        except (binascii.Error, ValueError, TypeError) as exc:
            raise OpError("VALIDATION_ERROR", "payload content_b64 is not valid base64.") from exc
        actual = hashlib.sha256(content).hexdigest()
        if actual != claimed:
            raise OpError("VALIDATION_ERROR", "payload sha256 does not match its content.")
        if actual not in objects:
            objects[actual] = content
            path = f"/v1/projects/{urllib.parse.quote(project, safe='')}/issues/media/staging/{actual}"
            try:
                response = http.request(
                    remote,
                    "PUT",
                    path,
                    raw_body=content,
                    content_type="application/octet-stream",
                    policy=MEDIA_UPLOAD_POLICY,
                    what="issue media upload",
                )
            except http.Unreachable as exc:
                raise server_unreachable(remote, exc.reason) from None
            except http.ServerError as exc:
                raise OpError(exc.code, exc.message, exc.details) from None
            metadata = response.data()
            if (
                not isinstance(metadata, dict)
                or metadata.get("sha256") != actual
                or metadata.get("size_bytes") != len(content)
                or metadata.get("staged") is not True
            ):
                raise OpError(
                    "INTEGRITY_ERROR", "server returned invalid issue-media staging metadata."
                )
        return {
            "filename": payload["filename"],
            "sha256": actual,
            "size": len(content),
            "staged": True,
        }

    items = result.get("media")
    if not isinstance(items, list):
        raise OpError("VALIDATION_ERROR", "issue media must be a list.")
    for item in items:
        if not isinstance(item, dict) or not isinstance(item.get("payload"), dict):
            raise OpError("VALIDATION_ERROR", "issue media item must contain a payload.")
        item["payload"] = stage(item["payload"])
        for frame in item.get("frames") or []:
            if not isinstance(frame, dict) or not isinstance(frame.get("payload"), dict):
                raise OpError("VALIDATION_ERROR", "issue media frame must contain a payload.")
            frame["payload"] = stage(frame["payload"])
    return result


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
        f"`{program_name()} remote op-status {op_id}` before retrying. ({remote.alias}: {detail})",
        {"op_id": op_id, "remote": remote.alias},
    )


def server_unreachable(remote: http.Remote, detail: str) -> OpError:
    """A read-only request that could not reach the server. It says nothing
    about writes: "Nothing was written" belongs only to a write that never
    connected (:func:`write_unreachable`)."""
    return OpError(
        "SERVER_UNREACHABLE",
        f"cannot reach {remote.alias} ({remote.url}): {detail}; "
        "run the command again when the server is reachable.",
        {"remote": remote.alias},
    )


def lookup_unreachable(remote: http.Remote, op_id: str, detail: str) -> OpError:
    """An op-status lookup that could not reach the server: the write's outcome
    is still unknown, so the only safe next step is the lookup again."""
    return OpError(
        "SERVER_UNREACHABLE",
        f"cannot reach {remote.alias} ({remote.url}) to look up operation {op_id}: "
        f"{detail}. Its outcome is still unknown; do not run the write again. Check "
        f"again when the server is reachable: {program_name()} remote op-status {op_id}",
        {"remote": remote.alias, "op_id": op_id},
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


class _Progress:
    """The progress lines of one write (SPEC §8.6 "No silent wait").

    While the write retries: the first line after the first failed attempt,
    then one line every ``PROGRESS_SECONDS``, each scheduled from the previous
    one. The retry loop ticks while it sleeps; a ticker thread ticks while any
    request is in flight (a connect, or a server that took the request and has
    not answered), for as long as it is, so the cadence holds past the retry
    window too, until the request's own read timeout. A request in flight
    before the first failure, or after the window, gets the waiting line.
    """

    def __init__(self, remote: http.Remote, started: float, deadline: float):
        self.remote = remote
        self.started = started
        self.deadline = deadline
        self.state = "not available"
        self.retrying = False
        self.next = started + PROGRESS_SECONDS
        self._lock = threading.Lock()

    def begin(self, now: float) -> None:
        with self._lock:
            if self.retrying:
                return
            self.retrying = True
            _progress(
                f"server {self.remote.alias} ({self.remote.url}) is {self.state}; retrying "
                f"for up to {self.remote.retry_seconds:g} s"
            )
            self.next = now + PROGRESS_SECONDS

    def tick(self, *, in_flight: bool = False) -> None:
        with self._lock:
            now = _now()
            if now < self.next:
                return
            self.next = now + PROGRESS_SECONDS
            if self.retrying and now < self.deadline:
                _progress(
                    f"{self.remote.alias} still {self.state} ({now - self.started:.0f} s of "
                    f"{self.remote.retry_seconds:g} s)"
                )
            elif in_flight:
                _progress(
                    f"still waiting for {self.remote.alias} to answer "
                    f"({now - self.started:.0f} s); if the request reached it, the write "
                    "may have applied"
                )
            # Otherwise the deadline has passed between attempts: the error
            # line follows at once.

    def sleep(self, seconds: float) -> None:
        """Sleep *seconds*, in slices so each progress line lands on time."""
        until = _now() + seconds
        while True:
            self.tick()
            now = _now()
            if now >= until:
                return
            _sleep(min(until, self.next) - now)

    @contextlib.contextmanager
    def in_flight(self) -> Iterator[None]:
        """Keep the lines coming while one request is in flight; the ticker
        stops (and is joined) before this returns, so nothing prints after."""
        stop = threading.Event()

        def run() -> None:
            while not stop.wait(max(0.01, self.next - _now())):
                self.tick(in_flight=True)

        ticker = threading.Thread(target=run, name="lattice-write-progress", daemon=True)
        ticker.start()
        try:
            yield
        finally:
            stop.set()
            ticker.join()


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
    It writes progress lines to stderr (SPEC §8.6): once retrying, at once and
    then every 5 seconds; and every 5 seconds while any request is in flight,
    past the retry window too. They name no operation ID and no raw OS error.
    """
    op_id = body["op_id"]
    path = (
        f"/v1/projects/{urllib.parse.quote(project, safe='')}/ops/"
        f"{urllib.parse.quote(op_name, safe='.')}"
    )
    started = _now()
    deadline = started + remote.retry_seconds
    progress = _Progress(remote, started, deadline)
    backoff = FIRST_BACKOFF_SECONDS
    reached = False
    first = True
    while True:
        wait: float | None = None
        try:
            with progress.in_flight():
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
            progress.state = "not available"
            wait = exc.retry_after  # a gateway may name one (SPEC §8.6)
            if first and offline and not exc.sent:
                raise write_unreachable(remote, detail, _now() - started) from None
        except http.ServerError as exc:
            if not _retryable(exc):
                if op_name.startswith("issue.") and exc.code in {"UNKNOWN_OP", "LOCAL_ONLY"}:
                    raise OpError(
                        exc.code,
                        "this server does not support the issue log; upgrade the server.",
                        {**exc.details, "op": op_name},
                    ) from None
                raise OpError(exc.code, exc.message, exc.details) from None
            reached = True
            detail = f"HTTP {exc.status} {exc.code}"
            progress.state = "busy"
            wait = exc.retry_after
        first = False
        now = _now()
        latest = deadline - LAST_ATTEMPT_MARGIN_SECONDS
        if wait is None:
            # The last backoff ends just before the deadline, so the retries
            # fill the budget.
            wait = min(backoff, latest - now)
        else:
            # A Retry-After (0 included) never retries faster than the backoff;
            # one past the budget is honored by giving up.
            wait = max(wait, backoff)
        backoff = min(backoff * 2, MAX_BACKOFF_SECONDS)
        if wait <= 0 or now + wait > latest:
            raise _give_up(remote, op_id, detail, reached, now - started)
        progress.begin(now)  # no silent wait: say so at once, in plain words
        progress.sleep(wait)
        now = _now()
        if now >= deadline:  # the sleep overshot: no attempt past the budget
            raise _give_up(remote, op_id, detail, reached, now - started)


def _give_up(
    remote: http.Remote, op_id: str, detail: str, reached: bool, waited: float
) -> OpError:
    if reached:
        return outcome_unknown(remote, op_id, detail)
    return write_unreachable(remote, detail, waited)


def op_status(remote: http.Remote, project: str, op_id: str) -> dict:
    """``GET /v1/projects/<project>/ops/<op_id>`` (SPEC §8.6)."""
    path = (
        f"/v1/projects/{urllib.parse.quote(project, safe='')}/ops/"
        f"{urllib.parse.quote(op_id, safe='')}"
    )
    try:
        return http.request(remote, "GET", path, policy=http.BULK).data()
    except http.Unreachable as exc:
        raise lookup_unreachable(remote, op_id, exc.reason) from None
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
