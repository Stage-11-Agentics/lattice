"""The client transport: one policy for every request to a Lattice server (SPEC §9.1).

- **A Lattice User-Agent.** Every request says ``User-Agent: lattice/<version>``
  (bot protection such as Cloudflare's Browser Integrity Check refuses urllib's
  ``Python-urllib/*``), unless the remote's ``headers`` set one, which wins.
- **No redirects.** A 3xx response fails with ``PROXY_REJECTED``, naming the
  status and the ``Location`` host; it is never followed. ``Authorization`` and
  every remote header are attached with ``add_unredirected_header``, so no
  redirect handler could copy them either.
- **Only a Lattice server's answer counts.** Every response must carry
  ``Lattice-Protocol`` (a different value is ``PROTOCOL_MISMATCH``); a JSON
  endpoint's response must also be ``application/json`` with a parseable
  envelope. Anything else (a proxy's login page, an error page) fails with
  ``PROXY_REJECTED``, naming the status and content type: it is never read as
  success or as unreachable. One carve-out: a 502, 503 or 504 without
  ``Lattice-Protocol`` is a gateway failure, raised as :class:`GatewayUnavailable`
  (an :class:`Unreachable`), so every caller treats it as the server being
  unreachable. Every other status keeps ``PROXY_REJECTED``.
- **Bounded time.** A :class:`Policy` bounds the connect and the wait for the
  response to start; once the server has started answering, the body has
  60 seconds plus 2 seconds per MiB announced (``Content-Length``), with a
  progress line on stderr every 5 seconds of a long transfer (SPEC §9.5).

Failures to reach the server raise :class:`Unreachable` (internal to the
client); a Lattice error envelope raises :class:`ServerError`, an ``OpError``
carrying the server's code, message, and details.
"""

from __future__ import annotations

import contextlib
import functools
import http.client
import json
import math
import re
import socket
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Any

from lattice.core.errors import OpError

PROTOCOL = 1
HEADER_PROTOCOL = "Lattice-Protocol"
HEADER_CLIENT_VERSION = "Lattice-Client-Version"
HEADER_SERVER_VERSION = "Lattice-Server-Version"

_MIB = 1024 * 1024
_CHUNK = 256 * 1024
_PROGRESS_SECONDS = 5.0
_ERROR_BODY_LIMIT = _MIB


@dataclass(frozen=True)
class Remote:
    """A resolved remote: where to send requests and the credentials to attach."""

    alias: str
    url: str
    token: str | None = field(default=None, repr=False)
    headers: Mapping[str, str] = field(default_factory=dict, repr=False)
    #: Machine-local choices for this remote (SPEC §9.1): run the hosted board's
    #: hooks here, run its auto-reviews here, allow plaintext to a non-loopback
    #: host, and how long one operation may retry (SPEC §8.6).
    run_board_hooks: bool = False
    run_auto_reviews: bool = True
    allow_plaintext: bool = False
    retry_seconds: float = 15.0

    def __post_init__(self) -> None:
        object.__setattr__(self, "url", self.url.rstrip("/"))

    @property
    def origin(self) -> tuple[str, str, int | None]:
        parts = urllib.parse.urlsplit(self.url)
        return (parts.scheme.lower(), (parts.hostname or "").lower(), _port(parts))


@dataclass(frozen=True)
class Policy:
    """Time bounds for one request.

    ``connect_seconds`` bounds the TCP (and TLS) connect; ``response_seconds``
    bounds everything from the start of the request until the status line and
    headers arrive. The body then gets 60 s plus 2 s per MiB announced.
    ``progress`` names the transfer in stderr progress lines (``None``: quiet).
    """

    connect_seconds: float
    response_seconds: float
    progress: str | None = None

    def with_progress(self, label: str | None) -> Policy:
        return Policy(self.connect_seconds, self.response_seconds, label)


#: A command's catch-up (SPEC §9.5): 2 s to connect, 5 s until the answer starts.
PROBE = Policy(2.0, 5.0)
#: ``lattice sync``, ``remote attach``, and every fetch after a probe answered.
BULK = Policy(10.0, 60.0)


def body_budget_seconds(content_length: int | None) -> float:
    """60 seconds plus 2 seconds per MiB announced (SPEC §9.5)."""
    return 60.0 + 2.0 * ((content_length or 0) / _MIB)


