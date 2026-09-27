"""AC-46 (H-11): ``lattice remote op-status`` renders ``committed``,
``in_flight``, and ``not_found``, plain and ``--json``, exiting 0 for all
three; ``in_flight`` never advises a rerun (SPEC §9.2)."""

from __future__ import annotations

import json
import threading
from pathlib import Path

import pytest

from lattice.core.ids import generate_op_id
from lattice.server import transactions
from tests.test_remote.hosted import PROJECT, HostedEnv, make_repo, run_cli


@pytest.fixture()
def repo(hosted_env: HostedEnv, tmp_path: Path) -> Path:
    repo = make_repo(tmp_path / "repo")
    assert run_cli(repo, "remote", "attach", "team", "demo").exit_code == 0
    return repo


def _both(repo: Path, op_id: str) -> tuple[str, dict]:
    plain = run_cli(repo, "remote", "op-status", op_id)
    as_json = run_cli(repo, "remote", "op-status", op_id, "--json")
    assert plain.exit_code == 0, plain.output
    assert as_json.exit_code == 0, as_json.output
    return plain.stdout, json.loads(as_json.stdout)["data"]


def test_committed(hosted_env: HostedEnv, repo: Path) -> None:
    op_id = generate_op_id()
    hosted_env.server_op("task.create", {"title": "Done"}, actor="human:alice", op_id=op_id)
    plain, data = _both(repo, op_id)
    assert data["state"] == "committed" and data["op_id"] == op_id
    assert f"{op_id}: committed (epoch {data['epoch']}, seq {data['seq']})" in plain


def test_in_flight_never_advises_a_rerun(
    hosted_env: HostedEnv, repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    paused, release = threading.Event(), threading.Event()

    def fault(point: str, **ctx: object) -> None:
        if point == "journal.write" and not paused.is_set():
            paused.set()
            release.wait(10)

    monkeypatch.setattr(transactions, "_fault", fault)
    op_id = generate_op_id()
    assert hosted_env.handle is not None
    thread = threading.Thread(
        target=hosted_env.handle.op,
        args=(PROJECT, "task.create", {"title": "Pending"}),
        kwargs={"token": hosted_env.token, "op_id": op_id, "actor": "human:alice"},
        daemon=True,
    )
    thread.start()
    try:
        assert paused.wait(5)
        plain, data = _both(repo, op_id)
    finally:
        release.set()
        thread.join(10)
    assert data == {"op_id": op_id, "state": "in_flight"}
    assert "still applying this write" in plain
    assert f"lattice remote op-status {op_id}" in plain
    assert "again applies" not in plain and "rerun" not in plain.lower()


def test_not_found(repo: Path) -> None:
    op_id = generate_op_id()
    plain, data = _both(repo, op_id)
    assert data == {"op_id": op_id, "state": "not_found"}
    assert "did not apply" in plain
    assert "as a new operation with a new op_id" in plain
