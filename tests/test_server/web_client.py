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
    server: ServerHandle
    cookies: dict[str, str] = field(default_factory=dict)

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
        conn = http.client.HTTPConnection("127.0.0.1", self.server.port, timeout=30)
        all_headers = dict(headers or {})
        if send_cookies and self.cookies:
            all_headers["Cookie"] = "; ".join(f"{k}={v}" for k, v in self.cookies.items())
        try:
            conn.request(method, path, body=body, headers=all_headers)
            resp = conn.getresponse()
            raw = resp.read()
            raw_headers = resp.getheaders()
        finally:
            conn.close()
        response = WebResponse(
            resp.status,
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
