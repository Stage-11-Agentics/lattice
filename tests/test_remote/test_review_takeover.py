"""A forced review takes the slot over safely (SPEC §3.4, H-12 review round 2).

``--force`` on a hosted checkout takes over this machine's live review record.
The review it displaced keeps running; when it finishes it must not write or
clear the record the forced review now owns. Otherwise another unforced review
could start and ``review-status`` would stop showing the running one. Each
claim carries a token; every later write and the final clear require still
holding it.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from lattice.core.review import read_review_state
from lattice.server.testing import wait_for
from tests.test_remote.hosted import (
    HostedEnv,
    events_of,
    fake_agent_on_path,
    git,
    make_repo,
    run_cli,
)

_LATTICE = [sys.executable, "-c", "from lattice.cli.main import cli; cli()"]


def _review(checkout: Path, actor: str, delay: float, *extra: str) -> subprocess.Popen:
    """A real ``lattice code-review`` process whose stub agent takes *delay* seconds."""
    return subprocess.Popen(
        [*_LATTICE, "code-review", "DEM-1", "--base", "main", "--head", "feat"]
        + ["--mode", "single", "--actor", actor, *extra],
        cwd=checkout,
        env={**os.environ, "FAKE_AGENT_DELAY": str(delay)},
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )


def test_the_displaced_review_leaves_the_forced_reviews_record_alone(
    hosted_env: HostedEnv, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = make_repo(tmp_path / "repo")
    assert run_cli(repo, "remote", "attach", "team", "demo").exit_code == 0
    git(repo, "checkout", "-q", "-b", "feat")
    (repo / "feature.txt").write_text("feature\n")
    git(repo, "add", "feature.txt")
    git(repo, "commit", "-q", "-m", "feature")
    created = run_cli(repo, "create", "Contended", "--actor", "agent:dev", "--json")
    task_id = json.loads(created.stdout)["data"]["id"]
    cache = repo / ".lattice"
    fake_agent_on_path(tmp_path, monkeypatch)

    def record() -> dict:
        return read_review_state(cache, task_id) or {}

    older = _review(repo, "agent:older", 1.0)
    forced = None
    try:
        assert wait_for(lambda: record().get("started_by_pid") == older.pid, 20), record()
        assert wait_for(lambda: record().get("agents"), 20)
        older_claim = record()["claim"]

        forced = _review(repo, "agent:forced", 2.5, "--force")
        assert wait_for(lambda: record().get("started_by_pid") == forced.pid, 20), record()
        forced_claim = record()["claim"]
        assert forced_claim != older_claim

        # The displaced review finishes while the forced one is still running.
        out, err = older.communicate(timeout=30)
        assert older.returncode == 0, err
        assert forced.poll() is None, "the forced review must still be running"
        held = record()
        assert held["started_by_pid"] == forced.pid
        assert held["claim"] == forced_claim
        assert held.get("status") != "failed"

        # review-status shows the forced review; an unforced review is refused.
        status = run_cli(repo, "review-status", "DEM-1", "--json")
        data = json.loads(status.stdout)["data"]
        assert data["started_by_pid"] == forced.pid
        assert "claim" not in data
        refused = run_cli(
            repo, "code-review", "DEM-1", "--mode", "inline", "--actor", "agent:third", "--json"
        )
        assert refused.exit_code == 1
        assert json.loads(refused.stdout)["error"]["code"] == "REVIEW_IN_FLIGHT"

        out, err = forced.communicate(timeout=30)
        assert forced.returncode == 0, err
    finally:
        for proc in (older, forced):
            if proc is not None and proc.poll() is None:
                proc.kill()
                proc.communicate()
    # The forced review owned the slot to the end, so its clear removed the record.
    assert record() == {}
    attached = [e for e in events_of(hosted_env, "DEM-1") if e["type"] == "artifact_attached"]
    assert sorted(e["actor"] for e in attached) == ["agent:forced", "agent:older"]


def _parent_claim(lattice: Path, task: str) -> dict:
    """The record auto-fire's parent writes, naming this process's parent."""
    from lattice.core.review import claim_review_state

    claimed, record = claim_review_state(
        lattice,
        task,
        mode="single",
        review_type="code-review",
        started_by_pid=os.getppid(),
        auto_fired=True,
    )
    assert claimed and record is not None
    return record


def test_the_auto_fired_child_adopts_its_parents_claim(tmp_path: Path) -> None:
    from lattice.cli.review_cmds import _claim_or_refuse

    lattice = tmp_path / ".lattice"
    lattice.mkdir()
    parent = _parent_claim(lattice, "task_1")
    claim = _claim_or_refuse(
        lattice,
        "task_1",
        mode="single",
        review_type="code-review",
        triggered_by="ev_trigger",
        is_json=True,
    )
    held = read_review_state(lattice, "task_1")
    assert held is not None
    assert (held["started_by_pid"], held["claim"]) == (os.getpid(), claim)
    assert claim != parent["claim"]


def test_adoption_never_displaces_a_force_that_landed_after_the_childs_read(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Round-3 interleaving, made deterministic: the child reads its parent's
    claim; a hosted --force takes the slot over; the child resumes. Its adoption is
    a compare-and-swap on what it read, so it fails and the child takes the normal
    claim path, which refuses while the forced holder is alive."""
    from lattice.cli import review_cmds
    from lattice.core.review import take_over_review_state

    lattice = tmp_path / ".lattice"
    lattice.mkdir()
    _parent_claim(lattice, "task_1")
    forced_record = {
        "task_id": "task_1",
        "mode": "single",
        "review_type": "code-review",
        "started_at": "2026-09-27T00:00:00Z",
        "started_by_pid": 1,  # a live process: the forced reviewer
        "auto_fired": False,
        "agents": [],
    }
    forced: dict[str, str] = {}
    real_read = review_cmds.read_review_state

    def read_then_force(lattice_dir: Path, task_id: str) -> dict | None:
        observed = real_read(lattice_dir, task_id)  # the child's read
        forced["claim"] = take_over_review_state(lattice_dir, forced_record)  # the force lands
        return observed

    monkeypatch.setattr(review_cmds, "read_review_state", read_then_force)
    with pytest.raises(SystemExit):
        review_cmds._claim_or_refuse(
            lattice,
            "task_1",
            mode="single",
            review_type="code-review",
            triggered_by="ev_trigger",
            is_json=True,
        )
    error = json.loads(capsys.readouterr().out)["error"]
    assert error["code"] == "REVIEW_IN_FLIGHT"
    held = read_review_state(lattice, "task_1")
    assert held is not None
    assert (held["started_by_pid"], held["claim"]) == (1, forced["claim"])
