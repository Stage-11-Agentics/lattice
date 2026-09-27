"""G-2 (hosted, H-12): no deletion of board data on a hosted board beyond what
SPEC §7 permits: archive and session relocation (copy first), and rollback of
an uncommitted operation, which removes only what that operation wrote.
``doctor --fix`` on a bound checkout is ``LOCAL_ONLY``. The whole parity corpus
carries the same recorder assertion (``tests/parity/test_hosted_parity.py``).
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from lattice.ops.base import CommonParams, OpContext, OpResult, operation
from lattice.storage.fs import atomic_write, jsonl_append
from tests.parity.hosted import durable_tree, forbidden_removals, recording_mutations
from tests.test_remote.hosted import HostedEnv, hosted_env, make_repo, run_cli  # noqa: F401


@dataclass(frozen=True, kw_only=True)
class FailParams(CommonParams):
    task: str


@operation("xtest.write_then_fail")
class WriteThenFail:
    """Writes a new file and appends to an existing log, then fails: the server
    must roll both back (SPEC §8.6)."""

    Params = FailParams

    def run(self, ctx: OpContext, p: FailParams) -> OpResult:
        atomic_write(ctx.lattice_dir / "notes" / "rolled-back.md", "never kept\n")
        jsonl_append(ctx.lattice_dir / "events" / f"{p.task}.jsonl", '{"torn": true}\n')
        raise RuntimeError("fail after writing")


def test_only_permitted_removals_on_a_hosted_board(
    hosted_env: HostedEnv,  # noqa: F811
    tmp_path: Path,
) -> None:
    repo = make_repo(tmp_path / "repo")
    assert run_cli(repo, "remote", "attach", "team", "demo").exit_code == 0
    with recording_mutations() as log:
        me = ("--actor", "agent:dev")
        created = run_cli(repo, "create", "Moved around", *me, "--json")
        task_id = json.loads(created.stdout)["data"]["id"]
        for args in (
            ("comment", "DEM-1", "hi", *me),
            ("archive", "DEM-1", *me),
            ("unarchive", "DEM-1", *me),
            ("session", "start", "--name", "Argus", "--model", "m", "--framework", "f"),
            ("session", "end", "Argus-1"),
            ("erase", "DEM-1", "--reason", "gone", *me),
        ):
            result = run_cli(repo, *args)
            assert result.exit_code == 0, (args, result.output)

        # A failed operation is rolled back: the board is exactly as before.
        before = durable_tree(hosted_env.board)
        status, _, body = hosted_env.handle.op(  # type: ignore[union-attr]
            "demo",
            "xtest.write_then_fail",
            {"task": task_id},
            token=hosted_env.token,
            actor="agent:dev",
        )
        assert status == 500, body
        assert durable_tree(hosted_env.board) == before

    mutations = [m for m in log.entries if m.project == "demo"]
    removals = [m for m in mutations if m.kind == "unlink"]
    # Not vacuous: archive, unarchive, and session end each relocated files.
    assert {m.op for m in removals} >= {"task.archive", "task.unarchive", "session.end"}
    assert forbidden_removals(mutations, hosted_env.board) == []
    # The erased task keeps every file (SPEC §7).
    assert (hosted_env.board / "events" / f"{task_id}.jsonl").is_file()
    assert (hosted_env.board / "tasks" / f"{task_id}.json").is_file()


def test_doctor_fix_on_a_bound_checkout_is_local_only(
    hosted_env: HostedEnv,  # noqa: F811
    tmp_path: Path,
) -> None:
    repo = make_repo(tmp_path / "repo")
    assert run_cli(repo, "remote", "attach", "team", "demo").exit_code == 0
    assert run_cli(repo, "create", "Kept", "--actor", "agent:dev").exit_code == 0
    before = durable_tree(repo / ".lattice")
    result = run_cli(repo, "doctor", "--fix", "--json")
    assert result.exit_code == 1
    assert json.loads(result.stdout)["error"]["code"] == "LOCAL_ONLY"
    assert durable_tree(repo / ".lattice") == before
