"""An in-process stub of the H-10a server, speaking exactly SPEC §8.8 and §8.9.

H-10c starts before H-10a lands, so its tests run against this stub rather
than the real server. It serves:

- ``GET /v1/info``: the envelope with ``stream_heartbeat_seconds``.
- ``GET /v1/projects/<slug>/sync?since=N&epoch=E&hash=H``: a delta of every
  path written after seq ``N`` (inline ``content_b64``), or ``reset: true``
  with every file when the epoch, seq, or line hash does not match.
- ``GET /v1/projects/<slug>/stream``: SSE. Subscribe, then replay after the
  ``Last-Event-ID`` (``<epoch>:<seq>:<line hash>``), or one ``reset`` when it
  does not match; then live ``journal`` entries (``id``, ``event: journal``,
  the entry plus ``events``), a ``heartbeat`` (``{epoch, head_seq}``, no id)
  at once and every ``heartbeat_seconds``, and ``reset`` on rotation.

Every response carries ``Lattice-Protocol: 1``. Also here: :class:`StubSyncer`
(a minimal cache syncer behind H-10b's pinned ``catch_up`` signature) and
:class:`TestProxy` (forwards everything, and refuses, blocks, buffers, or
filters the stream).
"""

from __future__ import annotations

import base64
import contextlib
import hashlib
import http.server
import json
import queue
import socket
import socketserver
import threading
import time
import urllib.error
import urllib.request
from collections.abc import Callable, Iterator
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlsplit

from lattice.core.errors import OpError
from lattice.remote.cache import SyncOutcome
from lattice.remote.http import Remote
from lattice.storage.fs import LATTICE_DIR, atomic_write, ensure_dir, unlink_path
from lattice.storage.ownership import syncing_board

SLUG = "demo"
TOKEN = "lt_test_secret_token"


def _line_hash(line: bytes) -> str:
    return hashlib.sha256(line.rstrip(b"\n")).hexdigest()[:32]


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


class _Server(socketserver.ThreadingMixIn, http.server.HTTPServer):
    daemon_threads = True
    allow_reuse_address = True
    block_on_close = False


