"""The client transport: one policy for every request to a Lattice server (SPEC §9.1).

- **No redirects.** A 3xx response fails with ``PROXY_REJECTED``, naming the
  status and the ``Location`` host; it is never followed. ``Authorization`` and
  every remote header are attached with ``add_unredirected_header``, so no
  redirect handler could copy them either.
- **Only a Lattice server's answer counts.** Every response must carry
  ``Lattice-Protocol`` (a different value is ``PROTOCOL_MISMATCH``); a JSON
  endpoint's response must also be ``application/json`` with a parseable
  envelope. Anything else (a proxy's login page, an error page) fails with
  ``PROXY_REJECTED``, naming the status and content type. The one exception is
  a gateway status (502, 503, 504) without ``Lattice-Protocol``: that is a
  proxy saying the server behind it is down, so it reads as unreachable.
- **Bounded time.** A :class:`Policy` bounds the connect and the wait for the
  response to start; once the server has started answering, the body has
  60 seconds plus 2 seconds per MiB announced (``Content-Length``), with a
  progress line on stderr every 5 seconds of a long transfer (SPEC §9.5).

Failures to reach the server raise :class:`Unreachable` (internal to the
client); a Lattice error envelope raises :class:`ServerError`, an ``OpError``
carrying the server's code, message, and details.
"""

from __future__ import annotations

import http.client
import json
import socket
import sys
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
_GATEWAY_STATUSES = frozenset({502, 503, 504})


@dataclass(frozen=True)
class Remote:
    """A resolved remote: where to send requests and the credentials to attach."""

    alias: str
    url: str
    token: str | None = None
    headers: Mapping[str, str] = field(default_factory=dict)

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

    def __init__(self, reason: str, *, sent: bool = False):
        super().__init__(reason)
        self.reason = reason
        self.sent = sent


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
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise _Redirected(code, headers.get("Location") or newurl)


class _Redirected(Exception):
    def __init__(self, status: int, location: str | None):
        super().__init__(status)
        self.status = status
        self.location = location


@dataclass
class _Socket:
    """What the connection factory saw: whether it connected.

    The socket's timeout, set once connected, bounds the wait for the answer to
    start and then each read of the body; the body's total budget is checked
    between reads.
    """

    started: float
    response_seconds: float
    connected: bool = False

    def read_timeout(self) -> float:
        return max(0.05, self.started + self.response_seconds - time.monotonic())


def _connection_class(base: type[http.client.HTTPConnection], holder: _Socket) -> Callable:
    class _Conn(base):  # type: ignore[misc, valid-type]
        def connect(self) -> None:
            super().connect()
            # The connect used the policy's connect timeout; from here on the
            # wait for the answer is bounded by what is left of its budget.
            self.sock.settimeout(holder.read_timeout())
            holder.connected = True

    return _Conn


class _HTTPHandler(urllib.request.HTTPHandler):
    def __init__(self, holder: _Socket):
        super().__init__()
        self._holder = holder

    def http_open(self, req):
        return self.do_open(_connection_class(http.client.HTTPConnection, self._holder), req)


class _HTTPSHandler(urllib.request.HTTPSHandler):
    def __init__(self, holder: _Socket):
        super().__init__()
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


def build_request(
    remote: Remote, method: str, url: str, body: bytes | None = None
) -> urllib.request.Request:
    req = urllib.request.Request(url, data=body, method=method)
    req.add_header(HEADER_PROTOCOL, str(PROTOCOL))
    req.add_header(HEADER_CLIENT_VERSION, _client_version())
    req.add_header("Accept", "application/json")
    if body is not None:
        req.add_header("Content-Type", "application/json")
    # Credentials never ride a redirect (SPEC §9.1).
    if remote.token:
        req.add_unredirected_header("Authorization", f"Bearer {remote.token}")
    for name, value in remote.headers.items():
        req.add_unredirected_header(name, value)
    return req


def request(
    remote: Remote,
    method: str,
    path: str,
    *,
    json_body: Any = None,
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

    Raises ``Unreachable``, ``ServerError`` (a Lattice error envelope),
    ``PROXY_REJECTED``, or ``PROTOCOL_MISMATCH``.
    """
    url = remote.url + path
    what = what or f"{method} {path.split('?', 1)[0]}"
    body = None if json_body is None else json.dumps(json_body).encode("utf-8")
    req = build_request(remote, method, url, body)
    holder = _Socket(time.monotonic(), policy.response_seconds)
    opener = urllib.request.build_opener(
        _NoRedirect(), _HTTPHandler(holder), _HTTPSHandler(holder)
    )
    try:
        try:
            response = opener.open(req, timeout=policy.connect_seconds)
        except urllib.error.HTTPError as exc:
            response = exc
    except _Redirected as exc:
        raise _redirect_error(remote, what, exc.status, exc.location) from None
    except (urllib.error.URLError, OSError, http.client.HTTPException, ValueError) as exc:
        raise Unreachable(_reason(exc), sent=holder.connected) from None

    with response:
        status = response.status if hasattr(response, "status") else response.code
        headers = {k.lower(): v for k, v in response.headers.items()}
        if 300 <= status < 400:
            raise _redirect_error(remote, what, status, headers.get("location"))
        ok = 200 <= status < 300
        limit = None if ok else _ERROR_BODY_LIMIT
        payload = _read_body(
            response,
            headers,
            policy,
            sink=sink if ok else None,
            limit=limit,
        )
    return _check(remote, what, status, headers, payload, expect=expect, streamed=ok and sink)


def _read_body(
    response: Any,
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
    try:
        while True:
            now = time.monotonic()
            if now > deadline:
                raise Unreachable("the transfer ran out of time", sent=True)
            chunk = response.read(_CHUNK)
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
    host = urllib.parse.urlsplit(location).hostname if location else None
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
        if status in _GATEWAY_STATUSES:
            raise Unreachable(f"HTTP {status} from a proxy in front of the server", sent=True)
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
    retry_after: float | None
    try:
        retry_after = float(headers.get("retry-after", ""))
    except ValueError:
        retry_after = None
    raise ServerError(
        str(error.get("code") or "INTEGRITY_ERROR"),
        str(error.get("message") or f"HTTP {status}"),
        details,
        status=status,
        retry_after=retry_after,
    )


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
