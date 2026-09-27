"""AC-4 (H-15 row): SIGKILL iterations of a mixed-operation writer loop against a
server subprocess. Every acknowledged ``op_id`` is present in full, every
operation the client never heard back about is wholly present or wholly absent,
and doctor is clean.

Two writer threads post ``task.create``, ``comment``, ``status``, ``plan_write``,
``assign`` and ``archive`` over HTTP, each with its own ``op_id``. A killer
SIGKILLs the server at a random moment, then restarts it on the same port. A
writer whose request died retries it with the same ``op_id`` (as the client
does, SPEC §8.6) or, one time in three, abandons it, so both "retried after a
crash" and "never heard back" are exercised.

The per-PR lane runs a few iterations; the envelope lane runs EVALUATION's 25.
"""

from __future__ import annotations

import json
import random
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

import pytest

from lattice.core.ids import generate_op_id
from lattice.server.testing import make_root
from tests.torture.harness import (
    PROJECT,
    ServerProcess,
    base_env,
    board_events,
    CLI_SHIM,
)

ENVELOPE_ITERATIONS = 25
QUICK_ITERATIONS = 4
WRITERS = 2
RETRYABLE = {429, 502, 503}


@dataclass
class Ledger:
    """What the writers know about each operation they sent."""

    acked: dict[str, dict] = field(default_factory=dict)  # op_id -> result
    unknown: dict[str, dict] = field(default_factory=dict)  # op_id -> request
    rejected: dict[str, dict] = field(default_factory=dict)  # op_id -> error envelope
    plans: dict[str, list[tuple[str, str]]] = field(default_factory=dict)  # task -> (op_id, text)
    tasks: list[str] = field(default_factory=list)
    lock: threading.Lock = field(default_factory=threading.Lock)


def _pick(rng: random.Random, ledger: Ledger, n: int, k: int) -> tuple[str, dict]:
    with ledger.lock:
        tasks = list(ledger.tasks)
    if len(tasks) < 3 or rng.random() < 0.2:
        return "task.create", {"title": f"w{n} task {k}"}
    task = rng.choice(tasks)
    roll = rng.random()
    if roll < 0.35:
        return "task.comment", {"task": task, "text": f"w{n} comment {k}"}
    if roll < 0.55:
        return "task.status", {"task": task, "new_status": rng.choice(["in_planning", "backlog"])}
    if roll < 0.75:
        return "task.plan_write", {"task": task, "file": f"# plan {task}\n\nw{n} v{k}\n"}
    if roll < 0.95:
        return "task.assign", {"task": task, "actor_id": f"agent:w{rng.randrange(3)}"}
    return "task.archive", {"task": task}


def _writer(
    server: ServerProcess, token: str, ledger: Ledger, n: int, stop: threading.Event
) -> None:
    rng = random.Random(n)
    k = 0
    while not stop.is_set():
        k += 1
        op, params = _pick(rng, ledger, n, k)
        op_id = generate_op_id()
        request = {"op": op, "params": params}
        attempts = 0
        while True:
            attempts += 1
            try:
                status, body = server.op(op, params, token=token, actor=f"agent:w{n}", op_id=op_id)
            except OSError:
                status, body = None, None
            if status == 200:
                result = body["data"]["result"]
                with ledger.lock:
                    ledger.unknown.pop(op_id, None)
                    ledger.acked[op_id] = {**request, "result": result}
                    if op == "task.create":
                        ledger.tasks.append(result["task"]["short_id"])
                    if op == "task.plan_write":
                        ledger.plans.setdefault(params["task"], []).append((op_id, params["file"]))
                break
            if status is not None and status not in RETRYABLE:
                with ledger.lock:
                    ledger.unknown.pop(op_id, None)
                    ledger.rejected[op_id] = {**request, "status": status, "error": body}
                break
            # No answer (the server died mid-request) or it is recovering.
            with ledger.lock:
                ledger.unknown[op_id] = request
                if op == "task.plan_write":
                    ledger.plans.setdefault(params["task"], []).append((op_id, params["file"]))
            if stop.is_set() or (attempts == 1 and status is None and rng.random() < 1 / 3):
                break  # abandoned: resolved through op status at the end
            time.sleep(0.1)


