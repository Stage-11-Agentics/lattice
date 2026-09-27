"""The load rig for AC-42 (``tests/torture/test_load.py``): a server subprocess
holding a 1,000-task board, with readers, writers, and followers in their
specified roles, and write latency measured per operation.

Shared by ``test_readers_writers`` (H-15) and ``test_with_dashboards`` (H-13b),
which adds dashboard viewers to the same rig (fixture ``load_rig`` in
``tests/torture/conftest.py``)::

    def test_x(load_rig: LoadRig) -> None:
        load_rig.start_followers(5)
        readers = load_rig.start_readers(20)
        latencies = load_rig.run_writers(5, seconds=60)   # blocks; fails fast
        reads = load_rig.stop_readers(readers)
        assert p95(latencies) < 0.5

Roles and cadence (the same shape as H-13b's harness):

- A **reader** is a long-lived process on its own bound checkout (no follower),
  running the real client's ``lattice list --json`` every
  :data:`READ_INTERVAL_SECONDS`, so each read is a catch-up plus a ``list``
  (SPEC §9.5). Readers start staggered across one interval.
- A **follower** is ``lattice sync --follow`` on its own checkout.
- A **writer** is a thread with one keep-alive HTTP connection, posting one
  mixed operation (with an ``op_id``) every :data:`WRITE_INTERVAL_SECONDS`;
  its latency is one operation's round trip, what ``HostedBoard.execute`` waits
  for before its post-write sync.

Every reader, follower, writer, and loader has its own token, as separate
machines would (one token would hit ``max_inflight_per_token`` and the per-token
rate). A reader or follower that dies fails the run at once; every reader ends
its output with a ``done`` line. ``TORTURE_CLIENT_DIR`` (not a ``LATTICE_*``
name: the suite strips those) puts client checkouts elsewhere, for example a
tmpfs, so client caches stop sharing the server's disk; it never changes the
workload.
"""

from __future__ import annotations

import http.client
import json
import os
import random
import shutil
import subprocess
import tempfile
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path

from lattice.core.ids import generate_op_id
from lattice.server.testing import make_root
from tests.torture.harness import (
    PROJECT,
    REMOTE,
    Client,
    ServerProcess,
    chmod_tree_writable,
    lattice,
    lattice_json,
    spawn_lattice,
    stop_process,
)
from tests.torture.rehearsal import read_jsonl, start_scripted

READ_INTERVAL_SECONDS = 5.0
WRITE_INTERVAL_SECONDS = 0.2


