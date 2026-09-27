"""The load rig for AC-42 (``tests/torture/test_load.py``): a server subprocess
holding a 1,000-task board, with readers, writers, and followers in their
specified roles, and write latency measured per operation.

Shared by ``test_readers_writers`` (H-15) and ``test_with_dashboards`` (H-13b),
which adds dashboard viewers to the same rig::

    def test_x(tmp_path: Path) -> None:
        with LoadRig.running(tmp_path) as rig:       # fails, never skips
            rig.start_followers(5)
            readers = rig.start_readers(20)
            latencies = rig.run_writers(5, seconds=60)   # blocks; fails fast
            reads = rig.stop_readers(readers)
        print(p95(read_latencies(reads)))               # reported, not bounded
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
its output with a ``done`` line.

**Client filesystem (EVALUATION AC-42).** The clients stand in for separate
machines, so their checkouts and caches live on a filesystem other than the
server root's: ``/dev/shm`` on Linux, or on macOS a RAM disk the rig creates
(``hdiutil attach -nomount ram://...`` then ``diskutil erasevolume APFS``) and
always detaches. If it cannot arrange that, :meth:`LoadRig.build` fails and says
why; it never skips, and it asserts the two directories are on different
devices (``st_dev``) before any client exists. Client read latency is reported
(``read_latencies``), not bounded (LAT-330).
"""

from __future__ import annotations

import http.client
import json
import os
import random
import shutil
import sys
import subprocess
import tempfile
import threading
import time
from collections.abc import Callable, Iterator
from concurrent.futures import ThreadPoolExecutor
from contextlib import AbstractContextManager, ExitStack, contextmanager
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


#: The macOS RAM disk's size: room for 30 caches of a 1,000-task board and more.
RAM_DISK_BYTES = 2 * 1024**3


#: Provides a mount point on a filesystem other than the server root's, for the
#: duration of the ``with``. The real one allocates; tests of the policy inject a fake.
MountProvider = Callable[[], AbstractContextManager[Path]]


@contextmanager
def platform_mount() -> Iterator[Path]:
    """The real allocator: ``/dev/shm`` on Linux; on macOS a RAM disk created for
    the block and always detached. Raises ``AssertionError`` naming the reason when
    neither is possible. Only the envelope load tests reach it."""
    if sys.platform == "darwin":
        with _mac_ram_disk() as mount:
            yield mount
        return
    shm = Path("/dev/shm")
    if not (shm.is_dir() and os.access(shm, os.W_OK)):
        raise AssertionError(
            "AC-42 needs the clients on a filesystem separate from the server's; "
            f"/dev/shm is not a writable directory on this {sys.platform} host"
        )
    yield shm


@contextmanager
def separate_client_filesystem(mount: MountProvider | None = None) -> Iterator[Path]:
    """A fresh directory under *mount*'s mount point, removed afterwards however the
    block ends. *mount* defaults to :func:`platform_mount`, looked up at call
    time. The device check against the server root is
    :func:`assert_separate_filesystems`, which :meth:`LoadRig.build` runs before
    any client exists: the load verdict is never given with clients on the
    server's filesystem."""
    with (mount or platform_mount)() as mount_point:
        path = Path(tempfile.mkdtemp(prefix="lattice-load-", dir=mount_point))
        try:
            yield path
        finally:
            chmod_tree_writable(path)
            shutil.rmtree(path, ignore_errors=True)


def _allocator_command(cmd: list[str], *, timeout: float) -> subprocess.CompletedProcess:
    """Run one RAM-disk command (``hdiutil`` / ``diskutil``). Every allocation goes
    through here, so the per-PR guard tests can refuse allocation without
    touching any other subprocess (``project create`` runs ``git``)."""
    return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)


@contextmanager
def _mac_ram_disk() -> Iterator[Path]:
    sectors = RAM_DISK_BYTES // 512
    attach = _allocator_command(
        ["hdiutil", "attach", "-nomount", f"ram://{sectors}"],
        timeout=60,
    )
    if attach.returncode != 0:
        raise AssertionError(f"AC-42 could not create a RAM disk for the clients: {attach.stderr}")
    device = attach.stdout.strip().split()[0]
    try:
        name = f"LatticeLoad{os.getpid()}"
        erase = _allocator_command(
            ["diskutil", "erasevolume", "APFS", name, device],
            timeout=120,
        )
        if erase.returncode != 0:
            raise AssertionError(f"AC-42 could not format the RAM disk {device}: {erase.stderr}")
        yield Path("/Volumes") / name
    finally:
        _allocator_command(["hdiutil", "detach", device, "-force"], timeout=60)


def assert_separate_filesystems(
    client_dir: Path, server_root: Path, *, device: Callable[[Path], int] | None = None
) -> None:
    """Fail unless the two paths are on different devices (``st_dev``); *device*
    replaces the ``os.stat`` lookup in tests of the policy."""
    device = device or (lambda path: os.stat(path).st_dev)
    client_dev, server_dev = device(client_dir), device(server_root)
    assert client_dev != server_dev, (
        f"client dir {client_dir} and server root {server_root} are on one filesystem "
        f"(st_dev {client_dev}); AC-42's clients stand in for separate machines"
    )


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
    #: On a filesystem separate from the server root's (:func:`separate_client_filesystem`).
    client_dir: Path = field(default_factory=Path)
    writers: list[WriterReport] = field(default_factory=list)
    #: Undoes the client filesystem; closed last by :meth:`close`.
    cleanup: ExitStack = field(default_factory=ExitStack)

    @classmethod
    def build(
        cls, work: Path, *, tasks: int = 1000, mount: MountProvider | None = None
    ) -> LoadRig:
        """Arrange the separate client filesystem, start a server, and fill project
        ``demo`` with *tasks* tasks over HTTP. Everything is undone if any step fails."""
        cleanup = ExitStack()
        try:
            client_dir = cleanup.enter_context(separate_client_filesystem(mount))
            server = ServerProcess(make_root(work, projects={PROJECT: {"code": "DEM"}}))
            assert_separate_filesystems(client_dir, server.root)
            server.start()
            cleanup.callback(server.stop)
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
            return cls(
                work=work,
                server=server,
                tasks=short_ids,
                client_dir=client_dir,
                cleanup=cleanup,
            )
        except BaseException:
            cleanup.close()
            raise

    @classmethod
    @contextmanager
    def running(
        cls, work: Path, *, tasks: int = 1000, mount: MountProvider | None = None
    ) -> Iterator[LoadRig]:
        """:meth:`build`, then :meth:`close` however the block ends. Enter it inside
        the test body, so a rig that cannot be arranged (no separate client
        filesystem) FAILS the test rather than erroring a fixture."""
        rig = cls.build(work, tasks=tasks, mount=mount)
        try:
            yield rig
        finally:
            rig.close()

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
        """Stop every child and the server, then remove the client filesystem, even
        if a step fails."""
        try:
            (self.work / "stop-readers").write_text("")
            for proc in self.procs:
                stop_process(proc)
            self.server.stop()
        finally:
            self.cleanup.close()


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


def read_latencies(reads: list[dict]) -> list[float]:
    """Each read's duration (catch-up plus ``list``), for the report (LAT-330)."""
    return [r["t"] - r["t0"] for r in reads]
