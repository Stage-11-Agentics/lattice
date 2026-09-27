"""AC-2 (server part): 8 clients x 50 concurrent creates yield 400 distinct short IDs;
a restart with a regressed ``ids.json`` issues none of them again.

The clients are threads posting ``task.create`` over HTTP to a server subprocess,
each with its own token. The restart regresses ``ids.json`` two ways (counter
rewound, map emptied) and runs another storm. The envelope lane runs EVALUATION's
8 x 50; the per-PR lane runs 8 x 15, which keeps it fast on a busy disk.
"""

from __future__ import annotations

import json
import threading
from pathlib import Path

import pytest

from lattice.core.ids import generate_op_id
from lattice.server.testing import make_root
from tests.torture.harness import PROJECT, ServerProcess, board_events

pytestmark = [pytest.mark.torture, pytest.mark.timeout(600)]

CLIENTS = 8
CREATES = 50
QUICK_CREATES = 15


def _storm(server: ServerProcess, tokens: list[str], creates: int, label: str) -> list[str]:
    """Every client creates *creates* tasks at once; returns the short IDs issued."""
    issued: list[list[str]] = [[] for _ in tokens]
    errors: list[str] = []
    start = threading.Barrier(len(tokens))

    def client(n: int) -> None:
        start.wait()
        for k in range(creates):
            status, body = server.op(
                "task.create",
                {"title": f"{label} {n}-{k}"},
                token=tokens[n],
                actor=f"agent:storm-{n}",
                op_id=generate_op_id(),
            )
            if status != 200:
                errors.append(f"client {n} create {k}: {status} {body}")
                return
            issued[n].append(body["data"]["result"]["task"]["short_id"])

    threads = [threading.Thread(target=client, args=(n,)) for n in range(len(tokens))]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=240)
    assert not errors, errors[:5]
    return [short_id for batch in issued for short_id in batch]


def _regress(board: Path, how: str) -> None:
    path = board / "ids.json"
    index = json.loads(path.read_text())
    if how == "counter":
        index["next_seqs"] = {"DEM": 1}
    else:
        index = {"schema_version": index.get("schema_version", 2), "next_seqs": {}, "map": {}}
    path.write_text(json.dumps(index, indent=2, sort_keys=True) + "\n")


@pytest.mark.parametrize(
    "creates",
    [
        pytest.param(QUICK_CREATES, id="quick"),
        pytest.param(CREATES, id="full", marks=pytest.mark.envelope),
    ],
)
@pytest.mark.parametrize("how", ["counter", "emptied"])
def test_create_storm_then_regressed_restart(tmp_path: Path, how: str, creates: int) -> None:
    server = ServerProcess(make_root(tmp_path, projects={PROJECT: {"code": "DEM"}}))
    server.start()
    try:
        tokens = [server.mint(user=f"human:u{n}", machine=f"m{n}") for n in range(CLIENTS)]
        first = _storm(server, tokens, creates, "first")
        assert len(first) == CLIENTS * creates
        assert len(set(first)) == len(first), "a short ID was issued twice"

        server.stop()
        _regress(server.board(), how)
        server.start()
        second = _storm(server, tokens, max(5, creates // 5), "second")
        assert len(set(second)) == len(second)
        reissued = set(first) & set(second)
        assert not reissued, sorted(reissued)[:10]
    finally:
        server.stop()

    # Every issued ID names exactly one task in the logs.
    assigned: dict[str, set[str]] = {}
    for event in board_events(server.board()):
        short_id = event.get("data", {}).get("short_id")
        if event["type"] in ("task_created", "task_short_id_assigned") and short_id:
            assigned.setdefault(short_id, set()).add(event["task_id"])
    assert {k for k, v in assigned.items() if len(v) > 1} == set()
    assert set(first) | set(second) <= set(assigned)
