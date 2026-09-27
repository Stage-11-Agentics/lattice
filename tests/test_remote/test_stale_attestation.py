"""Orchestrator ruling 1 (SPEC §3.4): on a hosted checkout, a stale attestation
re-syncs the cache (``HostedBoard.refresh``) before the recompute and the one
retry, which is a new operation call with a new ``op_id``."""

from __future__ import annotations

from pathlib import Path

import click
import pytest

from lattice.boards import HostedBoard, resolve_board
from lattice.cli.ops_bridge import run_attested_operation
from lattice.core.attestations import STALE_ATTESTATION
from lattice.core.errors import OpError
from lattice.remote import cache, client
from tests.test_remote.hosted import HostedEnv, make_repo, run_cli


def test_stale_attestation_resyncs_before_the_retry(
    hosted_env: HostedEnv, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = make_repo(tmp_path / "repo")
    assert run_cli(repo, "remote", "attach", "team", "demo").exit_code == 0
    assert run_cli(repo, "create", "Attested", "--actor", "agent:dev").exit_code == 0
    monkeypatch.chdir(repo)
    board = resolve_board()
    assert isinstance(board, HostedBoard)

    trail: list[str] = []
    real_catch_up = cache.catch_up
    real_post = client.post_operation
    op_ids: list[str] = []

    def catch_up(root: Path, *, bulk: bool = False) -> cache.SyncOutcome:
        trail.append("sync")
        return real_catch_up(root, bulk=bulk)

    def post(remote, project, op_name, body, **kwargs):  # noqa: ANN001, ANN003, ANN202
        trail.append("post")
        op_ids.append(body["op_id"])
        if len(op_ids) == 1:
            raise OpError(
                "COMPLETION_BLOCKED",
                "attestation is stale",
                {"reason": STALE_ATTESTATION},
            )
        return real_post(remote, project, op_name, body, **kwargs)

    def attest() -> dict:
        trail.append("attest")
        return {}

    monkeypatch.setattr(cache, "catch_up", catch_up)
    monkeypatch.setattr(client, "post_operation", post)
    with click.Context(click.Command("status")) as ctx:
        ctx.obj = {"_actor": "agent:dev"}
        result = run_attested_operation(
            "task.status",
            {"task": "DEM-1", "new_status": "in_planning"},
            False,
            board=board,
            attest=attest,
        )
    assert result.events[-1]["data"]["to"] == "in_planning"
    assert trail == ["attest", "post", "sync", "attest", "post", "sync"]
    assert len(set(op_ids)) == 2
