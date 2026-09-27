"""LAT-330's read-latency bench (not a test; run by hand).

A real ``lattice server serve`` on 127.0.0.1, one bound checkout, and *W*
writer and *R* reader processes on that one checkout for *S* seconds. Each
writer runs ``lattice comment`` in a loop, each reader ``lattice list --json``.
Every client command runs under a tracing shim that records, per command, the
client-side time after imports and the time spent waiting on each cache lock,
in the sync's HTTP requests, in its apply, and in ``fsync``::

    uv run python tests/torture/read_latency_bench.py --writers 10 --readers 10 \
        --seconds 60 --tasks 200 [--client-dir /dev/shm] [--idle]

``--idle`` instead measures one user's write-then-read on the idle server
(``comment`` then ``list``, 20 times).
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from lattice.core.ids import generate_op_id  # noqa: E402
from lattice.server.testing import make_root  # noqa: E402
from tests.torture.harness import (  # noqa: E402
    PROJECT,
    REMOTE,
    ServerProcess,
    chmod_tree_writable,
    lattice,
)

#: The CLI under a tracer: one JSON line per command to ``$BENCH_TRACE``.
TRACED_CLI = r"""
import atexit, json, os, sys, time
t0 = time.monotonic()
from lattice.cli.main import cli
from lattice.remote import cache, http
t1 = time.monotonic()
acc = {"lock_sync": 0.0, "lock_rw": 0.0, "http": 0.0, "apply": 0.0,
       "fsync": 0.0, "fsyncs": 0, "catch_up": 0.0, "syncs": 0}
_lock = cache._lock
def lock(path, exclusive, deadline):
    s = time.monotonic()
    try:
        return _lock(path, exclusive, deadline)
    finally:
        acc["lock_sync" if "sync" in path.name else "lock_rw"] += time.monotonic() - s
cache._lock = lock
_request = http.request
def request(*a, **k):
    s = time.monotonic()
    try:
        return _request(*a, **k)
    finally:
        acc["http"] += time.monotonic() - s
http.request = request
_apply = cache._Syncer._apply
def apply(self, *a, **k):
    s = time.monotonic()
    try:
        return _apply(self, *a, **k)
    finally:
        acc["apply"] += time.monotonic() - s
cache._Syncer._apply = apply
_catch_up = cache.catch_up
def catch_up(*a, **k):
    s = time.monotonic()
    try:
        return _catch_up(*a, **k)
    finally:
        acc["catch_up"] += time.monotonic() - s
cache.catch_up = catch_up
_run = cache._Syncer.run
def run(self):
    acc["syncs"] += 1
    return _run(self)
cache._Syncer.run = run
_fsync = os.fsync
def fsync(fd):
    s = time.monotonic()
    try:
        return _fsync(fd)
    finally:
        acc["fsync"] += time.monotonic() - s
        acc["fsyncs"] += 1
os.fsync = fsync
def done():
    acc["client"] = time.monotonic() - t1
    acc["imports"] = t1 - t0
    acc["cmd"] = sys.argv[1]
    with open(os.environ["BENCH_TRACE"], "a") as fh:
        fh.write(json.dumps(acc) + "\n")
