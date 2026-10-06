"""Tests for `lattice complete` compound operation."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from unittest.mock import patch

import pytest

from lattice.cli import task_cmds
from lattice.storage.fs import LATTICE_DIR

from tests.conftest import _add_policies_to_config


_ACTOR = "human:test"


@pytest.fixture()
def caller_repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A throwaway git checkout that the CLI is invoked from.

    The reachable-review-commit gate reads the *invoking* checkout, so these
    tests need one they own. Borrowing the repository the suite happens to run
    in makes them depend on its branch and HEAD — which is empty under CI's
    detached checkout.
    """
    repo = tmp_path / "caller-repo"
    repo.mkdir()
    git = ["git", "-C", str(repo), "-c", "user.email=t@t", "-c", "user.name=t"]
    subprocess.run(["git", "init", "-q", "-b", "work", str(repo)], check=True)
    (repo / "file.txt").write_text("work\n", encoding="utf-8")
    subprocess.run([*git, "add", "-A"], check=True)
    subprocess.run([*git, "commit", "-qm", "work"], check=True)
    monkeypatch.chdir(repo)
    return repo


def _create_and_advance_to(invoke, fill_plan, target_status: str) -> str:
    """Create a task and advance it to the given status. Returns task_id."""
    r = invoke("create", "Test task", "--actor", _ACTOR, "--json")
    task_id = json.loads(r.output)["data"]["id"]

    if target_status == "backlog":
        return task_id

    invoke("status", task_id, "in_planning", "--actor", _ACTOR)
    if target_status == "in_planning":
        return task_id

    fill_plan(task_id, "Test task")
    invoke("status", task_id, "planned", "--actor", _ACTOR)
    if target_status == "planned":
        return task_id

    invoke("status", task_id, "in_progress", "--actor", _ACTOR)
    if target_status == "in_progress":
        return task_id

    invoke("status", task_id, "review", "--actor", _ACTOR)
    if target_status == "review":
        return task_id

    return task_id


