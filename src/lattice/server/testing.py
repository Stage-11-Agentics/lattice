"""Test helpers: a real server on ``127.0.0.1:0`` in this process (EVALUATION §1).

Later tickets' tests use these to talk to a server over real HTTP::

    from lattice.server.testing import make_root, running_server

    root = make_root(tmp_path, projects={"demo": {"code": "DEM"}})
    token = add_token(root, user="human:alice", projects=["demo"])
    with running_server(root) as server:
        urllib.request.urlopen(server.url + "/healthz")

``running_server`` starts uvicorn in a background thread (its own event loop),
waits until it listens, and stops it on exit. It holds ``server.lock`` like
``lattice server serve``. Pass ``config`` overrides as a dict merged into
``server.json`` (for example ``{"limits": {"max_body_bytes": 1024}}``).
"""

from __future__ import annotations

import json
import os
import socket
import threading
import time
import urllib.error
import urllib.request
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from io import StringIO
from pathlib import Path
from typing import Any

from lattice.server import admin
from lattice.server.config import SERVER_JSON
from lattice.server.log import ServerLog


@dataclass
class ServerHandle:
    url: str
    root: Path
    port: int
    log_stream: StringIO
    app: Any = None
    _server: Any = field(default=None, repr=False)

    @property
    def state(self) -> Any:
        """The app's :class:`lattice.server.app.ServerState`."""
        return self.app.state

    def project(self, slug: str) -> Any:
        return self.state.registry.get(slug)

    @property
    def log_lines(self) -> list[dict]:
        return [json.loads(line) for line in self.log_stream.getvalue().splitlines() if line]

    def request(
        self,
        method: str,
        path: str,
        *,
        token: str | None = None,
        body: Any = None,
        headers: dict[str, str] | None = None,
        timeout: float = 30.0,
    ) -> tuple[int, dict[str, str], Any]:
        """One request; returns ``(status, headers, parsed JSON body or raw text)``."""
        return http_request(
            method,
            self.url + path,
            token=token,
            body=body,
            headers=headers,
            timeout=timeout,
        )

    def op(
        self,
        slug: str,
        op: str,
        params: dict | None = None,
        *,
        token: str | None,
        **envelope: Any,
    ) -> tuple[int, dict[str, str], Any]:
        """``POST /v1/projects/<slug>/ops/<op>`` with ``{"params": ..., **envelope}``."""
        body = {"params": params or {}, **envelope}
        return self.request("POST", f"/v1/projects/{slug}/ops/{op}", token=token, body=body)


def http_request(
    method: str,
    url: str,
    *,
    token: str | None = None,
    body: Any = None,
    headers: dict[str, str] | None = None,
    timeout: float = 30.0,
) -> tuple[int, dict[str, str], Any]:
    data = None
    all_headers = dict(headers or {})
    if body is not None:
        data = body if isinstance(body, bytes) else json.dumps(body).encode("utf-8")
        all_headers.setdefault("Content-Type", "application/json")
    if token is not None:
        all_headers["Authorization"] = f"Bearer {token}"
    request = urllib.request.Request(url, data=data, method=method, headers=all_headers)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            status, raw, resp_headers = response.status, response.read(), response.headers
    except urllib.error.HTTPError as exc:
        status, raw, resp_headers = exc.code, exc.read(), exc.headers
    text = raw.decode("utf-8", errors="replace")
    try:
        parsed: Any = json.loads(text)
    except ValueError:
        parsed = text
    return status, {k.lower(): v for k, v in resp_headers.items()}, parsed


def make_root(
    base: Path,
    *,
    projects: dict[str, dict] | None = None,
    config: dict | None = None,
) -> Path:
    """A server root under *base* with ``server init`` done and *projects* created.

    ``projects`` maps slug to ``create_project`` keyword arguments.
    """
    root = Path(base) / "server-root"
    admin.init_root(root)
    if config:
        write_config(root, config)
    for slug, options in (projects or {}).items():
        admin.create_project(root, slug, **options)
    return root


def write_config(root: Path, overrides: dict) -> None:
    """Merge *overrides* (nested dicts merged one level deep) into ``server.json``."""
    path = Path(root) / SERVER_JSON
    current = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
    for key, value in overrides.items():
        if isinstance(value, dict) and isinstance(current.get(key), dict):
            current[key] = {**current[key], **value}
        else:
            current[key] = value
    path.write_text(json.dumps(current, indent=2) + "\n", encoding="utf-8")


@contextmanager
def running_server(
    root: Path,
    *,
    config: dict | None = None,
    log_level: str = "debug",
    startup_timeout: float = 10.0,
    wait_prewarm: bool = True,
    control_poll_seconds: float = 0.05,
) -> Iterator[ServerHandle]:
    """Serve *root* on ``127.0.0.1`` in a background thread until the block exits.

    By default it returns once the startup prewarm has loaded every project, so
    a test's first request never races a project load; pass
    ``wait_prewarm=False`` to observe the prewarm itself. Control requests are
    polled every *control_poll_seconds* (the server's own period is 2 s).
    """
    import uvicorn

    from lattice.server.app import create_app
    from lattice.server.serve import acquire_server_lock, load_server_config

    root = Path(root)
    if config:
        write_config(root, config)
    server_config = load_server_config(root)
    stream = StringIO()
    log = ServerLog(log_level, stream)
    fd = acquire_server_lock(root)
    app = create_app(root, config=server_config, log=log)
    app.state.registry.control_poll_seconds = control_poll_seconds
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    uv_config = uvicorn.Config(
        app,
        log_config=None,
        access_log=False,
        lifespan="on",
        timeout_graceful_shutdown=5,
        server_header=False,
    )
    server = uvicorn.Server(uv_config)
    thread = threading.Thread(
        target=server.run, kwargs={"sockets": [sock]}, name=f"lattice-server-{port}", daemon=True
    )
    thread.start()
    deadline = time.monotonic() + startup_timeout
    while not server.started:
        if not thread.is_alive() or time.monotonic() > deadline:
            server.should_exit = True
            os.close(fd)
            raise RuntimeError("test server failed to start")
        time.sleep(0.01)
    handle = ServerHandle(
        url=f"http://127.0.0.1:{port}", root=root, port=port, log_stream=stream, app=app
    )
    handle._server = server
    if wait_prewarm and not wait_for(handle.state.registry.prewarm_done.is_set, startup_timeout):
        server.should_exit = True
        thread.join(timeout=10)
        os.close(fd)
        raise RuntimeError("test server did not finish its prewarm")
    try:
        yield handle
    finally:
        server.should_exit = True
        thread.join(timeout=10)
        sock.close()
        os.close(fd)


def wait_for(predicate: Any, timeout: float = 5.0, interval: float = 0.02) -> bool:
    """Poll *predicate* until true or *timeout*; returns its last value."""
    deadline = time.monotonic() + timeout
    while True:
        value = predicate()
        if value or time.monotonic() > deadline:
            return value
        time.sleep(interval)