atexit.register(done)
cli(prog_name="lattice")
"""


def run_cli(env: dict, cwd: Path, *args: str) -> tuple[float, int, str]:
    start = time.monotonic()
    proc = subprocess.run(
        [sys.executable, "-c", TRACED_CLI, *args],
        cwd=cwd,
        env=env,
        capture_output=True,
        text=True,
        timeout=300,
    )
    return time.monotonic() - start, proc.returncode, proc.stderr


def pct(values: list[float], p: int) -> float:
    ordered = sorted(values)
    return ordered[max(0, -(-p * len(ordered) // 100) - 1)]


def summary(label: str, values: list[float]) -> str:
    if not values:
        return f"{label}: none"
    return (
        f"{label}: n={len(values)} p50={pct(values, 50) * 1000:.0f}ms "
        f"p95={pct(values, 95) * 1000:.0f}ms max={max(values) * 1000:.0f}ms"
    )


def phases(rows: list[dict], cmd: str) -> str:
    mine = [r for r in rows if r["cmd"] == cmd]
    if not mine:
        return f"{cmd}: no traces"
    keys = ("client", "catch_up", "lock_sync", "lock_rw", "http", "apply", "fsync")
    means = " ".join(f"{k}={sum(r[k] for r in mine) / len(mine) * 1000:.0f}" for k in keys)
    fsyncs = sum(r["fsyncs"] for r in mine) / len(mine)
    syncs = sum(r["syncs"] for r in mine) / len(mine)
    return f"{cmd} mean ms: {means} fsyncs={fsyncs:.1f} syncs={syncs:.2f}"


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--writers", type=int, default=10)
    parser.add_argument("--readers", type=int, default=10)
    parser.add_argument("--seconds", type=float, default=60)
    parser.add_argument("--tasks", type=int, default=200)
    parser.add_argument("--client-dir", default=None)
    parser.add_argument("--idle", action="store_true")
    args = parser.parse_args()

    work = Path(tempfile.mkdtemp(prefix="lat330-"))
    client_base = Path(tempfile.mkdtemp(prefix="lat330-client-", dir=args.client_dir))
    server = ServerProcess(make_root(work, projects={PROJECT: {"code": "DEM"}}))
    server.start()
    try:
        loader = server.mint(user="human:loader", machine="loader")

        def create(n: int) -> str:
            status, body = server.op(
                "task.create",
                {"title": f"Bench task {n}", "description": "x" * 200},
                token=loader,
                actor="agent:loader",
                op_id=generate_op_id(),
            )
            assert status == 200, body
            return body["data"]["result"]["task"]["short_id"]

        with ThreadPoolExecutor(8) as pool:
            tasks = list(pool.map(create, range(args.tasks)))
        checkout = client_base / "checkout"
        checkout.mkdir()
        (checkout / ".lattice-remote.json").write_text(
            json.dumps({"project": PROJECT, "remote": REMOTE}) + "\n"
        )
        first = server.client(client_base / "home-0", machine="m0")
        lattice(first, checkout, "sync", timeout=600)
        trace = work / "trace.jsonl"

        def env_for(n: int) -> dict:
            client = server.client(client_base / f"home-{n}", machine=f"m{n}")
            return {**client.env, "BENCH_TRACE": str(trace), "LATTICE_NO_UPDATE_CHECK": "1"}

        if args.idle:
            env = env_for(1)
            pairs = []
            for k in range(20):
                w, code, err = run_cli(
                    env, checkout, "comment", tasks[0], f"idle {k}", "--actor", "agent:idle"
                )
                assert code == 0, err
                r, code, err = run_cli(env, checkout, "list", "--json")
                assert code == 0, err
                pairs.append((w, r))
            rows = [json.loads(line) for line in trace.read_text().splitlines()]
            clients = [rows[i]["client"] + rows[i + 1]["client"] for i in range(0, len(rows), 2)]
            print(summary("idle write+read wall", [w + r for w, r in pairs]))
            print(summary("idle write+read client-side (excl. start)", clients))
            print(phases(rows, "comment"))
            print(phases(rows, "list"))
            return

        deadline = time.monotonic() + args.seconds
        reads: list[float] = []
        writes: list[float] = []
        errors: list[str] = []
        lock = threading.Lock()

        def worker(n: int, reader: bool) -> None:
            env = env_for(n + 2)
            k = 0
            while time.monotonic() < deadline:
                k += 1
                if reader:
                    wall, code, err = run_cli(env, checkout, "list", "--json")
                else:
                    task = tasks[(n * 7 + k) % len(tasks)]
                    wall, code, err = run_cli(
                        env, checkout, "comment", task, f"w{n}-{k}", "--actor", f"agent:w{n}"
                    )
                with lock:
                    (reads if reader else writes).append(wall)
                    if code != 0 or "lattice:" in err:
                        errors.append(err.strip()[:300])

        threads = [threading.Thread(target=worker, args=(n, True)) for n in range(args.readers)]
        threads += [
            threading.Thread(target=worker, args=(args.readers + n, False))
            for n in range(args.writers)
        ]
        loads = []
        for t in threads:
            t.start()
        while any(t.is_alive() for t in threads):
            loads.append(os.getloadavg()[0])
            time.sleep(1)
        rows = [json.loads(line) for line in trace.read_text().splitlines()]
        print(
            f"{args.writers} writers, {args.readers} readers, {args.seconds:.0f}s, "
            f"{args.tasks} tasks, {os.cpu_count()} cpus, client dir {client_base}, "
            f"load avg (1m) max={max(loads):.1f} end={os.getloadavg()[0]:.1f}"
        )
        print(summary("read wall", reads))
        print(summary("write wall", writes))
        print(summary("read client-side", [r["client"] for r in rows if r["cmd"] == "list"]))
        print(phases(rows, "list"))
        print(phases(rows, "comment"))
        print(f"errors/notices: {len(errors)} {errors[:3]}")
    finally:
        server.stop()
        for path in (work, client_base):
            chmod_tree_writable(path)
            shutil.rmtree(path, ignore_errors=True)


if __name__ == "__main__":
    main()