class Unreachable(Exception):
    """The server could not be reached, or stopped answering within the policy.

    ``sent`` is true when a connection was made, so the request may have been
    received (it matters for writes, SPEC §8.6).
    """

    def __init__(self, reason: str, *, sent: bool = False, retry_after: float | None = None):
        super().__init__(reason)
        self.reason = reason
        self.sent = sent
        self.retry_after = retry_after


#: The statuses a gateway in front of the server answers when it cannot reach
#: it (SPEC §9.1): without ``Lattice-Protocol`` they mean "unreachable".
GATEWAY_STATUSES = frozenset({502, 503, 504})


class GatewayUnavailable(Unreachable):
    """A gateway in front of the server answered 502, 503 or 504 itself (no
    ``Lattice-Protocol``). ``sent`` is always true: the gateway may have
    forwarded the request, and the server committed it, before the gateway
    answered with its own error (SPEC §8.6)."""

    def __init__(self, reason: str, *, status: int, retry_after: float | None = None):
        super().__init__(reason, sent=True, retry_after=retry_after)
        self.status = status


class ServerError(OpError):
    """A Lattice server's error envelope: its code, message, and details, plus
    the HTTP status and any ``Retry-After``."""

    def __init__(
        self,
        code: str,
        message: str,
        details: dict[str, Any] | None,
        *,
        status: int,
        retry_after: float | None = None,
    ):
        super().__init__(code, message, details)
        self.status = status
        self.retry_after = retry_after


@dataclass
class Response:
    status: int
    headers: dict[str, str]  # lowercased names
    body: bytes

    @property
    def server_version(self) -> str | None:
        return self.headers.get(HEADER_SERVER_VERSION.lower())

    def data(self) -> Any:
        """The ``data`` of a success envelope (JSON endpoints only)."""
        return json.loads(self.body)["data"]


def proxy_rejected(message: str, **details: Any) -> OpError:
    return OpError("PROXY_REJECTED", message, {k: v for k, v in details.items() if v is not None})


# ---------------------------------------------------------------------------
# URL handling
# ---------------------------------------------------------------------------


def _port(parts: urllib.parse.SplitResult) -> int | None:
    try:
        port = parts.port
    except ValueError:
        return None
    if port is None:
        return {"http": 80, "https": 443}.get(parts.scheme.lower())
    return port


def href_url(remote: Remote, href: str) -> str | None:
    """The absolute URL of a server-supplied ``href``, or ``None`` if it is not
    a relative path on the same server (SPEC §9.4: never another origin)."""
    if not isinstance(href, str) or not href.startswith("/") or href.startswith("//"):
        return None
    parts = urllib.parse.urlsplit(href)
    if parts.scheme or parts.netloc or "\\" in href:
        return None
    return remote.url + href


# ---------------------------------------------------------------------------
# The opener: no redirects, bounded reads
# ---------------------------------------------------------------------------


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """Every redirect status stops the request with its raw ``Location``,
    before urllib parses (or could follow) it."""

    def http_error_302(self, req, fp, code, msg, headers):
        fp.close()
        raise _Redirected(code, headers.get("Location") or headers.get("URI"))

    http_error_301 = http_error_303 = http_error_307 = http_error_308 = http_error_302


class _Redirected(Exception):
    def __init__(self, status: int, location: str | None):
        super().__init__(status)
        self.status = status
        self.location = location


