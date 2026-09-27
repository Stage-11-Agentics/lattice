"""A browser-like HTTP client for the hosted dashboard tests: one cookie jar,
no redirect following, and an ``Origin`` header of the caller's choosing."""

from __future__ import annotations

import http.client
import json
import urllib.parse
from dataclasses import dataclass, field
from typing import Any

from lattice.server.testing import ServerHandle

SESSION_COOKIE = "lattice_session"

#: Failures that mean a kept-alive connection went stale before any response
#: byte arrived (``RemoteDisconnected`` is raised only when the status line never
#: started). ``IncompleteRead``, ``BadStatusLine``, ``LineTooLong`` and the like
#: are protocol failures and are never retried.
STALE_CONNECTION = (http.client.RemoteDisconnected, BrokenPipeError, ConnectionResetError)


@dataclass
class WebResponse:
    status: int
    headers: dict[str, str]
    raw_headers: list[tuple[str, str]]
    text: str

    @property
    def json(self) -> Any:
        return json.loads(self.text)

    def set_cookies(self) -> list[str]:
        return [v for k, v in self.raw_headers if k.lower() == "set-cookie"]


@dataclass
class WebClient:
    """With ``keep_alive``, one persistent connection is reused across requests
    (a GET is retried once on a fresh connection if the reused one fails), as a
    browser does; otherwise each request opens and closes its own."""

    server: ServerHandle
    cookies: dict[str, str] = field(default_factory=dict)
    keep_alive: bool = False
    _conn: http.client.HTTPConnection | None = field(default=None, repr=False)

    def close(self) -> None:
        if self._conn is not None:
            self._conn.close()
            self._conn = None

    def _exchange(
        self, method: str, path: str, body: bytes | None, headers: dict[str, str]
    ) -> tuple[int, bytes, list[tuple[str, str]]]:
        if not self.keep_alive:
            conn = http.client.HTTPConnection("127.0.0.1", self.server.port, timeout=30)
            try:
                conn.request(method, path, body=body, headers=headers)
                resp = conn.getresponse()
                return resp.status, resp.read(), resp.getheaders()
            finally:
                conn.close()
        for attempt in (1, 2):
            reused = self._conn is not None
            if self._conn is None:
                self._conn = http.client.HTTPConnection("127.0.0.1", self.server.port, timeout=30)
            try:
                try:
                    self._conn.request(method, path, body=body, headers=headers)
                    resp = self._conn.getresponse()
                except STALE_CONNECTION:
                    # The server closed the idle connection before sending one byte
                    # of a response: only a reused connection's GET may try again.
                    self.close()
                    if attempt == 2 or not reused or method != "GET":
                        raise
                    continue
                raw = resp.read()  # a truncated or malformed response always propagates
            except BaseException:
                self.close()
                raise
            if resp.getheader("connection", "").lower() == "close":
                self.close()
            return resp.status, raw, resp.getheaders()
        raise AssertionError("unreachable")

    @property
    def origin(self) -> str:
        return self.server.url

    @property
    def session(self) -> str | None:
        return self.cookies.get(SESSION_COOKIE)

    def request(
        self,
        method: str,
        path: str,
        *,
        body: bytes | None = None,
        headers: dict[str, str] | None = None,
        send_cookies: bool = True,
    ) -> WebResponse:
        all_headers = dict(headers or {})
        if send_cookies and self.cookies:
            all_headers["Cookie"] = "; ".join(f"{k}={v}" for k, v in self.cookies.items())
        status, raw, raw_headers = self._exchange(method, path, body, all_headers)
        response = WebResponse(
            status,
            {k.lower(): v for k, v in raw_headers},
            raw_headers,
            raw.decode("utf-8", errors="replace"),
        )
        for cookie in response.set_cookies():
            name, _, rest = cookie.partition("=")
            value = rest.split(";", 1)[0]
            if value == "" or "max-age=0" in cookie.lower():
                self.cookies.pop(name.strip(), None)
            else:
                self.cookies[name.strip()] = value
        return response

    def get(self, path: str, **headers: str) -> WebResponse:
        return self.request("GET", path, headers=headers)

    def post_json(
        self, path: str, data: Any, *, origin: str | None = "same", **headers: str
    ) -> WebResponse:
        all_headers = {"Content-Type": "application/json", **headers}
        if origin == "same":
            all_headers["Origin"] = self.origin
        elif origin is not None:
            all_headers["Origin"] = origin
        return self.request(
            "POST", path, body=json.dumps(data).encode("utf-8"), headers=all_headers
        )

    def login(
        self, token: str, *, origin: str | None = "same", next_path: str | None = None
    ) -> WebResponse:
        form = {"token": token}
        if next_path is not None:
            form["next"] = next_path
        headers = {"Content-Type": "application/x-www-form-urlencoded"}
        if origin == "same":
            headers["Origin"] = self.origin
        elif origin is not None:
            headers["Origin"] = origin
        return self.request(
            "POST", "/login", body=urllib.parse.urlencode(form).encode(), headers=headers
        )

    def logout(self, *, origin: str | None = "same", **headers: str) -> WebResponse:
        """``POST /logout`` with ``{}``, as ``/web/logout.js`` sends it."""
        return self.post_json("/logout", {}, origin=origin, **headers)