class StubServer:
    """One project's journal, files, and stream subscribers, over real HTTP."""

    def __init__(self, heartbeat_seconds: float = 0.2) -> None:
        self.heartbeat_seconds = heartbeat_seconds
        self.epoch = "ep_1"
        self.files: dict[str, bytes] = {}
        self.journal: list[bytes] = []  # raw lines of the current epoch
        self.entries: list[dict[str, Any]] = []  # parsed, with "events"
        self.lock = threading.Lock()
        self.subscribers: list[queue.Queue] = []
        self.fail_sync = False
        #: (status, headers, body) answered to every sync instead, when set.
        self.sync_override: tuple[int, dict[str, str], bytes] | None = None
        self.sync_requests = 0
        self.resets_served = 0
        self.stream_requests: list[dict[str, str]] = []
        self._httpd: _Server | None = None
        self._stopping = threading.Event()

    # -- board state ----------------------------------------------------------

    @property
    def head_seq(self) -> int:
        return len(self.journal)

    def write(self, files: dict[str, bytes], events: list[dict] | None = None) -> int:
        """Commit one operation: update *files*, append a journal line, broadcast."""
        with self.lock:
            self.files.update(files)
            seq = len(self.journal) + 1
            entry = {
                "seq": seq,
                "ts": _now(),
                "op": "task.test",
                "paths": sorted(files),
                "event_ids": [e.get("id") for e in events or []],
            }
            raw = json.dumps(entry, sort_keys=True, separators=(",", ":")).encode()
            self.journal.append(raw)
            full = dict(entry, events=list(events or []))
            self.entries.append(full)
            message = ("journal", self._entry_id(seq), full)
            for sub in list(self.subscribers):
                sub.put(message)
        return seq

    def rotate_epoch(self, files: dict[str, bytes] | None = None) -> None:
        """Start a new epoch (files optionally replaced) and broadcast ``reset``."""
        with self.lock:
            if files is not None:
                self.files = dict(files)
            self.epoch = f"ep_{int(self.epoch.removeprefix('ep_')) + 1}"
            self.journal = []
            self.entries = []
            for sub in list(self.subscribers):
                sub.put(("reset", None, {"epoch": self.epoch}))

    def _entry_id(self, seq: int) -> str:
        return f"{self.epoch}:{seq}:{_line_hash(self.journal[seq - 1])}"

    def _hash_at(self, seq: int) -> str | None:
        return _line_hash(self.journal[seq - 1]) if 0 < seq <= len(self.journal) else None

    # -- lifecycle --------------------------------------------------------------

    @contextlib.contextmanager
    def running(self) -> Iterator[StubServer]:
        self._httpd = _Server(("127.0.0.1", 0), _make_handler(self))
        thread = threading.Thread(target=self._httpd.serve_forever, args=(0.01,), daemon=True)
        thread.start()
        try:
            yield self
        finally:
            self._stopping.set()
            with self.lock:
                for sub in self.subscribers:
                    sub.put(None)
            self._httpd.shutdown()
            self._httpd.server_close()

    @property
    def url(self) -> str:
        assert self._httpd is not None
        return f"http://127.0.0.1:{self._httpd.server_address[1]}"

    # -- endpoints -----------------------------------------------------------------

    def sync_body(self, query: dict[str, list[str]]) -> dict[str, Any]:
        since = int(query.get("since", ["0"])[0] or 0)
        epoch = query.get("epoch", [None])[0]
        client_hash = query.get("hash", [None])[0]
        with self.lock:
            head = self.head_seq
            matches = (
                epoch == self.epoch
                and since <= head
                and (since == 0 or client_hash == self._hash_at(since))
            )
            if matches:
                paths: set[str] = set()
                for entry in self.entries[since:]:
                    paths.update(entry["paths"])
                reset = False
            else:
                paths = set(self.files)
                reset = True
                self.resets_served += 1
            files = {
                p: {
                    "sha256": hashlib.sha256(self.files[p]).hexdigest(),
                    "size": len(self.files[p]),
                    "content_b64": base64.b64encode(self.files[p]).decode(),
                }
                for p in sorted(paths)
                if p in self.files
            }
            body: dict[str, Any] = {
                "epoch": self.epoch,
                "head_seq": head,
                "reset": reset,
                "files": files,
                "removed": [],
            }
            if head:
                body["head_hash"] = self._hash_at(head)
        return body

    def stream_resume(self, last_id: str | None) -> tuple[bool, list[tuple]]:
        """(reset?, replay messages) for a subscriber resuming after *last_id*."""
        if not last_id:
            return False, []
        parts = last_id.split(":")
        if len(parts) != 3 or not parts[1].isdigit():
            return True, []
        epoch, seq, digest = parts[0], int(parts[1]), parts[2]
        if epoch != self.epoch or seq > self.head_seq or self._hash_at(seq) != digest:
            return True, []
        return False, [("journal", self._entry_id(e["seq"]), e) for e in self.entries[seq:]]


