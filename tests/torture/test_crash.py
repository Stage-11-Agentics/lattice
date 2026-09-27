"""AC-4 (H-15 row): SIGKILL iterations of a mixed-operation writer loop against a
server subprocess. Every acknowledged ``op_id`` is present in full, every
operation the client never heard back about is wholly present or wholly absent,
and doctor finds nothing.

Two writer threads post ``task.create``, ``comment``, ``status``, ``plan_write``,
``assign`` and ``archive`` over HTTP, each call with its own ``op_id``. The killer
SIGKILLs the server only while a transaction is provably in flight: it watches
``hosted/undo/`` for an undo log (whose name carries the ``op_id``, SPEC §8.6),
then kills either at once (**pre-commit**) or once the journal has grown while
that undo log still exists (**post-commit**: the commit point passed, the
response had not). Before killing it tells the writer what to do with that
``op_id``: **retry** it after the restart (as the client does) or **abandon** it
and settle it through op status. The four combinations cycle, and the loop runs
until every case has happened for real:

- *replayed*: a post-commit kill retried, answered with ``replayed: true``;
- *reapplied*: a pre-commit kill retried, rolled back and applied fresh;
- *absent*: a pre-commit kill abandoned, ``not_found`` in op status;
- *committed unheard*: a post-commit kill abandoned, ``committed`` in op status;
- every operation family both acknowledged and caught in flight by a kill.

Then, after a last SIGKILL and restart: op status agrees with the ledger for every
``op_id``; the board's events are exactly the committed operations' events;
each plan file holds the text of the plan write with the highest committed
``seq`` (and no uncommitted text anywhere); every snapshot is byte-identical to
a ``lattice rebuild --all`` of the event logs; no undo log remains; and
``server project doctor`` reports no finding at all.

The per-PR lane stops as soon as every case is covered (at least 6 kills); the
envelope lane runs at least EVALUATION's 25.
"""

from __future__ import annotations

import json
import random
import shutil
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

import pytest

from lattice.core.ids import generate_op_id
from lattice.server.testing import make_root
from tests.torture.harness import CLI_SHIM, PROJECT, ServerProcess, base_env, board_events

QUICK_KILLS = 6
ENVELOPE_KILLS = 25
MAX_KILLS = 120
WRITERS = 2
RETRYABLE = {429, 502, 503}
FAMILIES = (
    "task.create",
    "task.comment",
    "task.status",
    "task.plan_write",
    "task.assign",
    "task.archive",
)
#: (moment, what the writer does with the killed op_id), cycled.
MODES = (("pre", "abandon"), ("post", "retry"), ("pre", "retry"), ("post", "abandon"))


@dataclass
class Ledger:
    """What the writers and the killer know about each operation."""

    requests: dict[str, dict] = field(default_factory=dict)  # op_id -> {op, params}
    acked: dict[str, dict] = field(default_factory=dict)  # op_id -> result
    unknown: set[str] = field(default_factory=set)  # abandoned without an answer
    rejected: dict[str, dict] = field(default_factory=dict)  # op_id -> error envelope
    decisions: dict[str, str] = field(default_factory=dict)  # op_id -> retry | abandon
    killed: dict[str, str] = field(default_factory=dict)  # op_id in flight -> moment
    tasks: list[str] = field(default_factory=list)  # live (unarchived) short IDs
    lock: threading.Lock = field(default_factory=threading.Lock)


def _pick(rng: random.Random, ledger: Ledger, n: int, k: int) -> tuple[str, dict]:
    with ledger.lock:
        tasks = list(ledger.tasks)
    family = "task.create" if len(tasks) < 4 else rng.choice(FAMILIES)
    if family == "task.create":
        return family, {"title": f"w{n} task {k}"}
    task = rng.choice(tasks)
    if family == "task.comment":
        return family, {"task": task, "text": f"w{n} comment {k}"}
    if family == "task.status":
        return family, {"task": task, "new_status": rng.choice(["in_planning", "backlog"])}
    if family == "task.plan_write":
        return family, {"task": task, "file": f"# plan {task}\n\nw{n} v{k}\n"}
    if family == "task.assign":
        return family, {"task": task, "actor_id": f"agent:w{rng.randrange(3)}"}
    return family, {"task": task}


