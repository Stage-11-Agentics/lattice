"""STUB sync server: speaks the wire format of SPEC §8.8, nothing more.

Not the real server. H-10b was built before H-10a (server sync and stream), so
its tests needed something on ``127.0.0.1`` that answers ``sync`` and
``files`` exactly as SPEC §8.8 pins them. Tests the real server can drive use
H-10a's helper; this stub remains for faults the real server cannot produce
on demand (unsafe paths, cross-origin ``href``, corrupted hashes, a response
held mid-flight).

What it models:

- a plain local board directory (``StubServer.board``) and an in-memory
  journal: ``{seq, ts, op, paths, lengths}`` lines, each hashed as the first
  32 hex characters of SHA-256 over the line's bytes; a ``baseline`` of every
  log's length when the epoch began; the per-log length history;
- ``GET /v1/projects/<slug>/sync?since=&epoch=&hash=[&manifest=1]`` and
  ``GET /v1/projects/<slug>/files/<path>?sha256=`` (412 ``STALE_VERSION``);
- ``Lattice-Protocol``, ``Lattice-Server-Version``, ``Cache-Control: no-store``,
  and the CLI envelope on every answer; bearer-token auth.

Writes go through :meth:`StubServer.commit` (raw file changes) or
:meth:`StubServer.op` (a real ``lattice.ops`` operation on the stub's board,
journaled from its write recorder).
"""

from __future__ import annotations

import base64
import copy
import hashlib
import json
import os
import shutil
import threading
import time
import urllib.parse
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

from ulid import ULID

from lattice.storage.ownership import PathClass, classify_path

RESET_INLINE_CAP = 32 * 1024 * 1024
STUB_VERSION = "2.0.0.dev0+stub"


def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def line_hash(line: bytes) -> str:
    return hashlib.sha256(line).hexdigest()[:32]


def durable_files(lattice_dir: Path) -> dict[str, Path]:
    """Every durable or workspace file under *lattice_dir*, by relative POSIX path."""
    found: dict[str, Path] = {}
    for dirpath, dirnames, filenames in os.walk(lattice_dir):
        rel_dir = Path(dirpath).relative_to(lattice_dir)
        dirnames[:] = [
            d
            for d in dirnames
            if classify_path(rel_dir / d) in (PathClass.DURABLE, PathClass.WORKSPACE)
        ]
        for name in filenames:
            rel = (rel_dir / name).as_posix()
            if classify_path(rel) in (PathClass.DURABLE, PathClass.WORKSPACE):
                found[rel] = Path(dirpath) / name
    return found


@dataclass
class Fault:
    """One-shot or persistent tampering with the next answers (test knobs)."""

    #: Called with the parsed sync body before it is sent; may mutate it.
    mutate_sync: Callable[[dict], None] | None = None
    #: Called with (path, bytes) before a file is served; returns the bytes to send.
    mutate_file: Callable[[str, bytes], bytes] | None = None
    #: Answer every request with this (status, headers, body) instead.
    raw: tuple[int, dict[str, str], bytes] | None = None
    #: Answer every files request with this (status, headers, body) instead.
    raw_files: tuple[int, dict[str, str], bytes] | None = None
    #: Answer every ``manifest=1`` sync with this (status, headers, body) instead.
    raw_manifest: tuple[int, dict[str, str], bytes] | None = None
    #: Held (not set) → sync answers wait on it after assembly.
    sync_gate: threading.Event | None = None
    #: Held (not set) → file answers wait on it.
    files_gate: threading.Event | None = None