def _make_handler(stub: StubServer) -> type[http.server.BaseHTTPRequestHandler]:
    class Handler(http.server.BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.0"

        def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
            pass

        def _json(self, status: int, body: Any) -> None:
            raw = json.dumps(body).encode()
            self.send_response(status)
            self.send_header("Lattice-Protocol", "1")
            self.send_header("Content-Type", "application/json")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

        def do_GET(self) -> None:  # noqa: N802
            parts = urlsplit(self.path)
            query = parse_qs(parts.query)
            if self.headers.get("Authorization") != f"Bearer {TOKEN}":
                self._json(
                    401, {"ok": False, "error": {"code": "UNAUTHENTICATED", "message": "no"}}
                )
                return
            if parts.path == "/v1/info":
                self._json(
                    200,
                    {"ok": True, "data": {"stream_heartbeat_seconds": stub.heartbeat_seconds}},
                )
            elif parts.path == f"/v1/projects/{SLUG}/sync":
                stub.sync_requests += 1
                if stub.sync_override is not None:
                    status, headers, raw = stub.sync_override
                    self.send_response(status)
                    for name, value in headers.items():
                        self.send_header(name, value)
                    self.send_header("Content-Length", str(len(raw)))
                    self.end_headers()
                    self.wfile.write(raw)
                    return
                if stub.fail_sync:
                    self._json(
                        500, {"ok": False, "error": {"code": "INTEGRITY_ERROR", "message": "x"}}
                    )
                    return
                self._json(200, {"ok": True, "data": stub.sync_body(query)})
            elif parts.path == f"/v1/projects/{SLUG}/stream":
                self._stream()
            else:
                self._json(404, {"ok": False, "error": {"code": "NOT_FOUND", "message": "no"}})

        def _send_event(self, event: str, data: Any, event_id: str | None) -> None:
            lines = []
            if event_id is not None:
                lines.append(f"id: {event_id}")
            lines.append(f"event: {event}")
            lines.append("data: " + json.dumps(data, sort_keys=True, separators=(",", ":")))
            self.wfile.write(("\n".join(lines) + "\n\n").encode())
            self.wfile.flush()

        def _stream(self) -> None:
            stub.stream_requests.append(dict(self.headers.items()))
            sub: queue.Queue = queue.Queue()
            with stub.lock:
                stub.subscribers.append(sub)  # subscribe first, then replay
                reset, replay = stub.stream_resume(self.headers.get("Last-Event-ID"))
                epoch, head = stub.epoch, stub.head_seq
            try:
                self.send_response(200)
                self.send_header("Lattice-Protocol", "1")
                self.send_header("Content-Type", "text/event-stream")
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                if reset:
                    self._send_event("reset", {"epoch": epoch}, None)
                sent = 0
                for _, event_id, entry in replay:
                    self._send_event("journal", entry, event_id)
                    sent = entry["seq"]
                self._send_event("heartbeat", {"epoch": epoch, "head_seq": head}, None)
                next_beat = time.monotonic() + stub.heartbeat_seconds
                while not stub._stopping.is_set():
                    try:
                        message = sub.get(timeout=max(0.0, next_beat - time.monotonic()))
                    except queue.Empty:
                        with stub.lock:
                            beat = {"epoch": stub.epoch, "head_seq": stub.head_seq}
                        self._send_event("heartbeat", beat, None)
                        next_beat = time.monotonic() + stub.heartbeat_seconds
                        continue
                    if message is None:
                        return
                    kind, event_id, data = message
                    if kind == "journal":
                        if data["seq"] <= sent:
                            continue  # already replayed
                        sent = data["seq"]
                    else:
                        sent = 0
                    self._send_event(kind, data, event_id)
            except OSError:
                return
            finally:
                with stub.lock:
                    if sub in stub.subscribers:
                        stub.subscribers.remove(sub)

    return Handler


# ---------------------------------------------------------------------------
# A stub cache syncer behind H-10b's pinned signature
# ---------------------------------------------------------------------------


class _NoRedirectHandler(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: ANN001, ANN201
        return None


_NO_REDIRECT = urllib.request.build_opener(_NoRedirectHandler)


#: H-10b's ``AVAILABILITY_CODES``: the only server codes a catch-up turns into
#: an outcome instead of raising.
AVAILABILITY_CODES = {
    "BOARD_BUSY": "busy",
    "RATE_LIMITED": "busy",
    "BOARD_UNAVAILABLE": "unreachable",
}


def _classify_sync(
    status: int, headers: Any, raw: bytes
) -> tuple[dict[str, Any] | None, tuple[str, str] | None]:
    """(sync body, None) or (None, (transient kind, detail)); raises ``OpError`` for
    a hard failure, exactly as H-10b's ``catch_up`` does: ``PROXY_REJECTED`` for a
    redirect or an answer that is not a Lattice envelope, ``PROTOCOL_MISMATCH``,
    the server's own code for every error that is not an availability code,
    whatever its HTTP status (a 500 ``INTEGRITY_ERROR`` included), and
    ``INTEGRITY_ERROR`` (``MALFORMED_SYNC``) for an unusable body. Only
    :data:`AVAILABILITY_CODES` become outcomes, as in H-10b (1d6a047)."""
    if 300 <= status < 400:
        raise OpError("PROXY_REJECTED", f"redirect (status {status})", {"status": status})
    protocol = headers.get("Lattice-Protocol")
    if protocol is None:
        raise OpError("PROXY_REJECTED", f"not a Lattice server (status {status})")
    if protocol != "1":
        raise OpError("PROTOCOL_MISMATCH", f"server speaks protocol {protocol}")
    content_type = (headers.get("Content-Type") or "").split(";")[0].strip()
    try:
        if content_type != "application/json":
            raise ValueError(content_type)
        envelope = json.loads(raw)
        ok = envelope["ok"]
    except (ValueError, KeyError, TypeError):
        raise OpError("PROXY_REJECTED", f"not a JSON envelope (status {status})") from None
    if not ok:
        error = envelope.get("error") or {}
        code, message = str(error.get("code")), str(error.get("message"))
        kind = AVAILABILITY_CODES.get(code)
        if kind is not None:
            return None, (kind, f"stub answered {code}")
        raise OpError(code, message, error.get("details"))
    body = envelope.get("data")
    if not (
        isinstance(body, dict)
        and isinstance(body.get("epoch"), str)
        and isinstance(body.get("head_seq"), int)
        and isinstance(body.get("files"), dict)
        and isinstance(body.get("reset"), bool)
    ):
        raise OpError("INTEGRITY_ERROR", "malformed sync", {"reason": "MALFORMED_SYNC"})
    return body, None


class StubSyncer:
    """``catch_up(hosted_root) -> SyncOutcome`` over the stub's sync endpoint.

    Minimal on purpose (no modes, no fingerprint), but it fails the way
    H-10b's ``catch_up`` does (see :func:`_classify_sync`): transient failures
    are outcomes, hard failures raise ``OpError``.
    """

    def __init__(self, url: str) -> None:
        self.url = url
        self.calls = 0
        self.resets = 0
        self.lock = threading.Lock()

    def state_path(self, root: Path) -> Path:
        return Path(root) / LATTICE_DIR / "cache" / "state.json"

    def state(self, root: Path) -> dict[str, Any]:
        try:
            return json.loads(self.state_path(root).read_text())
        except (OSError, ValueError):
            return {}

    def __call__(
        self,
        hosted_root: Path,
        *,
        bulk: bool = False,
        adopt: frozenset[str] | None = None,
        on_unreachable: Callable[[], None] | None = None,
    ) -> SyncOutcome:
        """``cache.catch_up``'s signature: *on_unreachable* runs when the sync
        ends ``unreachable`` (the real syncer runs it under its lock)."""
        outcome = self._sync(hosted_root)
        if outcome.kind == "unreachable" and on_unreachable is not None:
            on_unreachable()
        return outcome

    def _sync(self, hosted_root: Path) -> SyncOutcome:
        with self.lock:
            self.calls += 1
            root = Path(hosted_root)
            state = self.state(root)
            query = f"since={state.get('head_seq', 0)}"
            if state.get("epoch"):
                query += f"&epoch={state['epoch']}"
            if state.get("head_hash"):
                query += f"&hash={state['head_hash']}"
            req = urllib.request.Request(f"{self.url}/v1/projects/{SLUG}/sync?{query}")
            req.add_unredirected_header("Authorization", f"Bearer {TOKEN}")

            def outcome(kind: str, detail: str) -> SyncOutcome:
                return SyncOutcome(kind, state.get("head_seq"), state.get("synced_at"), detail)

            try:
                with _NO_REDIRECT.open(req, timeout=5) as resp:
                    status, headers, raw = resp.status, resp.headers, resp.read()
            except urllib.error.HTTPError as exc:
                status, headers, raw = exc.code, exc.headers, exc.read()
            except (urllib.error.URLError, OSError) as exc:
                return outcome("unreachable", f"cannot reach stub: {exc}")
            body, transient = _classify_sync(status, headers, raw)
            if transient is not None:
                return outcome(*transient)
            board = root / LATTICE_DIR
            with syncing_board(board):
                ensure_dir(board / "cache")
                if body["reset"]:
                    self.resets += 1
                    for existing in _board_files(board):
                        if existing not in body["files"]:
                            unlink_path(board / existing)
                for rel, meta in body["files"].items():
                    target = board / rel
                    ensure_dir(target.parent)
                    atomic_write(target, base64.b64decode(meta["content_b64"]))
                synced_at = _now()
                changed = (
                    bool(body["files"])
                    or body["reset"]
                    or (body["head_seq"] != state.get("head_seq"))
                )
                new_state = {
                    "remote": "stub",
                    "project": SLUG,
                    "epoch": body["epoch"],
                    "head_seq": body["head_seq"],
                    "head_hash": body.get("head_hash"),
                    "synced_at": synced_at,
                }
                atomic_write(self.state_path(root), json.dumps(new_state) + "\n")
            return SyncOutcome("applied" if changed else "unchanged", body["head_seq"], synced_at)


def _board_files(board: Path) -> list[str]:
    out = []
    for path in board.rglob("*"):
        rel = path.relative_to(board).as_posix()
        if path.name.startswith(".tmp.") or rel.startswith(("cache/", "locks/")):
            continue
        if path.is_file():
            out.append(rel)
    return out


def cache_files(root: Path) -> dict[str, bytes]:
    """The cache's board files; unlocked, so a file a sync replaces mid-read is skipped."""
    board = Path(root) / LATTICE_DIR
    out = {}
    for rel in _board_files(board):
        try:
            out[rel] = (board / rel).read_bytes()
        except FileNotFoundError:
            continue
    return out


def stub_remote(url: str, **headers: str) -> Remote:
    """H-10b's :class:`Remote` for the stub (alias ``stub``); the project is :data:`SLUG`."""
    return Remote(alias="stub", url=url, token=TOKEN, headers=headers)


# ---------------------------------------------------------------------------
# Test proxies
# ---------------------------------------------------------------------------


class TestProxy:
    """A reverse proxy in front of the stub that forwards every request, with the
    stream handled by *stream_mode*:

    - ``pass``: forward it untouched.
    - ``refuse``: answer 502 with an HTML page (no ``Lattice-Protocol``).
    - ``block``: accept the connection and close it without answering.
    - ``buffer``: accept it and hold every byte (a buffering proxy on an
      endless response): the client never sees even the status line.
    - ``drop_entries``: forward it, but drop every ``journal`` event and pass
      heartbeats and resets.
    """

    __test__ = False  # not a pytest class

    def __init__(self, upstream: str, stream_mode: str = "pass") -> None:
        self.upstream = upstream
        self.stream_mode = stream_mode
        self._httpd: _Server | None = None
        self._stopping = threading.Event()

    @property
    def url(self) -> str:
        assert self._httpd is not None
        return f"http://127.0.0.1:{self._httpd.server_address[1]}"

    @contextlib.contextmanager
    def running(self) -> Iterator[TestProxy]:
        self._httpd = _Server(("127.0.0.1", 0), _make_proxy_handler(self))
        thread = threading.Thread(target=self._httpd.serve_forever, args=(0.01,), daemon=True)
        thread.start()
        try:
            yield self
        finally:
            self._stopping.set()
            self._httpd.shutdown()
            self._httpd.server_close()


def _make_proxy_handler(proxy: TestProxy) -> type[http.server.BaseHTTPRequestHandler]:
    class Handler(http.server.BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.0"

        def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
            pass

        def _upstream(self, timeout: float) -> Any:
            req = urllib.request.Request(proxy.upstream + self.path)
            for name, value in self.headers.items():
                if name.lower() not in {"host", "connection"}:
                    req.add_unredirected_header(name, value)
            try:
                return urllib.request.urlopen(req, timeout=timeout)  # noqa: S310
            except urllib.error.HTTPError as exc:
                return exc

        def _copy_head(self, resp: Any) -> None:
            self.send_response(resp.status if hasattr(resp, "status") else resp.code)
            for name, value in resp.headers.items():
                if name.lower() not in {"connection", "transfer-encoding", "date", "server"}:
                    self.send_header(name, value)
            self.end_headers()

        def do_GET(self) -> None:  # noqa: N802
            if urlsplit(self.path).path.endswith("/stream"):
                self._stream()
                return
            resp = self._upstream(timeout=10)
            body = resp.read()
            self._copy_head(resp)
            self.wfile.write(body)

        def _stream(self) -> None:
            mode = proxy.stream_mode
            if mode == "refuse":
                page = b"<html><body>Bad gateway</body></html>"
                self.send_response(502)
                self.send_header("Content-Type", "text/html")
                self.send_header("Content-Length", str(len(page)))
                self.end_headers()
                self.wfile.write(page)
                return
            if mode == "block":
                with contextlib.suppress(OSError):
                    self.connection.shutdown(socket.SHUT_RDWR)
                return
            if mode == "buffer":
                resp = self._upstream(timeout=60)
                with contextlib.suppress(OSError, ValueError):
                    while not proxy._stopping.is_set() and resp.readline():
                        pass  # held, never forwarded
                return
            resp = self._upstream(timeout=60)
            try:
                self._copy_head(resp)
                block: list[bytes] = []
                while not proxy._stopping.is_set():
                    line = resp.readline()
                    if not line:
                        return
                    block.append(line)
                    if line.strip():
                        continue
                    if mode == "drop_entries" and b"event: journal\n" in block:
                        block = []
                        continue
                    self.wfile.write(b"".join(block))
                    self.wfile.flush()
                    block = []
            except (OSError, ValueError):
                return

    return Handler


class RecordingListener:
    """A second listener that records every request's headers (AC-20)."""

    def __init__(self) -> None:
        self.requests: list[dict[str, str]] = []
        self._httpd: _Server | None = None

    @property
    def url(self) -> str:
        assert self._httpd is not None
        return f"http://127.0.0.1:{self._httpd.server_address[1]}"

    @contextlib.contextmanager
    def running(self) -> Iterator[RecordingListener]:
        listener = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
                pass

            def do_GET(self) -> None:  # noqa: N802
                listener.requests.append(dict(self.headers.items()))
                self.send_response(200)
                self.send_header("Content-Type", "text/html")
                self.send_header("Content-Length", "0")
                self.end_headers()

        self._httpd = _Server(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=self._httpd.serve_forever, args=(0.01,), daemon=True)
        thread.start()
        try:
            yield self
        finally:
            self._httpd.shutdown()
            self._httpd.server_close()


class OneShotServer:
    """Answers every request with one fixed response (redirect or HTML page)."""

    def __init__(self, status: int, headers: dict[str, str], body: bytes = b"") -> None:
        self.status, self.headers, self.body = status, headers, body
        self.requests: list[dict[str, str]] = []
        self._httpd: _Server | None = None

    @property
    def url(self) -> str:
        assert self._httpd is not None
        return f"http://127.0.0.1:{self._httpd.server_address[1]}"

    @contextlib.contextmanager
    def running(self) -> Iterator[OneShotServer]:
        server = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
                pass

            def do_GET(self) -> None:  # noqa: N802
                server.requests.append(dict(self.headers.items()))
                self.send_response(server.status)
                for name, value in server.headers.items():
                    self.send_header(name, value)
                self.send_header("Content-Length", str(len(server.body)))
                self.end_headers()
                self.wfile.write(server.body)

        self._httpd = _Server(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=self._httpd.serve_forever, args=(0.01,), daemon=True)
        thread.start()
        try:
            yield self
        finally:
            self._httpd.shutdown()
            self._httpd.server_close()


def wait_for(predicate: Any, timeout: float, interval: float = 0.02) -> float | None:
    """Seconds until *predicate()* was true, or ``None`` if it never was in *timeout*."""
    start = time.monotonic()
    while time.monotonic() - start < timeout:
        if predicate():
            return time.monotonic() - start
        time.sleep(interval)
    return None
