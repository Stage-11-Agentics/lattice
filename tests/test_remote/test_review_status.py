"""Review status across machines (AC-5, H-12; SPEC §3.4), with a fake agent.

A review auto-fired from checkout A (reported host ``a``) shows on checkout B
(reported host ``b``) as ``running on a since <t>``, and as failed once
``review_timeout_seconds`` has passed with no artifact. ``code-review`` on B
refuses with ``REVIEW_IN_FLIGHT`` while the spawn is younger than the timeout,
and runs with ``--force``. The auto-fired child is never refused by its own
spawn, in either order of the child starting and the parent recording it.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from lattice.server import admin
from tests.test_remote.hosted import (
    HostedEnv,
    SpawnRecorder,
    events_of,
    fake_agent_on_path,
    git,
    make_repo,
    run_cli,
    walk_to,
)


@contextmanager
def on_host(monkeypatch: pytest.MonkeyPatch, host: str) -> Iterator[None]:
    """Run the block as a client on machine *host* (``origin.reported.host``)."""
    import lattice.boards

    fields = {"host": host, "os_user": "tester", "client_version": "2.0.0"}
    with monkeypatch.context() as m:
        m.setattr(lattice.boards, "_process_origin", lambda: dict(fields))
        yield


def two_checkouts(env: HostedEnv, tmp_path: Path) -> tuple[Path, Path]:
    a = make_repo(tmp_path / "machine-a" / "repo")
    b = make_repo(tmp_path / "machine-b" / "repo")
    for repo in (a, b):
        result = run_cli(repo, "remote", "attach", "team", "demo")
        assert result.exit_code == 0, result.output
    # B has code to review on a branch of its own.
    git(b, "checkout", "-q", "-b", "feat")
    (b / "feature.txt").write_text("feature\n")
    git(b, "add", "feature.txt")
    git(b, "commit", "-q", "-m", "feature")
    return a, b


def fire_code_review_on_a(
    env: HostedEnv, a: Path, monkeypatch: pytest.MonkeyPatch, spawns: SpawnRecorder
) -> dict:
    """On A, move DEM-1 to ``review`` with auto code reviews on; returns the spawn event."""
    admin.set_project_config(
        env.server_root,
        "demo",
        {"auto_code_review_on_transition": "true", "auto_plan_review_on_transition": "false"},
    )
    with on_host(monkeypatch, "a"):
        assert run_cli(a, "create", "Reviewed", "--actor", "agent:dev").exit_code == 0
        walk_to(a, "DEM-1", "in_planning", "planned", "in_progress", "review")
    assert spawns.review_types == ["code-review"]
    spawned = [e for e in events_of(env, "DEM-1") if e["type"] == "auto_review_spawned"]
    assert len(spawned) == 1
    assert spawned[0]["origin"]["reported"]["host"] == "a"
    return spawned[0]


def test_a_review_fired_on_a_shows_running_on_b_and_refuses_until_forced(
    hosted_env: HostedEnv,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    spawns: SpawnRecorder,
) -> None:
    a, b = two_checkouts(hosted_env, tmp_path)
    spawn = fire_code_review_on_a(hosted_env, a, monkeypatch, spawns)
    spawned_at = spawn["data"]["spawned_at"]

    with on_host(monkeypatch, "b"):
        plain = run_cli(b, "review-status", "DEM-1")
        assert plain.exit_code == 0, plain.output
        assert f"code-review: running on a since {spawned_at}" in plain.stdout
        as_json = json.loads(run_cli(b, "review-status", "DEM-1", "--json").stdout)["data"]
        assert as_json["status"] == "running"
        assert as_json["gates"] == [
            {
                "review_type": "code-review",
                "status": "running",
                "host": "a",
                "spawned_at": spawned_at,
                "timeout_seconds": 600,
                "message": f"running on a since {spawned_at}",
            }
        ]

        review = ("code-review", "DEM-1", "--base", "main", "--head", "feat")
        refused = run_cli(b, *review, "--actor", "agent:dev", "--json")
        assert refused.exit_code == 1
        error = json.loads(refused.stdout)["error"]
        assert error["code"] == "REVIEW_IN_FLIGHT"
        assert f"running on a since {spawned_at}" in error["message"]
        assert "--force" in error["message"]

        env_dump = fake_agent_on_path(tmp_path, monkeypatch)
        forced = run_cli(b, *review, "--force", "--actor", "agent:dev", "--json")
        assert forced.exit_code == 0, forced.output
        # The review ran end to end on B: the artifact reached the server.
        attached = [e for e in events_of(hosted_env, "DEM-1") if e["type"] == "artifact_attached"]
        assert [e["data"]["role"] for e in attached] == ["review"]
        assert attached[0]["actor"] == "agent:dev"
        # The review agent never sees the remote's token (SPEC §3.4).
        agent_env = json.loads(env_dump.read_text())
        assert "LATTICE_TOKEN_TEAM" not in agent_env
        assert not [k for k in agent_env if k.startswith("LATTICE_REMOTE_")]

        # With the artifact attached after the spawn, the review has finished.
        done = run_cli(b, "review-status", "DEM-1")
        assert "running on" not in done.stdout
        assert "Review artifacts exist" in done.stdout


def test_a_spawn_older_than_the_timeout_reads_as_failed(
    hosted_env: HostedEnv,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    spawns: SpawnRecorder,
) -> None:
    from lattice.cli import auto_review

    a, b = two_checkouts(hosted_env, tmp_path)
    old = (datetime.now(timezone.utc) - timedelta(seconds=601)).strftime("%Y-%m-%dT%H:%M:%SZ")
    monkeypatch.setattr(auto_review, "_now_iso", lambda: old)
    fire_code_review_on_a(hosted_env, a, monkeypatch, spawns)

    with on_host(monkeypatch, "b"):
        plain = run_cli(b, "review-status", "DEM-1")
        assert (
            "code-review: spawned on a, no artifact after 600 s; treat as failed" in plain.stdout
        )
        assert "Re-run with:  lattice code-review" in plain.stdout
        data = json.loads(run_cli(b, "review-status", "DEM-1", "--json").stdout)["data"]
        assert data["status"] == "failed"
        assert data["gates"][0]["status"] == "failed"
        # A timed-out spawn refuses nothing.
        inline = run_cli(b, "code-review", "DEM-1", "--mode", "inline", "--actor", "agent:dev")
        assert inline.exit_code == 0, inline.output


def test_the_auto_fired_child_is_never_refused_by_its_own_spawn(
    hosted_env: HostedEnv,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    spawns: SpawnRecorder,
) -> None:
    """Both orders: the parent recorded the spawn before the child starts (the
    spawn names the child's trigger), and the child starts first (its trigger is
    a status change newer than the latest spawn, which is an older one)."""
    a, b = two_checkouts(hosted_env, tmp_path)
    spawn = fire_code_review_on_a(hosted_env, a, monkeypatch, spawns)
    trigger = spawn["data"]["trigger_status_event_id"]
    inline = ("code-review", "DEM-1", "--mode", "inline", "--actor", "agent:dev", "--json")

    with on_host(monkeypatch, "b"):
        # Parent first: the recorded spawn names this child's trigger.
        assert run_cli(b, *inline, "--triggered-by", trigger).exit_code == 0

        # Child first: a new transition to review whose spawn is not recorded yet.
        assert run_cli(b, "status", "DEM-1", "in_progress", "--actor", "agent:dev").exit_code == 0
        moved = run_cli(
            b, "status", "DEM-1", "review", "--no-auto-review", "--actor", "agent:dev", "--json"
        )
        assert moved.exit_code == 0, moved.output
        new_trigger = json.loads(moved.stdout)["data"]["last_event_id"]
        assert run_cli(b, *inline, "--triggered-by", new_trigger).exit_code == 0

        # Anything else is refused while the older spawn is in flight: no trigger,
        # a trigger that is not a status change, and a status change before the spawn.
        comment = run_cli(b, "comment", "DEM-1", "hi", "--actor", "agent:dev", "--json")
        comment_id = json.loads(comment.stdout)["data"]["last_event_id"]
        created = events_of(hosted_env, "DEM-1")[0]["id"]
        for extra in ((), ("--triggered-by", comment_id), ("--triggered-by", created)):
            refused = run_cli(b, *inline, *extra)
            assert refused.exit_code == 1, extra
            assert json.loads(refused.stdout)["error"]["code"] == "REVIEW_IN_FLIGHT"


def test_plan_review_refuses_the_same_way(
    hosted_env: HostedEnv,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    spawns: SpawnRecorder,
) -> None:
    admin.set_project_config(
        hosted_env.server_root, "demo", {"auto_plan_review_on_transition": "true"}
    )
    a, b = two_checkouts(hosted_env, tmp_path)
    with on_host(monkeypatch, "a"):
        assert run_cli(a, "create", "Planned", "--actor", "agent:dev").exit_code == 0
        walk_to(a, "DEM-1", "in_planning", "planned")
    assert spawns.review_types == ["plan-review"]
    with on_host(monkeypatch, "b"):
        plain = run_cli(b, "review-status", "DEM-1")
        assert "plan-review: running on a since" in plain.stdout
        refused = run_cli(b, "plan-review", "DEM-1", "--mode", "inline", "--actor", "agent:dev")
        assert refused.exit_code == 1
        assert "REVIEW_IN_FLIGHT" in refused.output or "already in flight" in refused.output
        forced = run_cli(
            b, "plan-review", "DEM-1", "--mode", "inline", "--force", "--actor", "agent:dev"
        )
        assert forced.exit_code == 0, forced.output


def _hold_review_state(checkout: Path, task_id: str, review_type: str) -> None:
    """A live review_state record on this checkout: pid 1 is always alive."""
    record = {
        "agents": [],
        "auto_fired": True,
        "mode": "single",
        "review_type": review_type,
        "started_at": "2026-09-27T00:00:00Z",
        "started_by_pid": 1,
        "task_id": task_id,
    }
    path = checkout / ".lattice" / "review_state" / f"{task_id}.json"
    path.parent.mkdir(exist_ok=True)
    path.write_text(json.dumps(record))


@pytest.mark.parametrize("review_type", ["code-review", "plan-review"])
def test_force_overrides_a_live_local_review_on_the_same_checkout(
    hosted_env: HostedEnv,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    review_type: str,
) -> None:
    """SPEC §3.4: on a hosted checkout ``--force`` overrides the refusal, including
    this machine's own live review_state record, in every mode."""
    _, b = two_checkouts(hosted_env, tmp_path)
    created = run_cli(b, "create", "Held", "--actor", "agent:dev", "--json")
    task_id = json.loads(created.stdout)["data"]["id"]
    plan = run_cli(b, "plan", "write", "DEM-1", "--stdin", "--actor", "agent:dev", input="# P\n")
    assert plan.exit_code == 0, plan.output
    _hold_review_state(b, task_id, review_type)
    extra = ("--base", "main", "--head", "feat") if review_type == "code-review" else ()
    review = (review_type, "DEM-1", *extra, "--actor", "agent:dev")

    for mode in ("inline", "single"):
        refused = run_cli(b, *review, "--mode", mode, "--json")
        assert refused.exit_code == 1, (mode, refused.output)
        assert json.loads(refused.stdout)["error"]["code"] == "REVIEW_IN_FLIGHT"

    inline = run_cli(b, *review, "--mode", "inline", "--force")
    assert inline.exit_code == 0, inline.output
    fake_agent_on_path(tmp_path, monkeypatch)
    single = run_cli(b, *review, "--mode", "single", "--force")
    assert single.exit_code == 0, single.output
    role = "review" if review_type == "code-review" else "plan-review"
    attached = [e for e in events_of(hosted_env, "DEM-1") if e["type"] == "artifact_attached"]
    assert [e["data"]["role"] for e in attached] == [role]


def test_force_does_not_override_a_live_review_on_a_local_board(tmp_path: Path) -> None:
    """Local boards keep today's behaviour: ``--force`` is a hosted override only."""
    repo = make_repo(tmp_path / "local")
    assert (
        run_cli(
            repo,
            "init",
            "--project-code",
            "LOC",
            "--actor",
            "human:a",
            "--no-setup-claude",
            "--no-setup-agents",
            "--no-seed",
        ).exit_code
        == 0
    )
    created = run_cli(repo, "create", "Held", "--actor", "agent:dev", "--json")
    task_id = json.loads(created.stdout)["data"]["id"]
    _hold_review_state(repo, task_id, "code-review")
    refused = run_cli(
        repo,
        "code-review",
        "LOC-1",
        "--mode",
        "inline",
        "--force",
        "--actor",
        "agent:dev",
        "--json",
    )
    assert refused.exit_code == 1
    assert json.loads(refused.stdout)["error"]["code"] == "REVIEW_IN_FLIGHT"