@dataclass
class StubServer:
    board_root: Path  # the project directory; the board is board_root/.lattice
    slug: str = "demo"
    token: str = "stub-token"
    inline_file_bytes: int = 1024 * 1024
    url: str = ""
    epoch: str = field(default_factory=lambda: f"ep_{ULID()}")
    fault: Fault = field(default_factory=Fault)
    requests: list[dict] = field(default_factory=list)
    lock: threading.RLock = field(default_factory=threading.RLock)
    # journal state
    lines: list[bytes] = field(default_factory=list)
    entries: list[dict] = field(default_factory=list)
    baseline: dict[str, int] = field(default_factory=dict)
    history: dict[str, list[tuple[int, int]]] = field(default_factory=dict)
    #: Every request reaching a handler, in order: (kind, query) where kind is sync|files.
    arrivals: list[tuple[str, dict]] = field(default_factory=list)
    #: Every file answer: (path, HTTP status).
    file_statuses: list[tuple[str, int]] = field(default_factory=list)

    @property
    def board(self) -> Path:
        return self.board_root / ".lattice"

    @property
    def head(self) -> int:
        return len(self.entries)

    def head_hash(self) -> str | None:
        return line_hash(self.lines[-1]) if self.lines else None

    # -- writes ---------------------------------------------------------------

    def start_epoch(self) -> None:
        """Rotate: a new epoch, an empty journal, a baseline of today's log lengths."""
        with self.lock:
            self.epoch = f"ep_{ULID()}"
            self.lines, self.entries, self.history = [], [], {}
            self.baseline = {
                rel: path.stat().st_size
                for rel, path in durable_files(self.board).items()
                if rel.endswith(".jsonl")
            }

    def _journal(self, op: str, paths: list[str], lengths: dict[str, int]) -> int:
        seq = self.head + 1
        entry = {
            "seq": seq,
            "ts": time.strftime("%Y-%m-%dT%H:%M:%S.000Z", time.gmtime()),
            "op": op,
            "op_id": f"op_{ULID()}",
            "fp": "0" * 32,
            "token_id": "tok_stub",
            "task_id": None,
            "event_ids": [],
            "paths": sorted(paths),
            "lengths": lengths,
        }
        line = json.dumps(entry, sort_keys=True, separators=(",", ":")).encode()
        self.entries.append(entry)
        self.lines.append(line)
        for rel, length in lengths.items():
            self.history.setdefault(rel, []).append((seq, length))
        return seq

    def commit(
        self,
        *,
        write: dict[str, bytes] | None = None,
        append: dict[str, bytes] | None = None,
        remove: list[str] | tuple[str, ...] = (),
        op: str = "stub.commit",
    ) -> int:
        """Change board files and journal one entry; returns its seq."""
        with self.lock:
            paths: list[str] = []
            lengths: dict[str, int] = {}
            for rel, data in (write or {}).items():
                target = self.board / rel
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(data)
                paths.append(rel)
            for rel, data in (append or {}).items():
                target = self.board / rel
                target.parent.mkdir(parents=True, exist_ok=True)
                with open(target, "ab") as fh:
                    fh.write(data)
                paths.append(rel)
                lengths[rel] = target.stat().st_size
            for rel in remove:
                (self.board / rel).unlink(missing_ok=True)
                paths.append(rel)
            return self._journal(op, paths, lengths)

    def op(self, op_name: str, params: dict, *, actor: str = "human:stub") -> Any:
        """Run a real operation on the stub's board and journal it."""
        from lattice.ops import Caller, execute

        kinds: dict[Path, set[str]] = {}

        def before(path: Path, kind: str) -> None:
            kinds.setdefault(path, set()).add(kind)

        with self.lock:
            result = execute(
                self.board,
                op_name,
                params,
                Caller(actor=actor, origin={"op_id": f"op_{ULID()}"}),
                run_hooks=False,
                on_mutation=before,
            )
            board = self.board.resolve()
            lengths = {
                p.relative_to(board).as_posix(): p.stat().st_size
                for p, k in kinds.items()
                if "append" in k and p.exists()
            }
            files = [p for p in result.paths if not (self.board / p).is_dir()]
            self._journal(op_name, files, lengths)
            return result

    # -- snapshots (restore divergence) ---------------------------------------

    def snapshot(self, dest: Path) -> dict:
        with self.lock:
            shutil.copytree(self.board, dest, copy_function=shutil.copy2)
            return {
                "dest": dest,
                "epoch": self.epoch,
                "lines": list(self.lines),
                "entries": copy.deepcopy(self.entries),
                "baseline": dict(self.baseline),
                "history": copy.deepcopy(self.history),
            }

    def restore(self, snap: dict) -> None:
        with self.lock:
            shutil.rmtree(self.board)
            shutil.copytree(snap["dest"], self.board, copy_function=shutil.copy2)
            self.epoch = snap["epoch"]
            self.lines = list(snap["lines"])
            self.entries = copy.deepcopy(snap["entries"])
            self.baseline = dict(snap["baseline"])
            self.history = copy.deepcopy(snap["history"])

    # -- sync assembly --------------------------------------------------------

    def _length_at(self, rel: str, seq: int) -> int | None:
        length = self.baseline.get(rel)
        for at, value in self.history.get(rel, []):
            if at <= seq:
                length = value
        return length

    def _file_entry(self, rel: str, data: bytes, *, inline: bool) -> dict:
        digest = sha256_hex(data)
        entry: dict[str, Any] = {"sha256": digest, "size": len(data)}
        if inline and len(data) <= self.inline_file_bytes:
            entry["content_b64"] = base64.b64encode(data).decode()
        else:
            entry["href"] = self.href(rel, digest)
        return entry

    def href(self, rel: str, digest: str) -> str:
        quoted = urllib.parse.quote(rel)
        return f"/v1/projects/{self.slug}/files/{quoted}?sha256={digest}"

    def sync_body(self, query: dict[str, str]) -> dict:
        with self.lock:
            files_now = durable_files(self.board)
            head = self.head
            body: dict[str, Any] = {"epoch": self.epoch, "head_seq": head}
            if self.head_hash():
                body["head_hash"] = self.head_hash()
            if query.get("manifest") == "1":
                body.update(
                    reset=True,
                    removed=[],
                    files={
                        rel: {"sha256": sha256_hex(p.read_bytes()), "size": p.stat().st_size}
                        for rel, p in sorted(files_now.items())
                    },
                )
                return body
            since = int(query.get("since") or 0)
            reset = (
                query.get("epoch") != self.epoch
                or since > head
                or (since > 0 and query.get("hash") != line_hash(self.lines[since - 1]))
            )
            files: dict[str, dict] = {}
            removed: list[str] = []
            if reset:
                budget = RESET_INLINE_CAP
                for rel, path in sorted(files_now.items()):
                    data = path.read_bytes()
                    inline = len(data) <= budget
                    entry = self._file_entry(rel, data, inline=inline)
                    if "content_b64" in entry:
                        budget -= len(data)
                    files[rel] = entry
            else:
                touched: set[str] = set()
                for entry in self.entries[since:]:
                    touched.update(entry["paths"])
                for rel in sorted(touched):
                    path = self.board / rel
                    if not path.is_file():
                        if rel not in files_now:
                            removed.append(rel)
                        continue
                    data = path.read_bytes()
                    base = self._length_at(rel, since)
                    logged = rel in self.baseline or rel in self.history
                    if (
                        logged
                        and base is not None
                        and base <= len(data)
                        and len(data) - base <= self.inline_file_bytes
                    ):
                        digest = sha256_hex(data)
                        files[rel] = {
                            "sha256": digest,
                            "size": len(data),
                            "append_from": base,
                            "content_b64": base64.b64encode(data[base:]).decode(),
                            "href": self.href(rel, digest),
                        }
                    else:
                        files[rel] = self._file_entry(rel, data, inline=True)
            body.update(reset=reset, files=files, removed=removed)
            return body

    def file_bytes(self, rel: str, digest: str | None) -> tuple[int, bytes | dict]:
        with self.lock:
            if classify_path(rel) not in (PathClass.DURABLE, PathClass.WORKSPACE) or ".." in rel:
                return 404, {"code": "NOT_FOUND", "message": f"no board file {rel}"}
            path = self.board / rel
            if not path.is_file():
                return 404, {"code": "NOT_FOUND", "message": f"no board file {rel}"}
            data = path.read_bytes()
            if digest is not None and sha256_hex(data) != digest:
                return 412, {"code": "STALE_VERSION", "message": f"{rel} has changed"}
            return 200, data