@dataclass
class _Socket:
    """The one connection a request makes, and its response-start deadline.

    ``response_seconds`` is an absolute budget from the start of the request
    until the status line and headers have arrived: the socket timeout bounds
    each wait inside it, and a watchdog shuts the socket down when the budget
    runs out, so a peer dribbling header bytes cannot stretch it. Once the
    headers are in, the body runs under its own deadline (:func:`_read_body`).
    """

    started: float
    response_seconds: float
    connected: bool = False
    expired: bool = False
    sock: socket.socket | None = None
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)
    _watchdog: threading.Timer | None = field(default=None, repr=False)

    def remaining(self) -> float:
        return self.started + self.response_seconds - time.monotonic()

    def arm(self) -> None:
        self._watchdog = threading.Timer(max(0.0, self.remaining()), self._expire)
        self._watchdog.daemon = True
        self._watchdog.start()

    def stop(self) -> None:
        """Stop the watchdog (the answer started, or the request failed)."""
        if self._watchdog is not None:
            self._watchdog.cancel()

    def expiry_reason(self) -> str:
        return f"no answer within {self.response_seconds:g} s"

    def attach(self, sock: socket.socket) -> None:
        with self._lock:
            if self.expired:
                raise TimeoutError("the answer did not start in time")
            sock.settimeout(max(0.05, self.remaining()))
            self.sock = sock
            self.connected = True

    def _expire(self) -> None:
        with self._lock:
            self.expired = True
            if self.sock is not None:
                with contextlib.suppress(OSError):
                    self.sock.shutdown(socket.SHUT_RDWR)

    def body_timeout(self, seconds: float) -> None:
        """Bound the next body read by what is left of the body's deadline."""
        if self.sock is not None:
            with contextlib.suppress(OSError):  # closed once the body is complete
                self.sock.settimeout(max(0.05, seconds))


def _connection_class(base: type[http.client.HTTPConnection], holder: _Socket) -> Callable:
    class _Conn(base):  # type: ignore[misc, valid-type]
        def connect(self) -> None:
            super().connect()
            holder.attach(self.sock)

    return _Conn


class _HTTPHandler(urllib.request.HTTPHandler):
    def __init__(self, holder: _Socket):
        super().__init__()
        self._holder = holder

    def http_open(self, req):
        return self.do_open(_connection_class(http.client.HTTPConnection, self._holder), req)


@functools.cache
def _tls_context() -> Any:
    """The default TLS context, built once per process: building one loads the
    system's CA store, which cost every request about 10 ms (for an ``http://``
    URL too, since urllib builds its HTTPS handler regardless)."""
    import ssl

    return ssl.create_default_context()


class _HTTPSHandler(urllib.request.HTTPSHandler):
    def __init__(self, holder: _Socket):
        super().__init__(context=_tls_context())
        self._holder = holder

    def https_open(self, req):
        return self.do_open(
            _connection_class(http.client.HTTPSConnection, self._holder),
            req,
            context=self._context,
        )


# ---------------------------------------------------------------------------
# Requests
# ---------------------------------------------------------------------------


def _client_version() -> str:
    try:
        from lattice import __version__
    except Exception:  # noqa: BLE001 - an uninstalled tree has no version
        return "0"
    return __version__


#: Characters RFC 9110 allows in a product version (``token``).
_NOT_TOKEN = re.compile(r"[^A-Za-z0-9!#$%&'*+.^_`|~-]")


def _version_token() -> str:
    """The version ``lattice --version`` prints, less any character a header
    token may not hold: no version string can inject a header or make
    ``http.client`` refuse the request."""
    return _NOT_TOKEN.sub("", _client_version()) or "0"


def user_agent() -> str:
    """``lattice/<version>`` (SPEC §9.1)."""
    return f"lattice/{_version_token()}"


def build_request(
    remote: Remote,
    method: str,
    url: str,
    body: bytes | None = None,
    *,
    content_type: str | None = None,
    headers: Mapping[str, str] | None = None,
) -> urllib.request.Request:
    req = urllib.request.Request(url, data=body, method=method)
    req.add_header(HEADER_PROTOCOL, str(PROTOCOL))
    req.add_header(HEADER_CLIENT_VERSION, _version_token())
    req.add_header("Accept", "application/json")
    # urllib adds ``Python-urllib/*`` only when no User-Agent is set; a remote
    # header of that name (any case) replaces this one below.
    req.add_unredirected_header("User-Agent", user_agent())
    if body is not None:
        req.add_header("Content-Type", content_type or "application/json")
    # Credentials never ride a redirect (SPEC §9.1).
    if remote.token:
        req.add_unredirected_header("Authorization", f"Bearer {remote.token}")
    for name, value in remote.headers.items():
        req.add_unredirected_header(name, value)
    for name, value in (headers or {}).items():
        req.add_header(name, value)
    return req