def _run(tmp_path: Path, iterations: int) -> None:
    server = ServerProcess(make_root(tmp_path, projects={PROJECT: {"code": "DEM"}}))
    server.start()
    token = server.mint(user="human:alice", machine="laptop")
    ledger = Ledger()
    stop = threading.Event()
    writers = [
        threading.Thread(target=_writer, args=(server, token, ledger, n, stop), daemon=True)
        for n in range(WRITERS)
    ]
    rng = random.Random(4)
    try:
        for thread in writers:
            thread.start()
        for _ in range(iterations):
            time.sleep(rng.uniform(0.3, 1.5))
            server.kill()
            time.sleep(rng.uniform(0.0, 0.3))
            server.start()
        time.sleep(1.0)  # let the last retries land on a live server
        stop.set()
        for thread in writers:
            thread.join(timeout=60)
        assert not any(t.is_alive() for t in writers), "a writer hung"
        server.kill()  # the last crash
        server.start()
        _verify(server, token, ledger, iterations)
    finally:
        stop.set()
        server.stop()


def _verify(server: ServerProcess, token: str, ledger: Ledger, iterations: int) -> None:
    assert len(ledger.acked) >= iterations, "the loop barely wrote anything"
    for op_id in list(ledger.unknown):
        state = server.op_status(op_id, token=token)
        if state["state"] == "committed":
            ledger.acked[op_id] = {**ledger.unknown.pop(op_id), "result": state.get("result")}
    committed_ids = set(ledger.acked)
    replayed = sum(1 for e in ledger.acked.values() if (e["result"] or {}).get("replayed"))
    print(
        f"acked={len(ledger.acked)} replayed={replayed} absent={len(ledger.unknown)} "
        f"rejected={len(ledger.rejected)} starts={server.starts}"
    )

    events = board_events(server.board())
    by_id: dict[str, dict] = {}
    for event in events:
        assert event["id"] not in by_id, f"duplicate event {event['id']}"
        by_id[event["id"]] = event
    by_op: dict[str, list[dict]] = {}
    for event in events:
        by_op.setdefault(event.get("origin", {}).get("op_id"), []).append(event)

    # Every acknowledged operation: committed per op status, every event in full.
    for op_id, entry in ledger.acked.items():
        assert server.op_status(op_id, token=token)["state"] == "committed", entry
        result = entry["result"] or {}
        for event in result.get("events") or []:
            assert by_id.get(event["id"]) == json.loads(json.dumps(event)), (op_id, event)
        expected = len(result.get("events") or [])
        assert len(by_op.get(op_id, [])) == expected, (op_id, entry["op"], by_op.get(op_id))
    # Operations never heard back and not committed are wholly absent; so are rejections.
    for op_id in [*ledger.unknown, *ledger.rejected]:
        assert server.op_status(op_id, token=token)["state"] == "not_found"
        assert op_id not in by_op, (op_id, by_op[op_id])
    assert set(by_op) - {None} <= committed_ids

    # Each plan file holds the last committed write to it.
    board = server.board()
    task_ids = json.loads((board / "ids.json").read_text())["map"]
    for short_id, writes in ledger.plans.items():
        committed = [text for op_id, text in writes if op_id in committed_ids]
        if not committed:
            continue
        task_id = task_ids[short_id]
        path = board / "plans" / f"{task_id}.md"
        if not path.exists():
            path = board / "archive" / "plans" / f"{task_id}.md"
        assert path.read_text() == committed[-1], short_id

    # No undo log survives recovery, and doctor is clean.
    undo = board / "hosted" / "undo"
    assert not undo.exists() or not any(undo.iterdir()), sorted(undo.iterdir())
    _assert_doctor_clean(server)


def _assert_doctor_clean(server: ServerProcess) -> None:
    import subprocess
    import sys

    proc = subprocess.run(
        [
            sys.executable,
            "-c",
            CLI_SHIM,
            "server",
            "project",
            "doctor",
            PROJECT,
            "--root",
            str(server.root),
            "--json",
        ],
        capture_output=True,
        text=True,
        timeout=120,
        env=base_env(),
    )
    report = json.loads(proc.stdout)
    assert report["ok"], proc.stdout + proc.stderr
    errors = [f for f in report["data"]["findings"] if f["level"] == "error"]
    assert not errors, errors
    assert proc.returncode == 0, proc.stdout + proc.stderr


@pytest.mark.torture
@pytest.mark.timeout(300)
def test_sigkill_loop(tmp_path: Path) -> None:
    _run(tmp_path, QUICK_ITERATIONS)


@pytest.mark.torture
@pytest.mark.envelope
@pytest.mark.timeout(900)
def test_sigkill_loop_full(tmp_path: Path) -> None:
    _run(tmp_path, ENVELOPE_ITERATIONS)
