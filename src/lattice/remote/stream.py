"""The client transport for ``/v1/info`` and the change stream (SPEC §9.1, §8.9).

One policy, the same as every other client request:

- **No redirects.** A 3xx fails with ``PROXY_REJECTED`` naming the status and
  the ``Location`` host, and is never followed. ``Authorization`` and every
  remote header are attached with ``add_unredirected_header``, so no redirect
  handler could copy them either.
- **Only a Lattice server's answer counts.** Every response must carry
  ``Lattice-Protocol``; a JSON endpoint's must be ``application/json`` with a
  parseable envelope, and the stream's must be ``text/event-stream``. Anything
  else fails with ``PROXY_REJECTED`` naming the status and content type.
- A connection that cannot be made or times out is ``SERVER_UNREACHABLE``.
"""

from __future__ import annotations

import http.client
import json
import socket
import urllib.error
import urllib.request
from collections.abc import Iterator
from typing import Any
from urllib.parse import urlsplit

from lattice import __version__
from lattice.core.errors import OpError
from lattice.remote.endpoint import RemoteEndpoint
from lattice.remote.sse import SSEEvent, SSEParser

PROTOCOL = "1"
HEADER_PROTOCOL = "Lattice-Protocol"


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """Refuse every redirect: ``urlopen`` then raises the 3xx as an ``HTTPError``."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: ANN001, ANN201
        return None


_OPENER = urllib.request.build_opener(_NoRedirect)


def _request(endpoint: RemoteEndpoint, url: str, accept: str, extra: dict[str, str]) -> Any:
    req = urllib.request.Request(url, method="GET")
    req.add_unredirected_header("Authorization", f"Bearer {endpoint.token}")
    for name, value in endpoint.headers.items():
        req.add_unredirected_header(name, value)
    req.add_header("Accept", accept)
    req.add_header(HEADER_PROTOCOL, PROTOCOL)
    req.add_header("Lattice-Client-Version", __version__)
    for name, value in extra.items():
        req.add_header(name, value)
    return req


def _content_type(headers: Any) -> str:
    return (headers.get("Content-Type") or "").split(";")[0].strip().lower()


def _rejected(status: int, headers: Any, reason: str) -> OpError:
    content_type = _content_type(headers) or "none"
    return OpError(
        "PROXY_REJECTED",
        f"The response did not come from a Lattice server ({reason}; "
        f"status {status}, content type {content_type}).",
        {"status": status, "content_type": content_type},
    )


def _redirect_error(status: int, headers: Any) -> OpError:
    location = headers.get("Location") or ""
    host = urlsplit(location).hostname or "(none)"
    return OpError(
        "PROXY_REJECTED",
        f"The server answered with a redirect (status {status}, to host {host}); "
        "a proxy in front of it is not letting this client through. "
        "Lattice never follows redirects.",
        {"status": status, "location_host": host},
    )


def _error_from_http(exc: urllib.error.HTTPError) -> OpError:
    """Map a non-2xx response to the server's own error, or ``PROXY_REJECTED``."""
    status, headers = exc.code, exc.headers
    if 300 <= status < 400:
        return _redirect_error(status, headers)
    if headers.get(HEADER_PROTOCOL) is None:
        return _rejected(status, headers, "no Lattice-Protocol header")
    if _content_type(headers) != "application/json":
        return _rejected(status, headers, "not a JSON envelope")
    try:
        body = json.loads(exc.read() or b"null")
        error = body["error"]
        return OpError(str(error["code"]), str(error["message"]), error.get("details"))
    except (ValueError, KeyError, TypeError, OSError):
        return _rejected(status, headers, "not a JSON envelope")


def _unreachable(endpoint: RemoteEndpoint, exc: BaseException) -> OpError:
    return OpError(
        "SERVER_UNREACHABLE",
        f"Cannot reach {endpoint.alias} ({exc.__class__.__name__}).",
    )


def _open(endpoint: RemoteEndpoint, req: Any, timeout: float) -> Any:
    try:
        return _OPENER.open(req, timeout=timeout)
    except urllib.error.HTTPError as exc:
        raise _error_from_http(exc) from None
    except (urllib.error.URLError, OSError, http.client.HTTPException) as exc:
        raise _unreachable(endpoint, exc) from None


def get_info(endpoint: RemoteEndpoint, *, timeout: float = 5.0) -> dict[str, Any]:
    """``GET /v1/info``: the envelope's ``data``."""
    req = _request(endpoint, endpoint.root("/v1/info"), "application/json", {})
    resp = _open(endpoint, req, timeout)
    with resp:
        if resp.headers.get(HEADER_PROTOCOL) is None:
            raise _rejected(resp.status, resp.headers, "no Lattice-Protocol header")
        if _content_type(resp.headers) != "application/json":
            raise _rejected(resp.status, resp.headers, "not a JSON envelope")
        try:
            body = json.loads(resp.read())
            if body.get("ok") is not True or not isinstance(body.get("data"), dict):
                raise ValueError("bad envelope")
        except (ValueError, AttributeError, OSError):
            raise _rejected(resp.status, resp.headers, "not a JSON envelope") from None
        return body["data"]


class StreamConnection:
    """An open ``GET .../stream`` response, read as SSE events."""

    def __init__(self, response: Any) -> None:
        self._response = response

    def events(self) -> Iterator[SSEEvent]:
        """Every event until the server closes the stream.

        A read that times out, or a connection that breaks, raises
        ``OpError("SERVER_UNREACHABLE")``.
        """
        parser = SSEParser()
        while True:
            try:
                raw = self._response.readline()
            except (OSError, ValueError, http.client.HTTPException) as exc:
                raise OpError(
                    "SERVER_UNREACHABLE", f"The stream broke ({exc.__class__.__name__})."
                ) from None
            if not raw:
                return
            line = raw.decode("utf-8", errors="replace").rstrip("\r\n")
            event = parser.feed(line)
            if event is not None:
                yield event

    def close(self) -> None:
        """Close the connection; a reader blocked in :meth:`events` then stops."""
        try:
            sock = self._response.fp.raw._sock  # the blocked readline sits on this
            sock.shutdown(socket.SHUT_RDWR)
        except (AttributeError, OSError, ValueError):
            pass
        try:
            self._response.close()
        except (OSError, ValueError):
            pass


def open_stream(
    endpoint: RemoteEndpoint,
    *,
    last_event_id: str | None,
    timeout: float,
) -> StreamConnection:
    """Open the project's change stream, resuming after *last_event_id* when given.

    *timeout* bounds the connect and every read: a stream that stays silent
    longer (no entry and no heartbeat) raises from :meth:`StreamConnection.events`.
    """
    extra = {"Cache-Control": "no-cache"}
    if last_event_id:
        extra["Last-Event-ID"] = last_event_id
    req = _request(endpoint, endpoint.api("stream"), "text/event-stream", extra)
    resp = _open(endpoint, req, timeout)
    if resp.headers.get(HEADER_PROTOCOL) is None:
        resp.close()
        raise _rejected(resp.status, resp.headers, "no Lattice-Protocol header")
    if _content_type(resp.headers) != "text/event-stream":
        resp.close()
        raise _rejected(resp.status, resp.headers, "not an event stream")
    return StreamConnection(resp)