class _Handler(BaseHTTPRequestHandler):
    server_version = "stub"
    stub: StubServer

    def log_message(self, format: str, *args: Any) -> None:
        pass

    def _send(self, status: int, body: bytes, content_type: str) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Lattice-Protocol", "1")
        self.send_header("Lattice-Server-Version", STUB_VERSION)
        self.send_header("Lattice-Min-Client-Version", "0.2.1")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        try:
            self.wfile.write(body)
        except OSError:
            pass

    def _raw(self, status: int, headers: dict[str, str], body: bytes) -> None:
        self.send_response(status)
        for name, value in headers.items():
            self.send_header(name, value)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _envelope(self, status: int, payload: Any) -> None:
        if 200 <= status < 300:
            doc = {"ok": True, "data": payload}
        else:
            doc = {"ok": False, "error": payload}
        self._send(status, json.dumps(doc).encode(), "application/json")

    def do_GET(self) -> None:
        stub = self.stub
        parts = urllib.parse.urlsplit(self.path)
        query = {k: v[-1] for k, v in urllib.parse.parse_qs(parts.query).items()}
        stub.requests.append({"path": self.path, "headers": dict(self.headers.items())})
        if stub.fault.raw is not None:
            self._raw(*stub.fault.raw)
            return
        if self.headers.get("Authorization") != f"Bearer {stub.token}":
            self._envelope(401, {"code": "UNAUTHENTICATED", "message": "bad token"})
            return
        prefix = f"/v1/projects/{stub.slug}/"
        if not parts.path.startswith(prefix):
            self._envelope(404, {"code": "NOT_FOUND", "message": "no such project"})
            return
        rest = parts.path[len(prefix) :]
        if rest == "sync":
            stub.arrivals.append(("sync", query))
            if query.get("manifest") == "1" and stub.fault.raw_manifest is not None:
                self._raw(*stub.fault.raw_manifest)
                return
            body = stub.sync_body(query)
            if stub.fault.mutate_sync is not None:
                stub.fault.mutate_sync(body)
            gate = stub.fault.sync_gate
            if gate is not None:
                gate.wait(10)
            self._envelope(200, body)
            return
        if rest.startswith("files/"):
            rel = urllib.parse.unquote(rest[len("files/") :])
            stub.arrivals.append(("files", {"path": rel, **query}))
            if stub.fault.raw_files is not None:
                self._raw(*stub.fault.raw_files)
                return
            gate = stub.fault.files_gate
            if gate is not None:
                gate.wait(10)
            status, payload = stub.file_bytes(rel, query.get("sha256"))
            stub.file_statuses.append((rel, status))
            if status != 200:
                self._envelope(status, payload)
                return
            assert isinstance(payload, bytes)
            if stub.fault.mutate_file is not None:
                payload = stub.fault.mutate_file(rel, payload)
            self._send(200, payload, "application/octet-stream")
            return
        self._envelope(404, {"code": "NOT_FOUND", "message": f"no route {parts.path}"})


@contextmanager
def running_stub(board_root: Path, **options: Any) -> Iterator[StubServer]:
    """Serve a stub over *board_root* (a project directory with ``.lattice/``) on
    ``127.0.0.1:0`` until the block exits. The epoch's baseline is taken now."""
    stub = StubServer(Path(board_root), **options)
    stub.start_epoch()
    handler = type("StubHandler", (_Handler,), {"stub": stub})
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    httpd.daemon_threads = True
    stub.url = f"http://127.0.0.1:{httpd.server_address[1]}"
    thread = threading.Thread(
        target=httpd.serve_forever, args=(0.02,), name="stub-sync", daemon=True
    )
    thread.start()
    try:
        yield stub
    finally:
        for gate in (stub.fault.sync_gate, stub.fault.files_gate):
            if gate is not None:
                gate.set()
        httpd.shutdown()
        httpd.server_close()
        thread.join(timeout=5)
