"""Local and hosted claims agree when a plan review is live or resolved."""

from __future__ import annotations

import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from lattice.boards import resolve_board
from lattice.cli.main import cli
from lattice.ops import Caller
from tests.parity.corpus import Scenario
from tests.parity.hosted import ParityServer, hosted_target
from tests.parity.record import (
    LocalTarget,
    Normalizer,
    _base_env,
    _chdir,
    _process_env,
    _runner,
)

TASK_ID = "task_01JAAAAAAAAAAAAAAAAAAAAAAA"
ACTOR = "agent:worker"
PLAN = "# Plan\n\nA complete plan for this parity check.\n"
REVIEW_TIMEOUT_SECONDS = 600


@pytest.mark.parametrize(
    ("local_status", "local_owner", "hosted_age_seconds", "expected_claimed"),
    [
        ("running", "live", 0, False),
        ("done", "live", REVIEW_TIMEOUT_SECONDS + 1, True),
        ("abandoned", "live", REVIEW_TIMEOUT_SECONDS + 1, True),
        ("running", "dead", REVIEW_TIMEOUT_SECONDS + 1, True),
    ],
    ids=("live", "done", "abandoned", "dead-owner"),
)
def test_local_review_state_and_hosted_event_gate_have_identical_public_results(
    server: ParityServer,
    tmp_path: Path,
    local_status: str,
    local_owner: str,
    hosted_age_seconds: int,
    expected_claimed: bool,
) -> None:
    """The local runtime record and hosted event log make the same claim decision."""
    scenario = Scenario("next-claim-review-gate", "", ())
    local_root = tmp_path / "local"
    local_root.mkdir()
    local_env = _base_env(local_root)
    runner = _runner()

    def init_local(args: list[str]) -> dict[str, object]:
        with _chdir(local_root), _process_env(local_env):
            result = runner.invoke(cli, args, env=local_env)
        return {"exit_code": result.exit_code, "stderr": result.stderr}

    initialized = LocalTarget().setup(scenario, local_root, init_local)
    assert initialized["exit_code"] == 0, initialized

    remote_checkout = tmp_path / "hosted"
    remote_checkout.mkdir()
    remote_target = hosted_target(server, scenario)
    hosted_env = {**_base_env(remote_checkout), **remote_target.env(remote_checkout)}
    remote_target.setup(scenario, remote_checkout, None)

    task_params = {"id": TASK_ID, "title": "Plan review parity", "status": "in_planning"}
    with _process_env(local_env):
        local_review_board = resolve_board(local_root)
        local_review_board.execute("task.create", task_params, Caller(actor="human:parity"))
        local_review_board.execute(
            "task.plan_write", {"task": TASK_ID, "stdin": PLAN}, Caller(actor="human:parity")
        )
        local_review_board.execute(
            "task.status", {"task": TASK_ID, "new_status": "planned"}, Caller(actor="human:parity")
        )
        local_review_board.execute(
            "task.assign", {"task": TASK_ID, "actor_id": "none"}, Caller(actor="human:parity")
        )

    now = datetime.now(timezone.utc)
    hosted_spawned_at = now - timedelta(seconds=hosted_age_seconds)
    with _process_env(hosted_env):
        hosted_review_board = resolve_board(remote_checkout)
        hosted_review_board.execute("task.create", task_params, Caller(actor="human:parity"))
        hosted_review_board.execute(
            "task.plan_write", {"task": TASK_ID, "stdin": PLAN}, Caller(actor="human:parity")
        )
        planned = hosted_review_board.execute(
            "task.status", {"task": TASK_ID, "new_status": "planned"}, Caller(actor="human:parity")
        )
        hosted_review_board.execute(
            "task.assign", {"task": TASK_ID, "actor_id": "none"}, Caller(actor="human:parity")
        )
        task_events = hosted_review_board.lattice_dir / "events" / f"{TASK_ID}.jsonl"
        spawned = hosted_review_board.execute(
            "task.record_auto_review",
            {
                "task": TASK_ID,
                "review_type": "plan-review",
                "mode": "single",
                "log_path": ".lattice/.daemon/auto-plan-review-parity.log",
                "spawned_at": hosted_spawned_at.isoformat(),
                "pid": os.getpid(),
                "trigger_status_event_id": planned.events[-1]["id"],
            },
            Caller(
                actor="agent:lattice-auto-review",
                origin={"reported": {"host": "separate-plan-review-host"}},
            ),
        )

    local_record_path = local_review_board.lattice_dir / "review_state" / f"{TASK_ID}.json"
    local_record_path.parent.mkdir(exist_ok=True)
    local_record_path.write_text(
        json.dumps(
            {
                "task_id": TASK_ID,
                "review_type": "plan-review",
                "status": local_status,
                "started_by_pid": (os.getpid() if local_owner == "live" else 2_000_000_000),
            }
        )
    )
    assert spawned.events[-1]["type"] == "auto_review_spawned"
    assert not (hosted_review_board.lattice_dir / "review_state" / f"{TASK_ID}.json").exists()
    local_events_before = (
        local_review_board.lattice_dir / "events" / f"{TASK_ID}.jsonl"
    ).read_bytes()
    hosted_events_before = task_events.read_bytes()

    def next_claim(root: Path, env: dict[str, str | None]) -> dict:
        with _chdir(root), _process_env(env):
            result = runner.invoke(
                cli,
                ["next", "--status", "planned", "--actor", ACTOR, "--claim", "--json"],
                env=env,
                catch_exceptions=False,
            )
        assert result.exit_code == 0, result.output
        return json.loads(result.stdout)

    local_result = next_claim(local_root, local_env)
    hosted_result = next_claim(remote_checkout, hosted_env)

    assert Normalizer([str(local_root)]).obj(local_result) == Normalizer(
        [str(remote_checkout)]
    ).obj(hosted_result)
    data = local_result["data"]
    if expected_claimed:
        assert data["status"] == "in_progress"
        assert data["assigned_to"] == ACTOR
        assert "claimed" not in data
    else:
        assert data["claimed"] is False
        assert data["reason"] == "PLAN_REVIEW_IN_FLIGHT"
        assert data["task"]["status"] == "planned"
        assert data["task"]["assigned_to"] is None
    local_events_after = (
        local_review_board.lattice_dir / "events" / f"{TASK_ID}.jsonl"
    ).read_bytes()
    hosted_events_after = task_events.read_bytes()
    if expected_claimed:
        assert local_events_after != local_events_before
        assert hosted_events_after != hosted_events_before
    else:
        assert local_events_after == local_events_before
        assert hosted_events_after == hosted_events_before