class TestCompleteBasic:
    """Basic happy-path tests for lattice complete."""

    def test_complete_from_pr_open_does_not_warn_about_reset(
        self, invoke, initialized_root, fill_plan
    ) -> None:
        task_id = _create_and_advance_to(invoke, fill_plan, "review")
        evidence = invoke(
            "comment",
            task_id,
            "Validation passed for the pull request.",
            "--role",
            "validation",
            "--actor",
            _ACTOR,
        )
        assert evidence.exit_code == 0, evidence.output

        pr_open = invoke("status", task_id, "pr_open", "--actor", _ACTOR)
        assert pr_open.exit_code == 0, pr_open.output

        completed = invoke("complete", task_id, "--review", "Reviewed.", "--actor", _ACTOR)
        assert completed.exit_code == 0, completed.output
        assert "pr_open -> review -> done" in completed.output
        assert "Previously completed, reset on " not in completed.output
        shown = invoke("show", task_id)
        assert shown.exit_code == 0, shown.output
        assert "Previously completed, reset on " not in shown.output

    def test_complete_from_in_progress(self, invoke, initialized_root, fill_plan) -> None:
        task_id = _create_and_advance_to(invoke, fill_plan, "in_progress")

        r = invoke("complete", task_id, "--review", "LGTM. All tests pass.", "--actor", _ACTOR)
        assert r.exit_code == 0
        assert "Completed" in r.output
        assert "4 events" in r.output

        r = invoke("show", task_id, "--json")
        snapshot = json.loads(r.output)["data"]
        assert snapshot["status"] == "done"

    def test_complete_from_review(self, invoke, initialized_root, fill_plan) -> None:
        task_id = _create_and_advance_to(invoke, fill_plan, "review")

        r = invoke(
            "complete", task_id, "--review", "Reviewed cold, looks good.", "--actor", _ACTOR
        )
        assert r.exit_code == 0
        assert "3 events" in r.output

        r = invoke("show", task_id, "--json")
        snapshot = json.loads(r.output)["data"]
        assert snapshot["status"] == "done"

    def test_complete_json_output(self, invoke, initialized_root, fill_plan) -> None:
        task_id = _create_and_advance_to(invoke, fill_plan, "in_progress")

        r = invoke("complete", task_id, "--review", "LGTM", "--actor", _ACTOR, "--json")
        assert r.exit_code == 0
        parsed = json.loads(r.output)
        assert parsed["ok"] is True
        assert parsed["data"]["status"] == "done"

    def test_complete_quiet_output(self, invoke, initialized_root, fill_plan) -> None:
        task_id = _create_and_advance_to(invoke, fill_plan, "in_progress")

        r = invoke("complete", task_id, "--review", "LGTM", "--actor", _ACTOR, "--quiet")
        assert r.exit_code == 0
        assert r.output.strip() == "ok"

    def test_via_complete_names_bundle_target_and_actual_path(
        self, invoke, initialized_root
    ) -> None:
        primary = json.loads(invoke("create", "Primary", "--actor", _ACTOR, "--json").output)[
            "data"
        ]
        bundled = json.loads(invoke("create", "Bundled", "--actor", _ACTOR, "--json").output)[
            "data"
        ]
        target_label = primary.get("short_id") or primary["id"]

        result = invoke(
            "complete",
            bundled["id"],
            "--review",
            "Reviewed in the primary PR.",
            "--via",
            primary["id"],
            "--reason",
            "bundle provenance",
            "--actor",
            _ACTOR,
        )

        assert result.exit_code == 0, result.output
        assert f"via {target_label}" in result.output
        assert "backlog -> review -> done" in result.output
        events = [
            json.loads(line)
            for line in (initialized_root / LATTICE_DIR / "events" / f"{bundled['id']}.jsonl")
            .read_text()
            .splitlines()
        ]
        emitted = events[-4:]
        assert emitted[1]["data"]["force"] is True
        assert target_label in emitted[1]["data"]["reason"]
        assert emitted[1]["provenance"]["reason"] == "bundle provenance"
        assert emitted[-1]["data"]["via"] == {
            "kind": "task",
            "id": primary["id"],
            "short_id": primary.get("short_id"),
        }

    def test_via_syntax_is_checked_before_board_access(self, tmp_path: Path) -> None:
        from click.testing import CliRunner

        from lattice.cli.main import cli

        result = CliRunner().invoke(
            cli,
            [
                "complete",
                "task_01JBBBBBBBBBBBBBBBBBBBBBBB",
                "--review",
                "Reviewed.",
                "--via",
                "https://user@example.com/PR/1",
            ],
            env={"LATTICE_ROOT": str(tmp_path / "missing-board")},
            catch_exceptions=False,
        )

        assert result.exit_code != 0
        assert "--via" in result.output
        assert "<task ID>" in result.output
        assert "#<N>" in result.output
        assert "http(s)://<host>/" in result.output