def request(
    remote: Remote,
    method: str,
    path: str,
    *,
    json_body: Any = None,
    raw_body: bytes | None = None,
    content_type: str | None = None,
    headers: Mapping[str, str] | None = None,
    expect: str = "json",
    policy: Policy = PROBE,
    sink: Callable[[bytes], None] | None = None,
    what: str | None = None,
) -> Response:
    """Send one request to *remote* and check that a Lattice server answered.

    ``path`` is server-relative (``/v1/...``). ``expect`` is ``"json"`` (an
    envelope; a success returns its body) or ``"bytes"`` (a raw file; errors are
    still envelopes). With ``sink``, a successful body is handed to it chunk by
    chunk instead of being kept in memory. ``what`` names the request in error
    messages (default: the method and path).

    Raises ``Unreachable`` (``GatewayUnavailable`` for a gateway's 502, 503 or
    504), ``ServerError`` (a Lattice error envelope), ``PROXY_REJECTED``, or
    ``PROTOCOL_MISMATCH``.
    """
    url = remote.url + path
    what = what or f"{method} {path.split('?', 1)[0]}"
    if json_body is not None and raw_body is not None:
        raise ValueError("request accepts either json_body or raw_body, not both")
    body = (
        raw_body
        if raw_body is not None
        else (None if json_body is None else json.dumps(json_body).encode("utf-8"))
    )
    req = build_request(remote, method, url, body, content_type=content_type, headers=headers)
    holder = _Socket(time.monotonic(), policy.response_seconds)
    opener = urllib.request.build_opener(
        _NoRedirect(), _HTTPHandler(holder), _HTTPSHandler(holder)
    )
    holder.arm()
    try:
        try:
            response = opener.open(
                req, timeout=max(0.05, min(policy.connect_seconds, holder.remaining()))
            )
        except urllib.error.HTTPError as exc:
            response = exc
    except _Redirected as exc:
        holder.stop()
        raise _redirect_error(remote, what, exc.status, exc.location) from None
    except (urllib.error.URLError, OSError, http.client.HTTPException, ValueError) as exc:
        holder.stop()
        reason = holder.expiry_reason() if holder.expired else _reason(exc)
        raise Unreachable(reason, sent=holder.connected) from None
    holder.stop()
    if holder.expired:  # the watchdog fired as the headers completed
        response.close()
        raise Unreachable(holder.expiry_reason(), sent=True)

    with response:
        status = response.status if hasattr(response, "status") else response.code
        headers = {k.lower(): v for k, v in response.headers.items()}
        if 300 <= status < 400:
            raise _redirect_error(remote, what, status, headers.get("location"))
        ok = 200 <= status < 300
        limit = None if ok else _ERROR_BODY_LIMIT
        payload = _read_body(
            response,
            holder,
            headers,
            policy,
            sink=sink if ok else None,
            limit=limit,
        )
    return _check(remote, what, status, headers, payload, expect=expect, streamed=ok and sink)


def _read_body(
    response: Any,
    holder: _Socket,
    headers: Mapping[str, str],
    policy: Policy,
    *,
    sink: Callable[[bytes], None] | None,
    limit: int | None,
) -> bytes:
    try:
        length = int(headers.get("content-length", ""))
    except ValueError:
        length = None
    started = time.monotonic()
    deadline = started + body_budget_seconds(length)
    next_progress = started + _PROGRESS_SECONDS
    chunks: list[bytes] = []
    received = 0
    read = getattr(response, "read1", None) or response.read  # one recv per call
    try:
        while True:
            now = time.monotonic()
            if now >= deadline:
                raise Unreachable("the transfer ran out of time", sent=True)
            holder.body_timeout(deadline - now)
            chunk = read(_CHUNK)
            if not chunk:
                break
            received += len(chunk)
            if sink is not None:
                sink(chunk)
            else:
                chunks.append(chunk)
                if limit is not None and received >= limit:
                    break
            if policy.progress and time.monotonic() >= next_progress:
                next_progress = time.monotonic() + _PROGRESS_SECONDS
                total = f" of {length / _MIB:.1f}" if length else ""
                print(
                    f"lattice: {policy.progress}: {received / _MIB:.1f}{total} MiB",
                    file=sys.stderr,
                )
    except Unreachable:
        raise
    except (OSError, http.client.HTTPException, ValueError) as exc:
        raise Unreachable(_reason(exc), sent=True) from None
    if length is not None and limit is None and received != length:
        raise Unreachable(f"the connection closed after {received} of {length} bytes", sent=True)
    return b"".join(chunks)


