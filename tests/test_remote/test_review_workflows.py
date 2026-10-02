"""AC-49: a machine whose remote sets ``run_auto_reviews: false`` starts no
review for a transition the board config would review, and says why (SPEC
§3.4, H-11); ``lattice code-review`` still runs one by hand, end to end (H-12)."""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from lattice.server import admin
from tests.test_remote.hosted import (
    HostedEnv,
    SpawnRecorder,
    add_worktree,
    events_of,
    fake_agent_on_path,
    git,
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


def test_declining_machine_still_runs_a_review_by_hand(
    hosted_env: HostedEnv,
    tmp_path: Path,
    spawns: SpawnRecorder,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """AC-49 (H-12 part): ``run_auto_reviews: false`` declines the board's
    auto-review, and ``lattice code-review <task>`` still runs one by hand, end
    to end: a stub agent reviews the diff and the artifact reaches the server."""
    admin.set_project_config(
        hosted_env.server_root,
        "demo",
        {"auto_code_review_on_transition": "true", "auto_plan_review_on_transition": "false"},
    )
    hosted_env.write_remote(run_auto_reviews=False)
    repo = make_repo(tmp_path / "repo")
    assert run_cli(repo, "remote", "attach", "team", "demo").exit_code == 0
    git(repo, "checkout", "-q", "-b", "feat")
    (repo / "feature.txt").write_text("feature\n")
    git(repo, "add", "feature.txt")
    git(repo, "commit", "-q", "-m", "feature")
    assert run_cli(repo, "create", "By hand", "--actor", "agent:dev").exit_code == 0
    walk_to(repo, "DEM-1", "in_planning", "planned", "in_progress")
    moved = run_cli(repo, "status", "DEM-1", "review", "--actor", "agent:dev", "--json")
    assert json.loads(moved.stdout)["data"]["auto_review"]["reason"] == "run_auto_reviews_false"
    assert spawns.calls == []

    fake_agent_on_path(tmp_path, monkeypatch)
    review = run_cli(
        repo, "code-review", "DEM-1", "--base", "main", "--head", "feat", "--actor", "agent:dev"
    )
    assert review.exit_code == 0, review.output
    assert "Review stored as artifact" in review.stdout
    match = re.search(r"Review stored as artifact (art_[0-9A-HJKMNP-TV-Z]{26})", review.stdout)
    assert match is not None, review.stdout
    art_id = match.group(1)
    events = events_of(hosted_env, "DEM-1")
    attached = [e for e in events if e["type"] == "artifact_attached"]
    assert [e["data"]["role"] for e in attached] == ["review"]
    assert not [e for e in events if e["type"] == "auto_review_spawned"]
    status = run_cli(repo, "review-status", "DEM-1")
    assert "Review artifacts exist" in status.stdout

    # A sibling linked worktree has no local .lattice tree. The binding file
    # alone routes artifact reads to the hosted board and lets it catch up.
    linked = add_worktree(repo, tmp_path / "review-reader", "review-reader")
    binding = repo / ".lattice-remote.json"
    assert binding.is_file()
    (linked / ".lattice-remote.json").write_bytes(binding.read_bytes())
    assert not (linked / ".lattice").exists()

    from lattice.remote import http

    real_request = http.request
    requests: list[tuple[str, str]] = []

    def recording_request(remote, method, path, **kwargs):
        requests.append((method, path))
        return real_request(remote, method, path, **kwargs)

    monkeypatch.setattr(http, "request", recording_request)
    shown = run_cli(linked, "artifact", "show", art_id, "--json")
    assert shown.exit_code == 0, shown.output
    payload = json.loads(shown.stdout)["data"]
    assert payload["artifact"]["id"] == art_id
    assert payload["content"].startswith("Lattice-Reviewed-Commit: ")
    assert "Lattice-Reviewed-Commit:" in payload["content"]
    assert payload["payload_path"] == f"artifacts/payload/{art_id}.md"
    assert not [
        (method, path) for method, path in requests if method == "POST" and "/ops/" in path
    ]
