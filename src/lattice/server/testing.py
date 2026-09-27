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

import base64
import http.client
import json
import os
import shutil
import socket
import urllib.parse
import threading
import time
import urllib.error
import urllib.request
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from io import StringIO
from pathlib import Path
from typing import TYPE_CHECKING, Any

from lattice.server import admin
from lattice.server.config import SERVER_JSON
from lattice.server.log import ServerLog

if TYPE_CHECKING:
    from lattice.server.project import Project


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

    def run_on_loop(self, coro: Any) -> Any:
        """Schedule *coro* on the server's event loop; returns a ``concurrent.futures.Future``."""
        import asyncio

        return asyncio.run_coroutine_threadsafe(coro, self.state.loop)

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
    control_poll_seconds: float = 0.01,
    heartbeat_seconds: float | None = None,
) -> Iterator[ServerHandle]:
    """Serve *root* on ``127.0.0.1`` in a background thread until the block exits.

    By default it returns once the startup prewarm has loaded every project, so
    a test's first request never races a project load; pass
    ``wait_prewarm=False`` to observe the prewarm itself. Control requests are
    polled every *control_poll_seconds* (the server's own period is 2 s).
    *heartbeat_seconds* overrides the stream heartbeat (``server.json`` allows
    whole seconds only), and ``/v1/info`` reports the override.
    """
    import uvicorn

    from lattice.server.app import create_app
    from lattice.server.serve import acquire_server_lock, load_server_config, uvicorn_options

    root = Path(root)
    if config:
        write_config(root, config)
    server_config = load_server_config(root)
    stream = StringIO()
    log = ServerLog(log_level, stream)
    fd = acquire_server_lock(root)
    app = create_app(root, config=server_config, log=log)
    app.state.registry.control_poll_seconds = control_poll_seconds
    if heartbeat_seconds is not None:
        app.state.heartbeat_seconds = heartbeat_seconds
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
        **uvicorn_options(server_config),
    )
    server = _test_server(uv_config)
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
        handle.state.registry.close_all_streams()
        server.should_exit = True
        thread.join(timeout=10)
        os.close(fd)
        raise RuntimeError("test server did not finish its prewarm")
    try:
        yield handle
    finally:
        handle.state.registry.close_all_streams()
        server.should_exit = True
        thread.join(timeout=10)
        sock.close()
        os.close(fd)


def _test_server(config: Any) -> Any:
    """A ``uvicorn.Server`` that stops in milliseconds.

    uvicorn checks ``should_exit`` every 0.1 s, then sleeps a fixed 0.1 s in
    ``shutdown`` and 0.1 s per poll while connections and tasks drain: about
    0.2 s per stop. The default suite starts some 400 test servers, so those
    sleeps were about 75 s of its serial time (G-9). Same steps, 10 ms polls.
    """
    import asyncio

    import uvicorn

    class TestServer(uvicorn.Server):
        async def main_loop(self) -> None:
            counter = 0
            should_exit = await self.on_tick(counter)
            while not should_exit:
                for _ in range(10):  # uvicorn's 0.1 s tick, checking every 10 ms
                    if self.should_exit:
                        return
                    await asyncio.sleep(0.01)
                counter = (counter + 1) % 864000
                should_exit = await self.on_tick(counter)

        async def shutdown(self, sockets: list[socket.socket] | None = None) -> None:
            for listener in self.servers:
                listener.close()
            for sock in sockets or []:
                sock.close()
            state = self.server_state
            for connection in list(state.connections):
                connection.shutdown()
            grace = self.config.timeout_graceful_shutdown  # None: wait as long as it takes
            deadline = None if grace is None else time.monotonic() + grace
            await asyncio.sleep(0.01)
            while (state.connections or state.tasks) and not self.force_exit:
                if deadline is not None and time.monotonic() > deadline:
                    for task in state.tasks:
                        task.cancel(msg="Task cancelled, timeout graceful shutdown exceeded")
                    break
                await asyncio.sleep(0.01)
            if not self.force_exit:
                await self.lifespan.shutdown()

    return TestServer(config)


def wait_for(predicate: Any, timeout: float = 5.0, interval: float = 0.02) -> bool:
    """Poll *predicate* until true or *timeout*; returns its last value."""
    deadline = time.monotonic() + timeout
    while True:
        value = predicate()
        if value or time.monotonic() > deadline:
            return value
        time.sleep(interval)


# ---------------------------------------------------------------------------
# A served fixture board (the interface H-10b and H-10c test against)
# ---------------------------------------------------------------------------

#: What is never copied from a source board: runtime state and server or cache control.
_NOT_COPIED = ("hosted", "cache", "locks", "review_state", "tmp-prompts", ".daemon")


@dataclass
class SSEMessage:
    event: str
    data: Any
    id: str | None


