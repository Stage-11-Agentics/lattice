"""Test proxies and listeners on ``127.0.0.1:0`` for the client transport tests.

- :func:`recording_listener`: answers everything with 200 and records each
  request's path and headers (the "second listener" of AC-20).
- :func:`fixed_answer`: answers every request with one canned response (a
  302 redirect, a proxy's HTML login page).
- :func:`tcp_proxy`: forwards TCP to a target, counting bytes each way, with an
  optional downstream throttle in bytes per second.
"""

from __future__ import annotations

import socket
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any


@dataclass
class Listener:
    url: str
    requests: list[dict] = field(default_factory=list)


@contextmanager
def _serve(handler: type[BaseHTTPRequestHandler]) -> Iterator[str]:
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    httpd.daemon_threads = True
    thread = threading.Thread(target=httpd.serve_forever, args=(0.02,), daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{httpd.server_address[1]}"
    finally:
        httpd.shutdown()
        httpd.server_close()
        thread.join(timeout=5)


@contextmanager
def recording_listener() -> Iterator[Listener]:
    listener = Listener(url="")

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, format: str, *args: Any) -> None:
            pass

        def _record(self) -> None:
            listener.requests.append(
                {"method": self.command, "path": self.path, "headers": dict(self.headers.items())}
            )
            body = b'{"ok": true, "data": {}}'
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Lattice-Protocol", "1")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        do_GET = do_POST = _record

    with _serve(Handler) as url:
        listener.url = url
        yield listener


@contextmanager
def fixed_answer(status: int, headers: dict[str, str], body: bytes = b"") -> Iterator[Listener]:
    listener = Listener(url="")

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, format: str, *args: Any) -> None:
            pass

        def _answer(self) -> None:
            listener.requests.append(
                {"method": self.command, "path": self.path, "headers": dict(self.headers.items())}
            )
            self.send_response(status)
            for name, value in headers.items():
                self.send_header(name, value)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        do_GET = do_POST = _answer

    with _serve(Handler) as url:
        listener.url = url
        yield listener


@dataclass
class TcpProxy:
    url: str
    downstream_bytes: int = 0  # server → client
    upstream_bytes: int = 0  # client → server
    lock: threading.Lock = field(default_factory=threading.Lock)

    def reset_counts(self) -> None:
        with self.lock:
            self.downstream_bytes = self.upstream_bytes = 0


@contextmanager
def tcp_proxy(target_port: int, *, bytes_per_second: float | None = None) -> Iterator[TcpProxy]:
    """Forward ``127.0.0.1:<new port>`` to ``127.0.0.1:target_port``."""
    listen = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listen.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    listen.bind(("127.0.0.1", 0))
    listen.listen(64)
    proxy = TcpProxy(url=f"http://127.0.0.1:{listen.getsockname()[1]}")
    stop = threading.Event()

    def pump(src: socket.socket, dst: socket.socket, downstream: bool) -> None:
        try:
            while not stop.is_set():
                chunk = src.recv(16384 if bytes_per_second else 65536)
                if not chunk:
                    break
                if downstream and bytes_per_second:
                    time.sleep(len(chunk) / bytes_per_second)
                dst.sendall(chunk)
                with proxy.lock:
                    if downstream:
                        proxy.downstream_bytes += len(chunk)
                    else:
                        proxy.upstream_bytes += len(chunk)
        except OSError:
            pass
        finally:
            for s in (src, dst):
                try:
                    s.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass

    def accept() -> None:
        listen.settimeout(0.2)
        while not stop.is_set():
            try:
                client, _ = listen.accept()
            except (TimeoutError, OSError):
                continue
            try:
                upstream = socket.create_connection(("127.0.0.1", target_port))
            except OSError:
                client.close()
                continue
            for args in ((client, upstream, False), (upstream, client, True)):
                threading.Thread(target=pump, args=args, daemon=True).start()

    thread = threading.Thread(target=accept, daemon=True)
    thread.start()
    try:
        yield proxy
    finally:
        stop.set()
        thread.join(timeout=5)
        listen.close()
