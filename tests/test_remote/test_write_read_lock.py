"""SPEC §9.4: a write command's read phase holds the cache's shared read lock
from its first read until the request is sent, so a sync can never interleave
its reads (review round 1); the lock is never held across the network call."""

from __future__ import annotations

import fcntl
import os
from pathlib import Path

import click
import pytest

from lattice.boards import HostedBoard, resolve_board
from lattice.cli.attestations import completion_attestations
from lattice.cli.ops_bridge import run_attested_operation
from lattice.core.errors import OpError
from lattice.remote import client
from tests.test_remote.hosted import HostedEnv, make_repo, run_cli


def _sync_could_apply(repo: Path) -> bool:
    """Whether a sync could take the cache's apply lock right now."""
    fd = os.open(repo / ".lattice" / "locks" / "cache_rw.lock", os.O_RDWR)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        return False
    else:
        fcntl.flock(fd, fcntl.LOCK_UN)
        return True
    finally:
        os.close(fd)


def test_attestation_reads_hold_the_lock_until_the_post(
    hosted_env: HostedEnv, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = make_repo(tmp_path / "repo")
    assert run_cli(repo, "remote", "attach", "team", "demo").exit_code == 0
    assert run_cli(repo, "create", "Locked", "--actor", "agent:dev").exit_code == 0
    monkeypatch.chdir(repo)
    board = resolve_board()
    assert isinstance(board, HostedBoard)
    seen: list[tuple[str, bool]] = []
    real_post = client.post_operation

    def post(remote, project, op_name, body, **kwargs):  # noqa: ANN001, ANN003, ANN202
        seen.append(("post", _sync_could_apply(repo)))
        return real_post(remote, project, op_name, body, **kwargs)

    monkeypatch.setattr(client, "post_operation", post)

    config = board.load_config()
    seen.append(("after load_config", _sync_could_apply(repo)))

    def attest() -> dict:
        result = completion_attestations(board, config, "DEM-1", "in_planning")
        seen.append(("attest", _sync_could_apply(repo)))
        return result

    with click.Context(click.Command("status")) as ctx:
        ctx.obj = {"_actor": "agent:dev"}
        try:
            run_attested_operation(
                "task.status",
                {"task": "DEM-1", "new_status": "in_planning"},
                False,
                board=board,
                attest=attest,
                config=config,
            )
        except (SystemExit, OpError):
            pass
    assert seen == [("after load_config", False), ("attest", False), ("post", True)]
    # After the post-write sync, the next read takes the lock again.
    _ = board.lattice_dir
    assert not _sync_could_apply(repo)