class SSEReader:
    """A blocking reader of one ``GET .../stream`` response (tests only)."""

    def __init__(self, conn: http.client.HTTPConnection, response: http.client.HTTPResponse):
        self.conn = conn
        self.response = response
        self.status = response.status
        self.headers = {k.lower(): v for k, v in response.getheaders()}

    def next(self, timeout: float = 5.0) -> SSEMessage | None:
        """The next event, or ``None`` when the server closed the stream. Raises
        ``TimeoutError`` when nothing arrives within *timeout*."""
        event, data, event_id = "message", [], None
        if self.conn.sock is not None:
            self.conn.sock.settimeout(timeout)
        while True:
            try:
                raw = self.response.readline()
            except (TimeoutError, socket.timeout):
                raise TimeoutError("no stream event arrived") from None
            except (OSError, http.client.HTTPException, ValueError):
                return None
            if not raw:
                return None
            line = raw.decode("utf-8").rstrip("\r\n")
            if line == "":
                if data:
                    text = "\n".join(data)
                    try:
                        parsed: Any = json.loads(text)
                    except ValueError:
                        parsed = text
                    return SSEMessage(event, parsed, event_id)
                event, data, event_id = "message", [], None
                continue
            if line.startswith(":"):
                continue
            name, _, value = line.partition(":")
            value = value[1:] if value.startswith(" ") else value
            if name == "event":
                event = value
            elif name == "data":
                data.append(value)
            elif name == "id":
                event_id = value

    def next_of(self, kind: str, timeout: float = 5.0) -> SSEMessage:
        """The next event of *kind*, skipping others (heartbeats, usually)."""
        deadline = time.monotonic() + timeout
        while True:
            message = self.next(max(0.01, deadline - time.monotonic()))
            if message is None:
                raise EOFError(f"the stream closed before a {kind!r} event")
            if message.event == kind:
                return message

    def close(self) -> None:
        try:
            if self.conn.sock is not None:
                self.conn.sock.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        self.conn.close()


def open_stream(
    url: str,
    slug: str,
    token: str | None,
    *,
    last_event_id: str | None = None,
    query: dict[str, Any] | None = None,
    timeout: float = 10.0,
    headers: dict[str, str] | None = None,
) -> SSEReader:
    """Open ``GET <url>/v1/projects/<slug>/stream`` and return its reader (any status)."""
    parts = urllib.parse.urlsplit(url)
    conn = http.client.HTTPConnection(parts.hostname, parts.port, timeout=timeout)
    path = f"/v1/projects/{slug}/stream"
    if query:
        path += "?" + urllib.parse.urlencode({k: v for k, v in query.items() if v is not None})
    headers = {"Accept": "text/event-stream", **(headers or {})}
    if token is not None:
        headers["Authorization"] = f"Bearer {token}"
    if last_event_id is not None:
        headers["Last-Event-ID"] = last_event_id
    conn.request("GET", path, headers=headers)
    return SSEReader(conn, conn.getresponse())


@dataclass
class BoardServer:
    """One project on a running in-process server, with a bearer token for it."""

    handle: ServerHandle
    slug: str
    token: str
    token_id: str
    user: str

    @property
    def url(self) -> str:
        return self.handle.url

    @property
    def root(self) -> Path:
        return self.handle.root

    @property
    def board(self) -> Path:
        """The served ``.lattice/`` directory."""
        return self.handle.root / "projects" / self.slug / ".lattice"

    @property
    def project(self) -> Project:
        return self.handle.project(self.slug)

    def op(self, op: str, params: dict | None = None, **envelope: Any) -> dict:
        """Run *op* as the token's user (unless ``actor`` is given); returns the
        ``OpResult`` JSON. Raises ``AssertionError`` on any non-200 answer."""
        envelope.setdefault("actor", self.user)
        status, _, body = self.handle.op(self.slug, op, params, token=self.token, **envelope)
        if status != 200:
            raise AssertionError(f"{op} failed with {status}: {body}")
        return body["data"]["result"]

    def sync(
        self,
        *,
        since: int = 0,
        epoch: str | None = None,
        hash: str | None = None,
        manifest: bool = False,
    ) -> dict:
        """``GET .../sync``; returns the envelope's ``data`` (raises on an error)."""
        query = {
            "since": since,
            "epoch": epoch,
            "hash": hash,
            "manifest": "1" if manifest else None,
        }
        qs = urllib.parse.urlencode({k: v for k, v in query.items() if v is not None})
        status, _, body = self.handle.request(
            "GET", f"/v1/projects/{self.slug}/sync?{qs}", token=self.token
        )
        if status != 200:
            raise AssertionError(f"sync failed with {status}: {body}")
        return body["data"]

    def file(self, path: str, sha256: str | None = None) -> tuple[int, bytes]:
        """``GET .../files/<path>``: ``(status, raw body)``."""
        url = f"{self.url}/v1/projects/{self.slug}/files/{urllib.parse.quote(path)}"
        if sha256 is not None:
            url += f"?sha256={sha256}"
        request = urllib.request.Request(url, headers={"Authorization": f"Bearer {self.token}"})
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                return response.status, response.read()
        except urllib.error.HTTPError as exc:
            return exc.code, exc.read()

    def file_href(self, href: str) -> tuple[int, bytes]:
        """Follow a sync answer's ``href`` (relative to the server) with this token."""
        assert href.startswith("/v1/"), href
        request = urllib.request.Request(
            self.url + href, headers={"Authorization": f"Bearer {self.token}"}
        )
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                return response.status, response.read()
        except urllib.error.HTTPError as exc:
            return exc.code, exc.read()

    def rotate_epoch(self) -> dict:
        """``lattice server project rotate-epoch`` against this running server."""
        return admin.rotate_project_epoch(self.root, self.slug)

    @contextmanager
    def stream(self, last_event_id: str | None = None, **query: Any) -> Iterator[SSEReader]:
        reader = open_stream(
            self.url, self.slug, self.token, last_event_id=last_event_id, query=query or None
        )
        try:
            yield reader
        finally:
            reader.close()


