"""A scripted Lattice client process for the rehearsals: ``python scripted.py SPEC``.

Runs the CLI in-process (one interpreter start per client, not per command) from
one directory, and writes one JSON line per step to ``spec["out"]``.

- ``{"mode": "write", "cwd", "steps": [{"args": [...], "input"?: str, "save"?: name}],
  "out", "start_at"?: epoch seconds}``: run each step in order. ``save`` names the
  step's task; later arguments may use it as ``{name}``. Each line records the
  exit code, the wall-clock time the command returned (``t``), and, for a step
  that names a task, the task's short ID and ``last_event_id`` after the write
  (from the command's ``--json`` output, or from ``show`` when the output has
  no snapshot, as for ``plan write``).
- ``{"mode": "poll", "cwds": [...], "out", "stop": path, "summary"?: bool}``: until *stop* exists,
  run ``list --json`` from each directory in turn and record when it started
  (``t0``) and returned (``t``) and every task's ``last_event_id`` (with
  ``summary``, only how many tasks it listed), plus any stderr notice (a
  catch-up that could not reach the server, or found it busy, says so there).

``TORTURE_HOST`` replaces ``socket.gethostname()`` (the reported host, SPEC §4).
"""

from __future__ import annotations

import json
import os
import socket
import sys
import time
from pathlib import Path

if os.environ.get("TORTURE_HOST"):
    _host = os.environ["TORTURE_HOST"]
    socket.gethostname = lambda: _host  # type: ignore[assignment]

from click.testing import CliRunner  # noqa: E402

from lattice.cli.main import cli  # noqa: E402


def _invoke(args: list[str], input: str | None = None) -> tuple[int, str, str]:
    result = CliRunner().invoke(cli, args, input=input, catch_exceptions=True)
    stderr = result.stderr
    if result.exception is not None and not isinstance(result.exception, SystemExit):
        stderr += f"\n{type(result.exception).__name__}: {result.exception}"
    return result.exit_code, result.stdout, stderr


def _snapshot(out: str) -> dict | None:
    try:
        data = json.loads(out).get("data")
    except (ValueError, AttributeError):
        return None
    if isinstance(data, dict) and "last_event_id" in data:
        return data
    return None


def _task_arg(args: list[str]) -> str | None:
    """The short ID a step writes to (``status T``, ``comment T``, ``plan write T``...)."""
    for arg in args:
        if arg[:1].isalpha() and "-" in arg and arg.split("-")[-1].isdigit():
            return arg
    return None


def write(spec: dict) -> None:
    os.chdir(spec["cwd"])
    if spec.get("start_at"):
        time.sleep(max(0.0, spec["start_at"] - time.time()))
    saved: dict[str, str] = {}
    with open(spec["out"], "a", encoding="utf-8") as fh:
        for n, step in enumerate(spec["steps"]):
            args = [arg.format(**saved) for arg in step["args"]]
            code, out, err = _invoke(args, step.get("input"))
            t = time.time()
            record: dict = {"i": n, "args": args, "exit": code, "t": t}
            if code != 0:
                record["output"] = (out + err)[-2000:]
            else:
                snap = _snapshot(out)
                task = snap.get("short_id") if snap else _task_arg(args)
                if snap is None and task:
                    shown = _invoke(["show", task, "--json"])
                    snap = _snapshot(shown[1])
                if snap is not None:
                    record["task"] = snap.get("short_id") or task
                    record["last_event_id"] = snap["last_event_id"]
                    if step.get("save"):
                        saved[step["save"]] = record["task"]
            fh.write(json.dumps(record) + "\n")
            fh.flush()


def poll(spec: dict) -> None:
    stop = Path(spec["stop"])
    with open(spec["out"], "a", encoding="utf-8") as fh:
        while not stop.exists():
            for cwd in spec["cwds"]:
                os.chdir(cwd)
                started = time.time()
                code, out, err = _invoke(["list", "--json"])
                t = time.time()
                if code != 0:
                    fh.write(json.dumps({"cwd": cwd, "t": t, "error": (out + err)[-500:]}) + "\n")
                    continue
                tasks = {
                    row.get("short_id") or row["id"]: row.get("last_event_id")
                    for row in json.loads(out)["data"]
                }
                row: dict = {"cwd": cwd, "t0": started, "t": t}
                if err.strip():
                    row["notice"] = err.strip()[-500:]  # e.g. "cannot reach", "busy"
                if spec.get("summary"):
                    row["count"] = len(tasks)
                else:
                    row["tasks"] = tasks
                fh.write(json.dumps(row) + "\n")
                fh.flush()


if __name__ == "__main__":
    spec = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
    {"write": write, "poll": poll}[spec["mode"]](spec)
