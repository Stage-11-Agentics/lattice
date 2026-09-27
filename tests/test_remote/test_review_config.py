"""AC-49: each hosted project keeps its own review workflow, decided on the
client from the synced ``config.json`` (SPEC §3.4)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from lattice.server import admin
from tests.test_remote.hosted import (
    HostedEnv,
    SpawnRecorder,
    events_of,
    make_repo,
    run_cli,
    walk_to,
)

WORKFLOWS = {
    "plan reviews only": ("true", "false", ["plan-review"]),
    "code reviews only": ("false", "true", ["code-review"]),
    "both": ("true", "true", ["plan-review", "code-review"]),
    "none": ("false", "false", []),
}


def _configure(env: HostedEnv, plan: str, code: str) -> None:
    admin.set_project_config(
        env.server_root,
        "demo",
        {"auto_plan_review_on_transition": plan, "auto_code_review_on_transition": code},
    )


@pytest.mark.parametrize("workflow", sorted(WORKFLOWS))
def test_each_workflow_spawns_exactly_its_reviews(
    hosted_env: HostedEnv, tmp_path: Path, spawns: SpawnRecorder, workflow: str
) -> None:
    plan, code, expected = WORKFLOWS[workflow]
    _configure(hosted_env, plan, code)
    repo = make_repo(tmp_path / "repo")
    assert run_cli(repo, "remote", "attach", "team", "demo").exit_code == 0
    for title in ("Planned task", "Reviewed task"):
        assert run_cli(repo, "create", title, "--actor", "agent:dev").exit_code == 0
    walk_to(repo, "DEM-1", "in_planning", "planned")
    walk_to(repo, "DEM-2", "in_planning")
    # Move DEM-2 to planned without a review so only its `review` transition counts.
    plan_file = repo / "plan.md"
    plan_file.write_text("# Plan\n\n- Work.\n")
    assert run_cli(repo, "plan", "write", "DEM-2", "--file", str(plan_file)).exit_code == 0
    moved = run_cli(repo, "status", "DEM-2", "planned", "--no-auto-review", "--actor", "agent:dev")
    assert moved.exit_code == 0, moved.output
    walk_to(repo, "DEM-2", "in_progress", "review")

    assert spawns.review_types == expected
    recorded = [
        e["data"]["review_type"]
        for short_id in ("DEM-1", "DEM-2")
        for e in events_of(hosted_env, short_id)
        if e["type"] == "auto_review_spawned"
    ]
    assert recorded == expected


def test_a_config_change_takes_effect_at_the_next_command(
    hosted_env: HostedEnv, tmp_path: Path, spawns: SpawnRecorder
) -> None:
    _configure(hosted_env, "false", "false")
    repo = make_repo(tmp_path / "repo")
    assert run_cli(repo, "remote", "attach", "team", "demo").exit_code == 0
    for title in ("One", "Two"):
        assert run_cli(repo, "create", title, "--actor", "agent:dev").exit_code == 0
    walk_to(repo, "DEM-1", "in_planning", "planned")
    assert spawns.calls == []
    _configure(hosted_env, "true", "false")  # while the client stays bound
    walk_to(repo, "DEM-2", "in_planning", "planned")
    assert spawns.review_types == ["plan-review"]
    config = json.loads((repo / ".lattice" / "config.json").read_text())
    assert config["auto_plan_review_on_transition"] is True