class TestCompleteEvents:
    """Verify the event stream produced by lattice complete."""

    def test_produces_four_events_from_in_progress(
        self,
        invoke,
        initialized_root,
        fill_plan,
    ) -> None:
        task_id = _create_and_advance_to(invoke, fill_plan, "in_progress")

        lattice_dir = initialized_root / LATTICE_DIR
        event_path = lattice_dir / "events" / f"{task_id}.jsonl"
        events_before = len(event_path.read_text().strip().splitlines())

        invoke("complete", task_id, "--review", "Review text here", "--actor", _ACTOR)

        all_lines = event_path.read_text().strip().splitlines()
        new_events = [json.loads(line) for line in all_lines[events_before:]]
        assert len(new_events) == 4

        assert new_events[0]["type"] == "comment_added"
        assert new_events[0]["data"]["role"] == "review"
        assert new_events[0]["data"]["body"] == "Review text here"

        assert new_events[1]["type"] == "status_changed"
        assert new_events[1]["data"]["to"] == "review"

        assert new_events[2]["type"] == "artifact_attached"
        assert new_events[2]["data"]["role"] == "review"

        assert new_events[3]["type"] == "status_changed"
        assert new_events[3]["data"]["to"] == "done"

    def test_produces_three_events_from_review(
        self,
        invoke,
        initialized_root,
        fill_plan,
    ) -> None:
        task_id = _create_and_advance_to(invoke, fill_plan, "review")

        lattice_dir = initialized_root / LATTICE_DIR
        event_path = lattice_dir / "events" / f"{task_id}.jsonl"
        events_before = len(event_path.read_text().strip().splitlines())

        invoke(
            "complete",
            task_id,
            "--review",
            "Already in review, finishing.",
            "--actor",
            _ACTOR,
        )

        all_lines = event_path.read_text().strip().splitlines()
        new_events = [json.loads(line) for line in all_lines[events_before:]]
        assert len(new_events) == 3

        assert new_events[0]["type"] == "comment_added"
        assert new_events[1]["type"] == "artifact_attached"
        assert new_events[2]["type"] == "status_changed"
        assert new_events[2]["data"]["to"] == "done"

    def test_artifact_payload_exists(self, invoke, initialized_root, fill_plan) -> None:
        task_id = _create_and_advance_to(invoke, fill_plan, "in_progress")

        r = invoke(
            "complete",
            task_id,
            "--review",
            "Detailed review findings",
            "--actor",
            _ACTOR,
            "--json",
        )
        snapshot = json.loads(r.output)["data"]

        evidence_refs = snapshot.get("evidence_refs", [])
        art_refs = [ref for ref in evidence_refs if ref.get("source_type") == "artifact"]
        assert len(art_refs) >= 1

        art_id = art_refs[0]["id"]
        lattice_dir = initialized_root / LATTICE_DIR

        meta_path = lattice_dir / "artifacts" / "meta" / f"{art_id}.json"
        assert meta_path.exists()
        meta = json.loads(meta_path.read_text())
        assert meta["type"] == "note"
        assert meta["title"] == "Review findings"

        payload_path = lattice_dir / "artifacts" / "payload" / f"{art_id}.md"
        assert payload_path.exists()
        assert payload_path.read_text() == "Detailed review findings"


class TestCompleteValidation:
    """Validation and error cases."""

    def test_fails_from_backlog(self, invoke, initialized_root, fill_plan) -> None:
        task_id = _create_and_advance_to(invoke, fill_plan, "backlog")

        r = invoke(
            "complete",
            task_id,
            "--review",
            "Review",
            "--actor",
            _ACTOR,
            "--json",
        )
        assert r.exit_code != 0
        parsed = json.loads(r.output)
        assert parsed["ok"] is False
        assert parsed["error"]["code"] == "INVALID_TRANSITION"

    def test_fails_from_in_planning(self, invoke, initialized_root, fill_plan) -> None:
        task_id = _create_and_advance_to(invoke, fill_plan, "in_planning")

        r = invoke(
            "complete",
            task_id,
            "--review",
            "Review",
            "--actor",
            _ACTOR,
            "--json",
        )
        assert r.exit_code != 0
        parsed = json.loads(r.output)
        assert parsed["ok"] is False
        assert parsed["error"]["code"] == "INVALID_TRANSITION"

    def test_fails_without_review_flag(self, invoke, initialized_root, fill_plan) -> None:
        # --review is no longer required at the Click level (--review-file can
        # satisfy it, LAT-263); the shared helper enforces exactly-one instead.
        task_id = _create_and_advance_to(invoke, fill_plan, "in_progress")

        r = invoke("complete", task_id, "--actor", _ACTOR, "--json")
        assert r.exit_code != 0
        parsed = json.loads(r.output)
        assert parsed["error"]["code"] == "VALIDATION_ERROR"
        assert "--review" in parsed["error"]["message"]
        assert "--review-file" in parsed["error"]["message"]

    def test_empty_review_text_fails(self, invoke, initialized_root, fill_plan) -> None:
        task_id = _create_and_advance_to(invoke, fill_plan, "in_progress")

        r = invoke(
            "complete",
            task_id,
            "--review",
            "",
            "--actor",
            _ACTOR,
            "--json",
        )
        assert r.exit_code != 0

    def test_short_id_accepted(self, invoke, initialized_root, fill_plan) -> None:
        lattice_dir = initialized_root / LATTICE_DIR
        config_path = lattice_dir / "config.json"
        config = json.loads(config_path.read_text())
        config["project_code"] = "TST"
        config_path.write_text(json.dumps(config, sort_keys=True, indent=2) + "\n")

        from lattice.storage.short_ids import _default_index, save_id_index

        save_id_index(lattice_dir, _default_index())

        r = invoke("create", "Short ID test", "--actor", _ACTOR, "--json")
        snapshot = json.loads(r.output)["data"]
        task_id = snapshot["id"]
        short_id = snapshot["short_id"]
        assert short_id is not None

        invoke("status", task_id, "in_planning", "--actor", _ACTOR)
        fill_plan(task_id, "Short ID test")
        invoke("status", task_id, "planned", "--actor", _ACTOR)
        invoke("status", task_id, "in_progress", "--actor", _ACTOR)

        r = invoke(
            "complete",
            short_id,
            "--review",
            "LGTM via short ID",
            "--actor",
            _ACTOR,
        )
        assert r.exit_code == 0
        assert "Completed" in r.output