def _writer(
    server: ServerProcess, token: str, ledger: Ledger, n: int, stop: threading.Event
) -> None:
    rng = random.Random(n)
    k = 0
    while not stop.is_set():
        k += 1
        op, params = _pick(rng, ledger, n, k)
        op_id = generate_op_id()
        with ledger.lock:
            ledger.requests[op_id] = {"op": op, "params": params}
        while True:
            try:
                status, body = server.op(op, params, token=token, actor=f"agent:w{n}", op_id=op_id)
            except OSError:
                status, body = None, None
            if status == 200:
                result = body["data"]["result"]
                with ledger.lock:
                    ledger.acked[op_id] = result
                    if op == "task.create":
                        ledger.tasks.append(result["task"]["short_id"])
                    elif op == "task.archive" and params["task"] in ledger.tasks:
                        ledger.tasks.remove(params["task"])
                break
            if status is not None and status not in RETRYABLE:
                with ledger.lock:
                    ledger.rejected[op_id] = body
                break
            # No answer (the server died under it) or it is starting up.
            with ledger.lock:
                abandon = ledger.decisions.get(op_id) == "abandon"
                if abandon:
                    ledger.unknown.add(op_id)
            if abandon or stop.is_set():
                if not abandon:
                    with ledger.lock:
                        ledger.unknown.add(op_id)
                break
            time.sleep(0.05)


def _in_flight(undo_dir: Path) -> str | None:
    """The ``op_id`` of an undo log that exists right now, if any."""
    try:
        for entry in undo_dir.iterdir():
            name = entry.name
            if name.endswith(".jsonl") and "--" in name:
                return name[: -len(".jsonl")].split("--", 1)[1]
    except FileNotFoundError:
        pass
    return None


def _size(path: Path) -> int:
    try:
        return path.stat().st_size
    except FileNotFoundError:
        return 0


def _kill_in_flight(server: ServerProcess, ledger: Ledger, moment: str, decision: str) -> str:
    """SIGKILL the server while an operation's transaction is open; return its op_id."""
    board = server.board()
    undo_dir, journal = board / "hosted" / "undo", board / "hosted" / "journal.jsonl"
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        op_id = _in_flight(undo_dir)
        if op_id is None:
            time.sleep(0.0005)
            continue
        if moment == "post":
            base = _size(journal)
            undo = next(undo_dir.glob(f"*--{op_id}.jsonl"), None)
            while undo is not None and undo.exists() and _size(journal) == base:
                if time.monotonic() > deadline:
                    break
                time.sleep(0)  # yield, but watch closely: the window is short
            if undo is None or not undo.exists():
                continue  # finished before we saw the commit; wait for the next one
        with ledger.lock:
            ledger.decisions[op_id] = decision
            ledger.killed[op_id] = moment
        server.kill()
        return op_id
    raise AssertionError(f"no transaction seen in flight for 30 s ({moment})")


@dataclass
class Coverage:
    replayed: int = 0
    reapplied: int = 0
    absent: int = 0
    committed_unheard: int = 0
    families_killed: set[str] = field(default_factory=set)
    families_acked: set[str] = field(default_factory=set)

    def complete(self) -> bool:
        return (
            min(self.replayed, self.reapplied, self.absent, self.committed_unheard) > 0
            and self.families_killed >= set(FAMILIES)
            and self.families_acked >= set(FAMILIES)
        )


def _coverage(server: ServerProcess, token: str, ledger: Ledger, settled: dict) -> Coverage:
    cov = Coverage()
    with ledger.lock:
        killed = dict(ledger.killed)
        acked = dict(ledger.acked)
        unknown = set(ledger.unknown)
        requests = dict(ledger.requests)
    for op_id, result in acked.items():
        cov.families_acked.add(requests[op_id]["op"])
    for op_id, moment in killed.items():
        cov.families_killed.add(requests[op_id]["op"])
        if op_id in acked:
            if acked[op_id].get("replayed"):
                cov.replayed += 1
            elif moment == "pre":
                cov.reapplied += 1
        elif op_id in unknown:
            if op_id not in settled:
                settled[op_id] = server.op_status(op_id, token=token)["state"]
            state = settled[op_id]
            if state == "not_found":
                cov.absent += 1
            elif state == "committed":
                cov.committed_unheard += 1
    return cov


def _run(tmp_path: Path, min_kills: int) -> None:
    server = ServerProcess(make_root(tmp_path, projects={PROJECT: {"code": "DEM"}}))
    server.start()
    token = server.mint(user="human:alice", machine="laptop")
    ledger = Ledger()
    stop = threading.Event()
    writers = [
        threading.Thread(target=_writer, args=(server, token, ledger, n, stop), daemon=True)
        for n in range(WRITERS)
    ]
    settled: dict[str, str] = {}
    kills = 0
    try:
        for thread in writers:
            thread.start()
        cov = Coverage()
        while kills < min_kills or not cov.complete():
            assert kills < MAX_KILLS, f"coverage incomplete after {kills} kills: {cov}"
            moment, decision = MODES[kills % len(MODES)]
            _kill_in_flight(server, ledger, moment, decision)
            kills += 1
            server.start()
            time.sleep(0.3)  # let retries land before counting
            cov = _coverage(server, token, ledger, settled)
        stop.set()
        for thread in writers:
            thread.join(timeout=60)
        assert not any(t.is_alive() for t in writers), "a writer hung"
        server.kill()  # a last crash with nothing in flight, then a clean load
        server.start()
        print(f"kills={kills} {cov} acked={len(ledger.acked)} rejected={len(ledger.rejected)}")
        _verify(server, token, ledger, tmp_path)
    finally:
        stop.set()
        server.stop()


