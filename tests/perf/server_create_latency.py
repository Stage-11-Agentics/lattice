"""Server ``task.create`` latency over HTTP, p50 and p95 (H-22a's measured cost).

A measurement, not a check: run it on the reference machine and record the
numbers. It uses only H-9's test harness, so the same file runs against a
checkout from before transactions for the "without" figure::

    uv run python -m tests.perf.server_create_latency [--n 300]
"""

from __future__ import annotations

import argparse
import json
import statistics
import tempfile
import time
from pathlib import Path

from lattice.server import tokens
from lattice.server.testing import make_root, running_server


def measure(n: int, warmup: int = 20) -> dict:
    with tempfile.TemporaryDirectory() as tmp:
        root = make_root(Path(tmp), projects={"bench": {"code": "BEN"}})
        token = tokens.create_token(root, user="human:bench", machine="m", all_projects=True)
        with running_server(root, log_level="error") as server:
            samples = []
            for i in range(warmup + n):
                started = time.perf_counter()
                status, _, body = server.op(
                    "bench", "task.create", {"title": f"t{i}"}, token=token["token"]
                )
                elapsed = (time.perf_counter() - started) * 1000
                assert status == 200, body
                if i >= warmup:
                    samples.append(elapsed)
    samples.sort()
    return {
        "n": n,
        "p50_ms": round(statistics.median(samples), 2),
        "p95_ms": round(samples[int(0.95 * (len(samples) - 1))], 2),
        "mean_ms": round(statistics.fmean(samples), 2),
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--n", type=int, default=300)
    args = parser.parse_args()
    print(json.dumps(measure(args.n)))


if __name__ == "__main__":
    main()