class TestCompleteCompletionPolicy:
    """Verify completion policies are properly enforced/satisfied."""

    def test_satisfies_review_role_policy(
        self,
        invoke_with_policies,
        initialized_root_with_policies,
        fill_plan_with_policies,
    ) -> None:
        task_id = _create_and_advance_to(
            invoke_with_policies,
            fill_plan_with_policies,
            "in_progress",
        )

        r = invoke_with_policies(
            "complete",
            task_id,
            "--review",
            "Review findings",
            "--actor",
            _ACTOR,
        )
        assert r.exit_code == 0
        assert "Completed" in r.output

    def test_fails_unmet_non_review_policy(
        self,
        invoke,
        initialized_root,
        fill_plan,
    ) -> None:
        _add_policies_to_config(
            initialized_root,
            {"done": {"require_roles": ["review", "security"]}},
        )

        lattice_dir = initialized_root / LATTICE_DIR
        config_path = lattice_dir / "config.json"
        config = json.loads(config_path.read_text())
        config["workflow"]["roles"] = ["review", "security"]
        config_path.write_text(json.dumps(config, sort_keys=True, indent=2) + "\n")

        task_id = _create_and_advance_to(invoke, fill_plan, "in_progress")

        r = invoke(
            "complete",
            task_id,
            "--review",
            "Review findings",
            "--actor",
            _ACTOR,
            "--json",
        )
        assert r.exit_code != 0
        parsed = json.loads(r.output)
        assert parsed["error"]["code"] == "COMPLETION_BLOCKED"
        assert "security" in parsed["error"]["message"]