def _verify(server: ServerProcess, token: str, ledger: Ledger, tmp_path: Path) -> None:
    # Op status agrees with the ledger for every op_id the writers ever sent.
    committed: dict[str, dict] = {}
    for op_id, request in ledger.requests.items():
        state = server.op_status(op_id, token=token)
        if op_id in ledger.acked:
            assert state["state"] == "committed", (op_id, request, state)
        elif op_id in ledger.rejected:
            assert state["state"] == "not_found", (op_id, request, state)
        else:
            assert op_id in ledger.unknown, (op_id, request)
            assert state["state"] in ("committed", "not_found"), (op_id, state)
        if state["state"] == "committed":
            committed[op_id] = state
    seqs = [(s["epoch"], s["seq"]) for s in committed.values()]
    assert len(set(seqs)) == len(seqs), "two operations share a journal seq"

    # The board's events are exactly the committed operations' events, in full.
    events = board_events(server.board())
    by_id: dict[str, dict] = {}
    by_op: dict[str | None, list[dict]] = {}
    for event in events:
        assert event["id"] not in by_id, f"duplicate event {event['id']}"
        by_id[event["id"]] = event
        by_op.setdefault(event.get("origin", {}).get("op_id"), []).append(event)
    assert set(by_op) - {None} <= set(committed), set(by_op) - set(committed)
    for op_id, state in committed.items():
        result = ledger.acked.get(op_id) or state.get("result") or {}
        expected = result.get("events") or []
        for event in expected:
            assert by_id.get(event["id"]) == event, (op_id, event)
        assert len(by_op.get(op_id, [])) == len(expected), (op_id, by_op.get(op_id))

    # Each plan file holds the plan write with the highest committed seq, and no
    # text of a plan write that did not commit survives anywhere.
    board = server.board()
    task_ids = json.loads((board / "ids.json").read_text())["map"]
    last: dict[str, tuple[tuple[str, int], str]] = {}
    written: set[str] = set()
    for op_id, request in ledger.requests.items():
        if request["op"] != "task.plan_write":
            continue
        written.add(request["params"]["file"])
        if op_id in committed:
            key = (committed[op_id]["epoch"], committed[op_id]["seq"])
            task = request["params"]["task"]
            if task not in last or key > last[task][0]:
                last[task] = (key, request["params"]["file"])
    committed_texts = {text for _, text in last.values()}
    for short_id, task_id in task_ids.items():
        path = board / "plans" / f"{task_id}.md"
        if not path.exists():
            path = board / "archive" / "plans" / f"{task_id}.md"
        text = path.read_text() if path.exists() else None
        if short_id in last:
            assert text == last[short_id][1], short_id
        else:
            assert text not in written, (short_id, text)
    for path in [*board.glob("plans/*.md"), *board.glob("archive/plans/*.md")]:
        text = path.read_text()
        assert text not in written or text in committed_texts, path

    # Every snapshot is what the event logs rebuild to.
    copy = tmp_path / "rebuilt"
    shutil.copytree(board, copy / ".lattice", ignore=shutil.ignore_patterns("hosted", "locks"))
    rebuild = subprocess.run(
        [sys.executable, "-c", CLI_SHIM, "rebuild", "--all", "--json"],
        cwd=copy,
        env={**base_env(), "LATTICE_ROOT": str(copy)},
        capture_output=True,
        text=True,
        timeout=300,
    )
    assert rebuild.returncode == 0, rebuild.stdout + rebuild.stderr
    for rel in ("tasks", "archive/tasks"):
        for path in (board / rel).glob("*.json"):
            rebuilt = copy / ".lattice" / rel / path.name
            assert json.loads(path.read_text()) == json.loads(rebuilt.read_text()), path

    undo = board / "hosted" / "undo"
    assert not undo.exists() or not any(undo.iterdir()), sorted(undo.iterdir())
    _assert_doctor_silent(server)


def _assert_doctor_silent(server: ServerProcess) -> None:
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
    assert report["data"]["findings"] == [], report["data"]["findings"]
    assert proc.returncode == 0, proc.stdout + proc.stderr


@pytest.mark.torture
@pytest.mark.timeout(600)
def test_sigkill_loop(tmp_path: Path) -> None:
    _run(tmp_path, QUICK_KILLS)


@pytest.mark.torture
@pytest.mark.envelope
@pytest.mark.timeout(1800)
def test_sigkill_loop_full(tmp_path: Path) -> None:
    _run(tmp_path, ENVELOPE_KILLS)