def _reason(exc: BaseException) -> str:
    if isinstance(exc, urllib.error.URLError) and not isinstance(exc, urllib.error.HTTPError):
        inner = exc.reason
        return str(inner) if not isinstance(inner, BaseException) else _reason(inner)
    if isinstance(exc, (TimeoutError, socket.timeout)):
        return "timed out"
    return str(exc) or type(exc).__name__


def _redirect_error(remote: Remote, what: str, status: int, location: str | None) -> OpError:
    try:
        host = urllib.parse.urlsplit(location).hostname if location else None
    except ValueError:  # a malformed Location is still a redirect, and still refused
        host = None
    target = f" to {host}" if host else ""
    return proxy_rejected(
        f"{remote.alias}: the answer to {what} was an HTTP {status} redirect{target}, "
        "not a Lattice server's answer; Lattice never follows redirects. A proxy in "
        "front of the server may need login or extra headers (remote headers).",
        status=status,
        location_host=host,
    )


def _not_lattice(remote: Remote, what: str, status: int, content_type: str | None) -> OpError:
    kind = content_type or "no content type"
    return proxy_rejected(
        f"{remote.alias}: the answer to {what} (HTTP {status}, {kind}) is not a Lattice "
        "server's answer. A proxy in front of the server may need login or extra "
        "headers (remote headers).",
        status=status,
        content_type=content_type,
    )


def _check(
    remote: Remote,
    what: str,
    status: int,
    headers: Mapping[str, str],
    payload: bytes,
    *,
    expect: str,
    streamed: Any,
) -> Response:
    content_type = headers.get("content-type")
    protocol = headers.get(HEADER_PROTOCOL.lower())
    if protocol is None:
        if status in GATEWAY_STATUSES:
            kind = content_type or "no content type"
            raise GatewayUnavailable(
                f"a gateway in front of it answered HTTP {status} ({kind})",
                status=status,
                retry_after=parse_retry_after(headers.get("retry-after")),
            )
        raise _not_lattice(remote, what, status, content_type)
    if protocol.strip() != str(PROTOCOL):
        raise OpError(
            "PROTOCOL_MISMATCH",
            f"{remote.alias} speaks Lattice protocol {protocol.strip()}; this client speaks "
            f"protocol {PROTOCOL}. Upgrade Lattice on the side that is older.",
            {"server_protocol": protocol.strip(), "client_protocol": PROTOCOL},
        )
    ok = 200 <= status < 300
    if ok and expect == "bytes":
        return Response(status, dict(headers), payload)
    if ok and streamed:
        return Response(status, dict(headers), b"")
    is_json = (content_type or "").split(";", 1)[0].strip().lower() == "application/json"
    envelope = _parse_envelope(payload) if is_json else None
    if envelope is None:
        raise _not_lattice(remote, what, status, content_type)
    if envelope["ok"] and ok:
        return Response(status, dict(headers), payload)
    error = envelope.get("error")
    if envelope["ok"] or not isinstance(error, dict):
        raise _not_lattice(remote, what, status, content_type)
    details = error.get("details") if isinstance(error.get("details"), dict) else None
    raise ServerError(
        str(error.get("code") or "INTEGRITY_ERROR"),
        str(error.get("message") or f"HTTP {status}"),
        details,
        status=status,
        retry_after=parse_retry_after(headers.get("retry-after")),
    )


#: The longest ``Retry-After`` the client honors; the retry budget still bounds it.
MAX_RETRY_AFTER_SECONDS = 60.0


def parse_retry_after(value: str | None) -> float | None:
    """``Retry-After`` in seconds, or ``None`` when absent or unusable (negative,
    NaN, infinite, not a number): the caller then backs off as usual. Values
    above :data:`MAX_RETRY_AFTER_SECONDS` are capped."""
    try:
        seconds = float(value) if value is not None else None
    except ValueError:
        return None
    if seconds is None or not math.isfinite(seconds) or seconds < 0:
        return None
    return min(seconds, MAX_RETRY_AFTER_SECONDS)


def _parse_envelope(payload: bytes) -> dict | None:
    try:
        data = json.loads(payload)
    except ValueError:
        return None
    if not isinstance(data, dict) or not isinstance(data.get("ok"), bool):
        return None
    if data["ok"] and "data" not in data:
        return None
    return data


