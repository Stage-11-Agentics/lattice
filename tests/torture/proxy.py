"""A header-checking reverse proxy stub for Scenario B (AC-41).

It stands in for an access proxy in front of the server (the kind that admits a
remote environment by service-token headers): a request carrying every required
header with the right value is forwarded to the server, and anything else gets
the proxy's own answer, a 302 to a login page, never the server's. It records
each request's path, whether it was admitted, and which headers it carried.

    with header_proxy(server.url, {"X-Access-Id": "id", "X-Access-Secret": "s"}) as proxy:
        client = server.client(home, url=proxy.url, headers={...})
"""

from __future__ import annotations

import http.client
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import urlsplit

#: Hop-by-hop headers a proxy never forwards (RFC 9110 §7.6.1), plus the ones it
#: sets itself.
_HOP = {
    "connection",
    "keep-alive",
    "proxy-authenticate",
    "proxy-authorization",
    "te",
    "trailer",
    "transfer-encoding",
    "upgrade",
    "content-length",
    "host",
}


@dataclass
class HeaderProxy:
    url: str
    required: dict[str, str]
    requests: list[dict] = field(default_factory=list)
    lock: threading.Lock = field(default_factory=threading.Lock)

    @property
    def admitted(self) -> list[dict]:
        return [r for r in self.requests if r["admitted"]]

    @property
    def refused(self) -> list[dict]:
        return [r for r in self.requests if not r["admitted"]]


@contextmanager
def header_proxy(target: str, required: dict[str, str]) -> Iterator[HeaderProxy]:
    upstream = urlsplit(target)
    proxy = HeaderProxy(url="", required=dict(required))

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, format: str, *args: Any) -> None:
            pass

        def _handle(self) -> None:
            length = int(self.headers.get("Content-Length") or 0)
            body = self.rfile.read(length) if length else None
            admitted = all(self.headers.get(k) == v for k, v in proxy.required.items())
            with proxy.lock:
                proxy.requests.append(
                    {
                        "method": self.command,
                        "path": self.path,
                        "admitted": admitted,
                        "has_authorization": "Authorization" in self.headers,
                    }
                )
            if not admitted:
                page = b"<html><body>Sign in to continue</body></html>"
                self.send_response(302)
                self.send_header("Location", "https://login.example.invalid/sso")
                self.send_header("Content-Type", "text/html")
                self.send_header("Content-Length", str(len(page)))
                self.end_headers()
                self.wfile.write(page)
                return
            conn = http.client.HTTPConnection(upstream.hostname, upstream.port, timeout=120)
            try:
                headers = {
                    k: v
                    for k, v in self.headers.items()
                    if k.lower() not in _HOP and k not in proxy.required
                }
                conn.request(self.command, self.path, body=body, headers=headers)
                response = conn.getresponse()
                payload = response.read()
                self.send_response(response.status, response.reason)
                for key, value in response.getheaders():
                    if key.lower() not in _HOP:
                        self.send_header(key, value)
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)
            except OSError:
                self.send_error(502)
            finally:
                conn.close()

        do_GET = do_POST = _handle

    httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    httpd.daemon_threads = True
    thread = threading.Thread(target=httpd.serve_forever, args=(0.05,), daemon=True)
    thread.start()
    proxy.url = f"http://127.0.0.1:{httpd.server_address[1]}"
    try:
        yield proxy
    finally:
        httpd.shutdown()
        httpd.server_close()
        thread.join(timeout=5)