def p95(values: list[float]) -> float:
    """The 95th percentile (nearest rank) of *values*."""
    ordered = sorted(values)
    assert ordered, "no samples"
    return ordered[max(0, -(-95 * len(ordered) // 100) - 1)]


@dataclass
class WriterReport:
    latencies: list[float] = field(default_factory=list)
    finished: bool = False


@dataclass
class LoadRig:
    work: Path
    server: ServerProcess
    tasks: list[str]
    checkouts: int = 0
    procs: list[subprocess.Popen] = field(default_factory=list)
    #: Each checkout's own client (and token).
    clients: dict[Path, Client] = field(default_factory=dict)
    client_dir: Path = field(default_factory=Path)
    writers: list[WriterReport] = field(default_factory=list)

    def __post_init__(self) -> None:
        base = os.environ.get("TORTURE_CLIENT_DIR")
        self.client_dir = (
            Path(tempfile.mkdtemp(prefix="lattice-load-", dir=base))
            if base
            else self.work / "checkouts"
        )

    @classmethod
    def build(cls, work: Path, *, tasks: int = 1000) -> LoadRig:
        """Start a server and fill project ``demo`` with *tasks* tasks over HTTP. The
        server is stopped again if anything here fails."""
        server = ServerProcess(make_root(work, projects={PROJECT: {"code": "DEM"}}))
        server.start()
        try:
            loaders = [server.mint(user="human:loader", machine=f"loader-{n}") for n in range(8)]

            def create(n: int) -> str:
                status, body = server.op(
                    "task.create",
                    {"title": f"Load task {n}", "description": "x" * 200},
                    token=loaders[n % len(loaders)],
                    actor="agent:loader",
                    op_id=generate_op_id(),
                )
                assert status == 200, body
                return body["data"]["result"]["task"]["short_id"]

            with ThreadPoolExecutor(8) as pool:
                short_ids = list(pool.map(create, range(tasks)))
            return cls(work=work, server=server, tasks=short_ids)
        except BaseException:
            server.stop()
            raise

    # -- clients ------------------------------------------------------------

    def checkout(self, label: str) -> Path:
        """A bound directory with its initial sync done, for a client of its own
        (``self.clients[path]``)."""
        self.checkouts += 1
        name = f"{label}-{self.checkouts}"
        path = self.client_dir / name
        path.mkdir(parents=True)
        (path / ".lattice-remote.json").write_text(
            json.dumps({"project": PROJECT, "remote": REMOTE}) + "\n"
        )
        client = self.server.client(self.client_dir / f"home-{name}", machine=name)
        self.clients[path] = client
        lattice(client, path, "sync", timeout=600)
        return path

    def checkouts_for(self, label: str, n: int) -> list[Path]:
        with ThreadPoolExecutor(2) as pool:
            return list(pool.map(lambda _: self.checkout(label), range(n)))

    def start_followers(self, n: int) -> list[subprocess.Popen]:
        """*n* followers, each on its own checkout, returned once each reads as live."""
        followers = []
        for path in self.checkouts_for("follower", n):
            client = self.clients[path]
            log = path.parent / f"{path.name}.log"
            proc = spawn_lattice(client, path, "sync", "--follow", log=log)
            self.procs.append(proc)
            followers.append(proc)
            deadline = time.monotonic() + 60
            while not lattice_json(client, path, "remote", "status")["follower"]["live"]:
                assert proc.poll() is None, log.read_text()
                assert time.monotonic() < deadline, "follower never went live"
                time.sleep(0.2)
        return followers

    def start_readers(self, n: int) -> list[tuple[subprocess.Popen, Path, Path]]:
        """*n* reader processes, each running ``list`` every READ_INTERVAL_SECONDS
        from its own checkout, staggered across one interval, until :meth:`stop_readers`."""
        readers = []
        stop = self.work / "stop-readers"
        for k, path in enumerate(self.checkouts_for("reader", n)):
            out = self.work / f"reader-{k}.jsonl"
            proc = start_scripted(
                self.clients[path],
                {
                    "mode": "poll",
                    "cwds": [str(path)],
                    "out": str(out),
                    "stop": str(stop),
                    "summary": True,
                    "interval": READ_INTERVAL_SECONDS,
                    "offset": READ_INTERVAL_SECONDS * k / n,
                },
                self.work / f"reader-{k}",
            )
            self.procs.append(proc)
            readers.append((proc, path, out))
        return readers

    # -- writers ------------------------------------------------------------

    def run_writers(self, n: int, *, seconds: float) -> list[float]:
        """*n* writers, one operation every WRITE_INTERVAL_SECONDS each, for
        *seconds*; returns every operation's latency. Fails at once if an
        operation fails or a reader or follower dies, and afterwards unless every
        writer finished with samples (``self.writers``)."""
        self.writers = [WriterReport() for _ in range(n)]
        tokens = [self.server.mint(user="human:writer", machine=f"writer-{w}") for w in range(n)]
        errors: list[str] = []
        abort = threading.Event()
        deadline = time.monotonic() + seconds

        def writer(w: int) -> None:
            rng = random.Random(w)
            report = self.writers[w]
            conn: http.client.HTTPConnection | None = None
            next_at = time.monotonic()
            k = 0
            try:
                while time.monotonic() < deadline and not abort.is_set():
                    time.sleep(max(0.0, next_at - time.monotonic()))
                    next_at = max(next_at + WRITE_INTERVAL_SECONDS, time.monotonic())
                    k += 1
                    op, params = _mixed_op(rng, self.tasks, w, k)
                    if conn is None:
                        conn = http.client.HTTPConnection(
                            "127.0.0.1", self.server.port, timeout=60
                        )
                    started = time.monotonic()
                    status, body = _post(conn, op, params, tokens[w], f"agent:w{w}")
                    report.latencies.append(time.monotonic() - started)
                    if status != 200:
                        errors.append(f"writer {w}: {op} {params}: {status} {body}")
                        abort.set()
                report.finished = True
            except Exception as exc:  # noqa: BLE001 - reported, and fails the run
                errors.append(f"writer {w}: {type(exc).__name__}: {exc}")
                abort.set()
            finally:
                if conn is not None:
                    conn.close()

        threads = [threading.Thread(target=writer, args=(w,), daemon=True) for w in range(n)]
        for thread in threads:
            thread.start()
        while any(t.is_alive() for t in threads):
            dead = [p for p in self.procs if p.poll() is not None]
            if dead and not abort.is_set():
                errors.append(f"{len(dead)} reader or follower process(es) died: {dead[0].args}")
                abort.set()
            if time.monotonic() > deadline + 120:
                errors.append("a writer is still running two minutes past the window")
                abort.set()
                break
            time.sleep(0.2)
        assert not errors, errors[:3]
        unfinished = [w for w, r in enumerate(self.writers) if not r.finished or not r.latencies]
        assert not unfinished, f"writers without a full run and samples: {unfinished}"
        return [x for report in self.writers for x in report.latencies]

    def stop_readers(self, readers: list[tuple[subprocess.Popen, Path, Path]]) -> list[dict]:
        """Stop the readers; every one must end with its ``done`` line. Returns every
        read (``t0``/``t``, the task count, any notice)."""
        (self.work / "stop-readers").write_text("")
        rows = []
        for proc, path, out in readers:
            proc.wait(timeout=READ_INTERVAL_SECONDS + 120)
            lines = read_jsonl(out)
            assert proc.returncode == 0 and lines and lines[-1].get("done"), (path, lines[-1:])
            rows += [line for line in lines if "cwd" in line]
        return rows

    def close(self) -> None:
        (self.work / "stop-readers").write_text("")
        for proc in self.procs:
            stop_process(proc)
        self.server.stop()
        if not self.client_dir.is_relative_to(self.work):
            chmod_tree_writable(self.client_dir)
            shutil.rmtree(self.client_dir, ignore_errors=True)


def _mixed_op(rng: random.Random, tasks: list[str], w: int, k: int) -> tuple[str, dict]:
    task = rng.choice(tasks)
    roll = rng.random()
    if roll < 0.2:
        return "task.create", {"title": f"writer {w} task {k}"}
    if roll < 0.7:
        return "task.comment", {"task": task, "text": f"w{w} c{k}"}
    if roll < 0.85:
        return "task.assign", {"task": task, "actor_id": f"agent:w{w}"}
    priority = rng.choice(["low", "medium", "high"])
    return "task.update", {"task": task, "pairs": [f"priority={priority}"]}


def _post(
    conn: http.client.HTTPConnection, op: str, params: dict, token: str, actor: str
) -> tuple[int, object]:
    body = json.dumps({"params": params, "actor": actor, "op_id": generate_op_id()})
    conn.request(
        "POST",
        f"/v1/projects/{PROJECT}/ops/{op}",
        body=body,
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
    )
    response = conn.getresponse()
    raw = response.read()
    try:
        return response.status, json.loads(raw)
    except ValueError:
        return response.status, raw[:200]