class TestReachableReviewCommitCommandBoundaries:
    """Exercise the completion door, including its prospective artifact."""

    def _enable_gate_and_link_current_branch(
        self, invoke, root: Path, task_id: str
    ) -> tuple[str, str]:
        config_path = root / LATTICE_DIR / "config.json"
        config = json.loads(config_path.read_text())
        config["workflow"]["completion_policies"]["done"] = {
            "require_reachable_review_commit": True
        }
        config_path.write_text(json.dumps(config, sort_keys=True, indent=2) + "\n")
        repo = task_cmds._caller_git_worktree()
        assert repo is not None
        assert repo.name == "caller-repo", "the gate must read the invoking checkout"
        branch = subprocess.check_output(
            ["git", "-C", str(repo), "branch", "--show-current"], text=True
        ).strip()
        head = subprocess.check_output(
            ["git", "-C", str(repo), "rev-parse", "HEAD"], text=True
        ).strip()
        linked = invoke("branch-link", task_id, branch, "--actor", _ACTOR)
        assert linked.exit_code == 0, linked.output
        return branch, head

    def test_status_done_rejects_unreachable_then_accepts_reachable_artifact(
        self, invoke, initialized_root, fill_plan, caller_repo
    ) -> None:
        task_id = _create_and_advance_to(invoke, fill_plan, "review")
        _, head = self._enable_gate_and_link_current_branch(invoke, initialized_root, task_id)
        bad = initialized_root / "bad-review.md"
        bad.write_text(f"Lattice-Reviewed-Commit: {'0' * 40}\n\nnope", encoding="utf-8")
        assert (
            invoke("attach", task_id, str(bad), "--role", "review", "--actor", _ACTOR).exit_code
            == 0
        )

        rejected = invoke("status", task_id, "done", "--actor", _ACTOR, "--json")
        assert rejected.exit_code != 0
        assert json.loads(rejected.output)["error"]["code"] == "COMPLETION_BLOCKED"

        good = initialized_root / "good-review.md"
        good.write_text(f"Lattice-Reviewed-Commit: {head}\n\npass", encoding="utf-8")
        assert (
            invoke("attach", task_id, str(good), "--role", "review", "--actor", _ACTOR).exit_code
            == 0
        )
        accepted = invoke("status", task_id, "done", "--actor", _ACTOR, "--json")
        assert accepted.exit_code == 0, accepted.output
        assert json.loads(accepted.output)["data"]["status"] == "done"

    def test_complete_rejects_candidate_without_persisting_then_writes_strict_marker(
        self, invoke, initialized_root, fill_plan, caller_repo
    ) -> None:
        task_id = _create_and_advance_to(invoke, fill_plan, "review")
        _, head = self._enable_gate_and_link_current_branch(invoke, initialized_root, task_id)
        lattice_dir = initialized_root / LATTICE_DIR
        event_path = lattice_dir / "events" / f"{task_id}.jsonl"
        before_events = event_path.read_text(encoding="utf-8")
        before_payloads = set((lattice_dir / "artifacts" / "payload").glob("*"))
        before_meta = set((lattice_dir / "artifacts" / "meta").glob("*"))

        with patch.object(task_cmds.subprocess, "check_output", return_value="0" * 40 + "\n"):
            rejected = invoke("complete", task_id, "--review", "bad", "--actor", _ACTOR, "--json")
        assert rejected.exit_code != 0
        assert json.loads(rejected.output)["error"]["code"] == "COMPLETION_BLOCKED"
        assert event_path.read_text(encoding="utf-8") == before_events
        assert set((lattice_dir / "artifacts" / "payload").glob("*")) == before_payloads
        assert set((lattice_dir / "artifacts" / "meta").glob("*")) == before_meta

        accepted = invoke("complete", task_id, "--review", "good", "--actor", _ACTOR, "--json")
        assert accepted.exit_code == 0, accepted.output
        snapshot = json.loads(accepted.output)["data"]
        art_id = next(
            ref["id"]
            for ref in snapshot["evidence_refs"]
            if ref["role"] == "review" and ref["source_type"] == "artifact"
        )
        payload = (lattice_dir / "artifacts" / "payload" / f"{art_id}.md").read_text(
            encoding="utf-8"
        )
        assert payload == f"Lattice-Reviewed-Commit: {head}\n\ngood"

    def test_via_does_not_borrow_primary_branch_for_reachability(
        self, invoke, initialized_root, caller_repo
    ) -> None:
        task_id = json.loads(invoke("create", "Bundled", "--actor", _ACTOR, "--json").output)[
            "data"
        ]["id"]
        primary_id = json.loads(invoke("create", "Primary", "--actor", _ACTOR, "--json").output)[
            "data"
        ]["id"]
        config_path = initialized_root / LATTICE_DIR / "config.json"
        config = json.loads(config_path.read_text())
        config["workflow"]["completion_policies"]["done"] = {
            "require_reachable_review_commit": True
        }
        config_path.write_text(json.dumps(config, sort_keys=True, indent=2) + "\n")

        subprocess.run(["git", "branch", "primary"], cwd=caller_repo, check=True)
        (caller_repo / "later.txt").write_text("not on primary\n", encoding="utf-8")
        subprocess.run(["git", "add", "later.txt"], cwd=caller_repo, check=True)
        subprocess.run(
            ["git", "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-m", "later"],
            cwd=caller_repo,
            check=True,
        )
        assert invoke("branch-link", task_id, "primary", "--actor", _ACTOR).exit_code == 0

        event_path = initialized_root / LATTICE_DIR / "events" / f"{task_id}.jsonl"
        before_events = event_path.read_bytes()
        before_payloads = set((initialized_root / LATTICE_DIR / "artifacts" / "payload").glob("*"))
        before_meta = set((initialized_root / LATTICE_DIR / "artifacts" / "meta").glob("*"))

        result = invoke(
            "complete",
            task_id,
            "--review",
            "Not reachable from the primary branch.",
            "--via",
            primary_id,
            "--actor",
            _ACTOR,
            "--json",
        )

        assert result.exit_code != 0
        assert json.loads(result.output)["error"]["code"] == "COMPLETION_BLOCKED"
        assert event_path.read_bytes() == before_events
        assert (
            set((initialized_root / LATTICE_DIR / "artifacts" / "payload").glob("*"))
            == before_payloads
        )
        assert (
            set((initialized_root / LATTICE_DIR / "artifacts" / "meta").glob("*")) == before_meta
        )


