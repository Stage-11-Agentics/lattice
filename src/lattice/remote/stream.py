"""The change stream and ``/v1/info`` for the follower (SPEC §8.9), on the client
transport of :mod:`lattice.remote.http`.

Every request here goes through that module's one policy (SPEC §9.1): no
redirects (``PROXY_REJECTED``), credentials attached with
``add_unredirected_header``, only a Lattice server's answer counts
(``Lattice-Protocol``; ``text/event-stream`` for the stream,
``application/json`` for info). This module only adds SSE framing, and maps a
server that cannot be reached to ``OpError("SERVER_UNREACHABLE")``, the error
the follower reconnects on.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any
from urllib.parse import quote

from lattice.core.errors import OpError
from lattice.remote import http
from lattice.remote.http import Remote
from lattice.remote.sse import SSEEvent, SSEParser


def _unreachable(remote: Remote, exc: http.Unreachable) -> OpError:
    return OpError("SERVER_UNREACHABLE", f"Cannot reach {remote.alias} ({exc.reason}).")


def stream_path(project: str) -> str:
    return f"/v1/projects/{quote(project, safe='')}/stream"


def get_info(remote: Remote, *, policy: http.Policy = http.PROBE) -> dict[str, Any]:
    """``GET /v1/info``: the envelope's ``data``."""
    try:
        data = http.request(remote, "GET", "/v1/info", policy=policy).data()
    except http.Unreachable as exc:
        raise _unreachable(remote, exc) from None
    if not isinstance(data, dict):
        raise http.proxy_rejected(f"{remote.alias}: /v1/info did not answer an object")
    return data


class StreamConnection:
    """An open ``GET .../stream`` response, read as SSE events."""

    def __init__(self, remote: Remote, response: http.StreamResponse) -> None:
        self._remote = remote
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
            except http.Unreachable as exc:
                raise _unreachable(self._remote, exc) from None
            if not raw:
                return
            event = parser.feed(raw.decode("utf-8", errors="replace").rstrip("\r\n"))
            if event is not None:
                yield event

    def close(self) -> None:
        """Close the connection; a reader blocked in :meth:`events` then stops."""
        self._response.close()


def open_stream(
    remote: Remote,
    project: str,
    *,
    last_event_id: str | None,
    timeout: float,
) -> StreamConnection:
    """Open *project*'s change stream, resuming after *last_event_id* when given.

    *timeout* bounds the connect and every read: a stream that stays silent
    longer (no entry and no heartbeat) raises from :meth:`StreamConnection.events`.
    """
    headers = {"Cache-Control": "no-cache"}
    if last_event_id:
        headers["Last-Event-ID"] = last_event_id
    policy = http.Policy(min(timeout, http.PROBE.connect_seconds), timeout)
    try:
        response = http.open_stream(
            remote,
            stream_path(project),
            headers=headers,
            policy=policy,
            read_timeout=timeout,
            what="the change stream",
        )
    except http.Unreachable as exc:
        raise _unreachable(remote, exc) from None
    return StreamConnection(remote, response)