# ---------------------------------------------------------------------------
# Streaming responses (the change stream, SPEC §8.9)
# ---------------------------------------------------------------------------


class StreamResponse:
    """An open streaming response (``text/event-stream``), read line by line.

    Every read waits at most the *read_timeout* given to :func:`open_stream`;
    :meth:`close` shuts the socket down, so a reader blocked in
    :meth:`readline` on another thread returns at once.
    """

    def __init__(self, response: Any, holder: _Socket, headers: dict[str, str]):
        self._response = response
        self._holder = holder
        self.headers = headers  # lowercased names

    def readline(self) -> bytes:
        """One line (with its terminator), or ``b""`` when the server closed the
        stream. Raises :class:`Unreachable` when a read times out or breaks."""
        try:
            return self._response.readline()
        except (OSError, http.client.HTTPException, ValueError) as exc:
            raise Unreachable(_reason(exc), sent=True) from None

    def close(self) -> None:
        sock = self._holder.sock
        if sock is not None:
            with contextlib.suppress(OSError):
                sock.shutdown(socket.SHUT_RDWR)
        with contextlib.suppress(OSError, ValueError):
            self._response.close()


def open_stream(
    remote: Remote,
    path: str,
    *,
    headers: Mapping[str, str] | None = None,
    policy: Policy = PROBE,
    read_timeout: float,
    what: str | None = None,
) -> StreamResponse:
    """Open a streaming ``GET`` under the same policy as :func:`request`.

    The response must be a Lattice server's ``text/event-stream``: a redirect,
    a missing ``Lattice-Protocol``, or any other content type is
    ``PROXY_REJECTED`` (a different protocol is ``PROTOCOL_MISMATCH``; a
    gateway's 502, 503 or 504 is :class:`GatewayUnavailable`), and a Lattice
    error envelope raises :class:`ServerError`. *policy* bounds the
    connect and the wait for the headers; after that each read waits at most
    *read_timeout* (the stream is endless, so it has no body deadline).
    """
    url = remote.url + path
    what = what or f"GET {path.split('?', 1)[0]}"
    req = build_request(remote, "GET", url)
    req.add_header("Accept", "text/event-stream")
    for name, value in (headers or {}).items():
        req.add_header(name, value)
    holder = _Socket(time.monotonic(), policy.response_seconds)
    opener = urllib.request.build_opener(
        _NoRedirect(), _HTTPHandler(holder), _HTTPSHandler(holder)
    )
    holder.arm()
    try:
        try:
            response = opener.open(
                req, timeout=max(0.05, min(policy.connect_seconds, holder.remaining()))
            )
        except urllib.error.HTTPError as exc:
            response = exc
    except _Redirected as exc:
        holder.stop()
        raise _redirect_error(remote, what, exc.status, exc.location) from None
    except (urllib.error.URLError, OSError, http.client.HTTPException, ValueError) as exc:
        holder.stop()
        reason = holder.expiry_reason() if holder.expired else _reason(exc)
        raise Unreachable(reason, sent=holder.connected) from None
    holder.stop()
    if holder.expired:
        response.close()
        raise Unreachable(holder.expiry_reason(), sent=True)
    status = response.status if hasattr(response, "status") else response.code
    response_headers = {k.lower(): v for k, v in response.headers.items()}
    content_type = (response_headers.get("content-type") or "").split(";", 1)[0].strip().lower()
    if 300 <= status < 400:
        response.close()
        raise _redirect_error(remote, what, status, response_headers.get("location"))
    if not (200 <= status < 300) or content_type != "text/event-stream":
        # Not an event stream: read what is there (bounded) and let the one
        # response check name it (an error envelope, a proxy page, a mismatch).
        with response:
            payload = _read_body(
                response, holder, response_headers, policy, sink=None, limit=_ERROR_BODY_LIMIT
            )
        _check(remote, what, status, response_headers, payload, expect="json", streamed=None)
        raise _not_lattice(remote, what, status, response_headers.get("content-type"))
    protocol = response_headers.get(HEADER_PROTOCOL.lower())
    if protocol is None or protocol.strip() != str(PROTOCOL):
        response.close()
        _check(remote, what, status, response_headers, b"", expect="bytes", streamed=None)
    holder.body_timeout(read_timeout)
    return StreamResponse(response, holder, response_headers)