class TestCompleteOnComposedWorkflows:
    """``complete`` picks its route from the current status (LAT-395)."""

    def _compose(self, root: Path, *, include_review: bool) -> None:
        from lattice.core.config import compose_workflow

        config_path = root / LATTICE_DIR / "config.json"
        config = json.loads(config_path.read_text())
        config["workflow"] = compose_workflow(
            include_review=include_review, include_validation=True, include_pr_open=True
        )
        config_path.write_text(json.dumps(config, sort_keys=True, indent=2) + "\n")

    def _walk(self, invoke, fill_plan, steps: tuple[str, ...]) -> str:
        r = invoke("create", "Composed task", "--actor", _ACTOR, "--json")
        task_id = json.loads(r.output)["data"]["id"]
        invoke("status", task_id, "in_planning", "--actor", _ACTOR)
        fill_plan(task_id, "Composed task")
        for step in ("planned", "in_progress", *steps):
            r = invoke("status", task_id, step, "--actor", _ACTOR, "--json")
            assert r.exit_code == 0, r.output
        return task_id

    @pytest.mark.parametrize(
        ("include_review", "steps"),
        [(True, ("review", "in_validation")), (False, ("in_validation",))],
    )
    def test_complete_from_validation_goes_straight_to_done(
        self, invoke, initialized_root, fill_plan, include_review, steps
    ) -> None:
        self._compose(initialized_root, include_review=include_review)
        task_id = self._walk(invoke, fill_plan, steps)

        r = invoke(
            "complete",
            task_id,
            "--review",
            "Review PASS; merged and validated.",
            "--actor",
            _ACTOR,
            "--json",
        )

        assert r.exit_code == 0, r.output
        assert json.loads(r.output)["data"]["status"] == "done"

    def test_complete_from_review_still_needs_review_to_done(
        self, invoke, initialized_root, fill_plan
    ) -> None:
        self._compose(initialized_root, include_review=True)
        task_id = self._walk(invoke, fill_plan, ("review",))

        r = invoke("complete", task_id, "--review", "LGTM", "--actor", _ACTOR, "--json")

        assert r.exit_code != 0
        error = json.loads(r.output)["error"]
        assert error["code"] == "INVALID_TRANSITION"
        assert "review to done" in error["message"]

    def test_human_summary_names_the_direct_route(
        self, invoke, initialized_root, fill_plan
    ) -> None:
        self._compose(initialized_root, include_review=False)
        task_id = self._walk(invoke, fill_plan, ("in_validation",))

        r = invoke("complete", task_id, "--review", "Validated after merge.", "--actor", _ACTOR)

        assert r.exit_code == 0, r.output
        assert "(in_validation -> done)" in r.output