def apply_sync(board: Path, body: dict, fetch: Any = None) -> None:
    """Apply a sync body to a plain directory *board* (a test mirror, not the cache).

    ``fetch(href) -> bytes`` resolves ``href`` entries; a reset replaces the tree.
    """
    board = Path(board)
    if body["reset"] and board.exists():
        shutil.rmtree(board)
    board.mkdir(parents=True, exist_ok=True)
    for rel, spec in body["files"].items():
        target = board / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        if (
            "append_from" in spec
            and target.exists()
            and target.stat().st_size == spec["append_from"]
        ):
            data = target.read_bytes() + base64.b64decode(spec["content_b64"])
        elif "content_b64" in spec and "append_from" not in spec:
            data = base64.b64decode(spec["content_b64"])
        else:
            assert fetch is not None, f"{rel} needs a fetch of {spec['href']}"
            data = fetch(spec["href"])
        target.write_bytes(data)
    for rel in body["removed"]:
        (board / rel).unlink(missing_ok=True)


@contextmanager
def serve_board(
    base: Path,
    *,
    slug: str = "demo",
    code: str | None = "DEM",
    source: Path | None = None,
    user: str = "human:alice",
    machine: str = "test",
    config: dict | None = None,
    heartbeat_seconds: float | None = None,
    audit: bool = True,
    **server_options: Any,
) -> Iterator[BoardServer]:
    """A server root under *base* with one project, a token for it, and a running
    in-process server on ``127.0.0.1:0``.

    The project is new (``project create`` with *code*), or a copy of the local
    board *source* (a directory holding ``.lattice/``, or the ``.lattice/``
    itself) placed with a fresh journal whose baseline is its current logs and,
    as ``project import`` does, its audit repository. ``audit=False`` turns audit
    off in ``server.json`` (no repository, no committer), for tests it is not the
    subject of.
    """
    from lattice.server import tokens

    root = Path(base) / "server-root"
    admin.init_root(root)
    if not audit:
        config = {**(config or {}), "audit": {"enabled": False}}
    if config:
        write_config(root, config)
    if source is None:
        admin.create_project(root, slug, code=code)
    else:
        _place_board(root, slug, Path(source))
    minted = tokens.create_token(root, user=user, machine=machine, projects=[slug])
    token_id = minted["record"]["id"]
    with running_server(root, heartbeat_seconds=heartbeat_seconds, **server_options) as handle:
        yield BoardServer(handle, slug, minted["token"], token_id, user)


def _place_board(root: Path, slug: str, source: Path) -> None:
    from lattice.server.config import load_config
    from lattice.server.control import CONTROL_DIR
    from lattice.server.journal import HOSTED_DIR, Journal
    from lattice.storage.fs import ensure_dir
    from lattice.storage.ownership import owning_board, release_owner_flock, try_owner_flock

    admin.check_slug(slug)
    lattice_dir = source if source.name == ".lattice" else source / ".lattice"
    if not (lattice_dir / "config.json").is_file():
        raise ValueError(f"{source} holds no Lattice board")
    board = admin.project_dir(root, slug) / ".lattice"
    shutil.copytree(
        lattice_dir,
        board,
        copy_function=shutil.copy2,
        ignore=lambda d, names: [n for n in names if Path(d) == lattice_dir and n in _NOT_COPIED],
    )
    with owning_board(board):
        ensure_dir(board / HOSTED_DIR)
        fd = try_owner_flock(board)
        assert fd is not None
        try:
            journal = Journal.create(board)
            ensure_dir(board / HOSTED_DIR / CONTROL_DIR)
            admin._write_owner_marker(board, "lattice-server-test")
        finally:
            release_owner_flock(fd)
    # The audit repository, as ``project create`` and ``project import`` make it.
    # Left to the first load, its one-time init (seconds for an envelope-size
    # board) would run inside the server's startup prewarm and its bounded wait.
    admin._create_audit_repo(
        admin.project_dir(root, slug), load_config(root).audit, epoch=journal.epoch
    )
