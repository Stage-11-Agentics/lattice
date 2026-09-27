"""AC-49 (H-11 part): a machine whose remote sets ``run_auto_reviews: false``
starts no review for a transition the board config would review, and says
why (SPEC §3.4). The hand-run review end to end is H-12's."""

from __future__ import annotations

import json
from pathlib import Path

from lattice.server import admin
from tests.test_remote.hosted import (
    HostedEnv,
    SpawnRecorder,
    events_of,
    make_repo,
    run_cli,
    walk_to,
)


def test_declining_machine_starts_no_review_and_says_why(
    hosted_env: HostedEnv, tmp_path: Path, spawns: SpawnRecorder
) -> None:
    admin.set_project_config(
        hosted_env.server_root,
        "demo",
        {"auto_plan_review_on_transition": "true", "auto_code_review_on_transition": "true"},
    )
    hosted_env.write_remote(run_auto_reviews=False)
    repo = make_repo(tmp_path / "repo")
    assert run_cli(repo, "remote", "attach", "team", "demo").exit_code == 0
    assert run_cli(repo, "create", "Declined", "--actor", "agent:dev").exit_code == 0
    walk_to(repo, "DEM-1", "in_planning")
    plan = repo / "plan.md"
    plan.write_text("# Plan\n\n- Work.\n")
    assert run_cli(repo, "plan", "write", "DEM-1", "--file", str(plan)).exit_code == 0

    plain = run_cli(repo, "status", "DEM-1", "planned", "--actor", "agent:dev")
    assert plain.exit_code == 0, plain.output
    assert "run_auto_reviews: false" in plain.stdout
    walk_to(repo, "DEM-1", "in_progress")
    as_json = run_cli(repo, "status", "DEM-1", "review", "--actor", "agent:dev", "--json")
    assert as_json.exit_code == 0, as_json.output
    auto = json.loads(as_json.stdout)["data"]["auto_review"]
    assert auto == {"fired": False, "reason": "run_auto_reviews_false"}

    assert spawns.calls == []
    assert not [e for e in events_of(hosted_env, "DEM-1") if e["type"] == "auto_review_spawned"]


def test_default_machine_runs_the_boards_reviews(
    hosted_env: HostedEnv, tmp_path: Path, spawns: SpawnRecorder
) -> None:
    admin.set_project_config(
        hosted_env.server_root, "demo", {"auto_plan_review_on_transition": "true"}
    )
    repo = make_repo(tmp_path / "repo")
    assert run_cli(repo, "remote", "attach", "team", "demo").exit_code == 0
    assert run_cli(repo, "create", "Accepted", "--actor", "agent:dev").exit_code == 0
    walk_to(repo, "DEM-1", "in_planning", "planned")
    assert spawns.review_types == ["plan-review"]
