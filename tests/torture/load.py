"""The load rig for AC-42 (``tests/torture/test_load.py``): a server subprocess
holding a 1,000-task board, readers, writers, and followers, with write latency
measured per operation.

Shared by ``test_readers_writers`` (H-15) and ``test_with_dashboards`` (H-13b),
which adds dashboard viewers to the same rig::

    rig = LoadRig.build(tmp_path, tasks=1000)
    try:
        rig.start_followers(5)
        readers = rig.start_readers(20)
        latencies = rig.run_writers(5, seconds=60)   # blocks for the duration
        reads = rig.stop_readers(readers)
    finally:
        rig.close()
    assert p95(latencies) < 0.5

- A **reader** is a process looping ``list --json`` from its own checkout with no
  follower, so every read is a catch-up plus a ``list`` (SPEC §9.5).
- A **follower** is ``lattice sync --follow`` in its own checkout.
- A **writer** is a thread posting mixed operations over HTTP with an ``op_id``;
  its latency is one operation's round trip, what ``HostedBoard.execute`` waits
  for before its post-write sync.

Checkouts are directories holding only ``.lattice-remote.json``; each gets its
initial sync before the measured window. ``TORTURE_LOAD_SECONDS`` and
``TORTURE_LOAD_TASKS`` (not ``LATTICE_*`` names: the suite strips those) shrink
the run for a quick local check. ``TORTURE_CLIENT_DIR`` puts the client
checkouts elsewhere (a tmpfs): on one box every client cache fsyncs on the
server's disk, which a real deployment's clients never do.
"""

from __future__ import annotations

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

LOAD_SECONDS = float(os.environ.get("TORTURE_LOAD_SECONDS", "60"))
LOAD_TASKS = int(os.environ.get("TORTURE_LOAD_TASKS", "1000"))


def p95(values: list[float]) -> float:
    """The 95th percentile (nearest rank) of *values*."""
    ordered = sorted(values)
    assert ordered, "no samples"
    return ordered[max(0, -(-95 * len(ordered) // 100) - 1)]


@dataclass
class LoadRig:
    work: Path
    server: ServerProcess
    client: Client
    token: str
    tasks: list[str]
    checkouts: int = 0
    procs: list[subprocess.Popen] = field(default_factory=list)
    #: Each checkout's own client (and token): every reader, follower, and writer
    #: is its own machine, as far as the server's per-token limits go.
    clients: dict[Path, Client] = field(default_factory=dict)
    #: Where client checkouts live: under *work*, or under ``TORTURE_CLIENT_DIR``
    #: (for example a tmpfs), so client cache writes stop sharing the server's disk.
    client_dir: Path = field(default_factory=lambda: Path())

    def __post_init__(self) -> None:
        base = os.environ.get("TORTURE_CLIENT_DIR")
        self.client_dir = (
            Path(tempfile.mkdtemp(prefix="lattice-load-", dir=base))
            if base
            else self.work / "checkouts"
        )

    @classmethod
    def build(cls, work: Path, *, tasks: int = LOAD_TASKS) -> LoadRig:
        """Start a server and fill project ``demo`` with *tasks* tasks over HTTP."""
        server = ServerProcess(make_root(work, projects={PROJECT: {"code": "DEM"}}))
        server.start()
        client = server.client(work / "home", user="human:alice", machine="load-box")
        token = server.mint(user="human:loader", machine="load-box")
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
        return cls(work=work, server=server, client=client, token=token, tasks=short_ids)

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
        lattice(client, path, "sync", timeout=300)
        return path

    def checkouts_for(self, label: str, n: int) -> list[Path]:
        with ThreadPoolExecutor(2) as pool:
            return list(pool.map(lambda _: self.checkout(label), range(n)))

    def start_followers(self, n: int) -> list[subprocess.Popen]:
        """*n* followers, each on its own checkout, returned once each reads as live."""
        followers = []
        for path in self.checkouts_for("follower", n):
            client = self.clients[path]
            proc = spawn_lattice(
                client, path, "sync", "--follow", log=path.parent / f"{path.name}.log"
            )
            self.procs.append(proc)
            followers.append(proc)
            deadline = time.monotonic() + 30
            while not lattice_json(client, path, "remote", "status")["follower"]["live"]:
                assert proc.poll() is None, (path.parent / f"{path.name}.log").read_text()
                assert time.monotonic() < deadline, "follower never went live"
                time.sleep(0.2)
        return followers

    def start_readers(self, n: int) -> list[tuple[subprocess.Popen, Path, Path]]:
        """*n* reader processes looping catch-up plus ``list`` until :meth:`stop`."""
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
                },
                self.work / f"reader-{k}",
            )
            self.procs.append(proc)
            readers.append((proc, path, out))
        return readers

    def run_writers(self, n: int, *, seconds: float = LOAD_SECONDS) -> list[float]:
        """*n* writer threads posting mixed operations for *seconds*; returns every
        operation's latency in seconds. Any failed operation fails the run."""
        latencies: list[float] = []
        errors: list[str] = []
        lock = threading.Lock()
        tokens = [self.server.mint(user="human:writer", machine=f"writer-{w}") for w in range(n)]
        deadline = time.monotonic() + seconds

        def writer(w: int) -> None:
            rng = random.Random(w)
            k = 0
            while time.monotonic() < deadline and not errors:
                k += 1
                task = rng.choice(self.tasks)
                roll = rng.random()
                if roll < 0.2:
                    op, params = "task.create", {"title": f"writer {w} task {k}"}
                elif roll < 0.7:
                    op, params = "task.comment", {"task": task, "text": f"w{w} c{k}"}
                elif roll < 0.85:
                    op, params = "task.assign", {"task": task, "actor_id": f"agent:w{w}"}
                else:
                    priority = rng.choice(["low", "medium", "high"])
                    op, params = "task.update", {"task": task, "pairs": [f"priority={priority}"]}
                started = time.monotonic()
                status, body = self.server.op(
                    op, params, token=tokens[w], actor=f"agent:w{w}", op_id=generate_op_id()
                )
                elapsed = time.monotonic() - started
                with lock:
                    if status != 200:
                        errors.append(f"{op} {params}: {status} {body}")
                    latencies.append(elapsed)

        threads = [threading.Thread(target=writer, args=(w,)) for w in range(n)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=seconds + 120)
        assert not errors, errors[:3]
        return latencies

    def stop_readers(self, readers: list[tuple[subprocess.Popen, Path, Path]]) -> list[dict]:
        """Stop the readers; return every read (``t0``/``t`` and the task count)."""
        (self.work / "stop-readers").write_text("")
        rows = []
        for proc, _path, out in readers:
            proc.wait(timeout=120)
            assert proc.returncode == 0, f"reader exited {proc.returncode}"
            rows += read_jsonl(out)
        return rows

    def close(self) -> None:
        (self.work / "stop-readers").write_text("")
        for proc in self.procs:
            stop_process(proc)
        self.server.stop()
        if not self.client_dir.is_relative_to(self.work):
            chmod_tree_writable(self.client_dir)
            shutil.rmtree(self.client_dir, ignore_errors=True)
