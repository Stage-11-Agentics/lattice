"""AC-5 attestations (SPEC §3.4): ``reachable_review_commits`` is validated
against the task's current state.

An attestation naming another branch, omitting a marker SHA, or listing a SHA
the board lacks is rejected as stale (``COMPLETION_BLOCKED``, details.reason
``STALE_ATTESTATION``); the CLI recomputes and retries once; a fresh
attestation passes. Operations are called directly with attestations as a
client would send them (no git needed); the CLI retry is driven through a
patched computation.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from lattice.boards import LocalBoard, resolve_board
from lattice.core.attestations import STALE_ATTESTATION
from lattice.ops import Caller, OpError
from lattice.ops.task_attach import encode_payload
from lattice.storage.fs import LATTICE_DIR

SHA_A = "a" * 40
SHA_B = "b" * 40
SHA_HEAD = "c" * 40
BRANCH = "feat/X-1-thing"


@pytest.fixture()
def board(initialized_root: Path, monkeypatch: pytest.MonkeyPatch) -> LocalBoard:
    monkeypatch.delenv("LATTICE_ROOT", raising=False)
    config_path = initialized_root / LATTICE_DIR / "config.json"
    config = json.loads(config_path.read_text())
    config["workflow"]["completion_policies"]["done"] = {"require_reachable_review_commit": True}
    config_path.write_text(json.dumps(config, sort_keys=True, indent=2) + "\n")
    return resolve_board(initialized_root)


def _run(board: LocalBoard, op: str, params: dict, attestations: dict | None = None):  # noqa: ANN202
    return board.execute(op, params, Caller(actor="agent:t", attestations=attestations or {}))


def _entry(sha: str, *, branch: str | None = BRANCH, ok: bool = True) -> dict:
    return {"sha": sha, "branch": branch, "exists": ok, "reachable": ok}


def _reviewed_task(board: LocalBoard) -> str:
    """A task in review, linked to BRANCH, with one review artifact naming SHA_A."""
    task_id = _run(board, "task.create", {"title": "Gated"}).value["id"]
    _run(board, "task.branch_link", {"task": task_id, "branch": BRANCH})
    payload = encode_payload("review.md", f"Lattice-Reviewed-Commit: {SHA_A}\n\nok".encode())
    _run(board, "task.attach", {"task": task_id, "payload": payload, "role": "review"})
    _run(
        board,
        "task.status",
        {"task": task_id, "new_status": "review", "force": True, "reason": "test setup"},
    )
    return task_id


def _stale(board: LocalBoard, op: str, params: dict, attestations: dict) -> OpError:
    with pytest.raises(OpError) as exc:
        _run(board, op, params, attestations)
    assert exc.value.code == "COMPLETION_BLOCKED"
    assert exc.value.details["reason"] == STALE_ATTESTATION
    assert exc.value.details["snapshot"]["status"] == "review"
    return exc.value


class TestStatusAttestation:
    def test_names_another_branch(self, board: LocalBoard) -> None:
        task_id = _reviewed_task(board)
        err = _stale(
            board,
            "task.status",
            {"task": task_id, "new_status": "done"},
            {"reachable_review_commits": [_entry(SHA_A, branch="main")]},
        )
        assert "'main'" in err.message and BRANCH in err.message

    def test_omits_a_marker_sha(self, board: LocalBoard) -> None:
        task_id = _reviewed_task(board)
        err = _stale(
            board,
            "task.status",
            {"task": task_id, "new_status": "done"},
            {"reachable_review_commits": []},
        )
        assert SHA_A in err.message

    def test_lists_a_sha_the_board_lacks(self, board: LocalBoard) -> None:
        task_id = _reviewed_task(board)
        err = _stale(
            board,
            "task.status",
            {"task": task_id, "new_status": "done"},
            {"reachable_review_commits": [_entry(SHA_A), _entry(SHA_B)]},
        )
        assert SHA_B in err.message

    def test_fresh_attestation_passes_and_is_recorded(self, board: LocalBoard) -> None:
        task_id = _reviewed_task(board)
        entries = [_entry(SHA_A)]
        result = _run(
            board,
            "task.status",
            {"task": task_id, "new_status": "done"},
            {"reachable_review_commits": entries},
        )
        assert result.value["status"] == "done"
        assert result.events[-1]["data"]["attestations"] == {"reachable_review_commits": entries}

    def test_fresh_but_unreachable_is_the_policy_failure(self, board: LocalBoard) -> None:
        task_id = _reviewed_task(board)
        with pytest.raises(OpError) as exc:
            _run(
                board,
                "task.status",
                {"task": task_id, "new_status": "done"},
                {"reachable_review_commits": [_entry(SHA_A, ok=False)]},
            )
        assert exc.value.code == "COMPLETION_BLOCKED"
        assert "reason" not in exc.value.details
        assert "reachable from the linked branch" in exc.value.message

    def test_no_attestation_lacks_repository_context(self, board: LocalBoard) -> None:
        task_id = _reviewed_task(board)
        with pytest.raises(OpError) as exc:
            _run(board, "task.status", {"task": task_id, "new_status": "done"})
        assert exc.value.code == "COMPLETION_BLOCKED"
        assert "requires repository context" in exc.value.message

    def test_malformed_attestation(self, board: LocalBoard) -> None:
        task_id = _reviewed_task(board)
        with pytest.raises(OpError) as exc:
            _run(
                board,
                "task.status",
                {"task": task_id, "new_status": "done"},
                {"reachable_review_commits": [{"sha": SHA_A}]},
            )
        assert exc.value.code == "VALIDATION_ERROR"

    def test_force_does_not_skip_the_staleness_check(self, board: LocalBoard) -> None:
        """--force bypasses the policy verdict, never the attestation's freshness."""
        task_id = _reviewed_task(board)
        _stale(
            board,
            "task.status",
            {"task": task_id, "new_status": "done", "force": True, "reason": "override"},
            {"reachable_review_commits": [_entry(SHA_A, branch="main")]},
        )

    def test_force_bypasses_the_verdict_of_a_fresh_attestation(self, board: LocalBoard) -> None:
        task_id = _reviewed_task(board)
        entries = [_entry(SHA_A, ok=False)]
        result = _run(
            board,
            "task.status",
            {"task": task_id, "new_status": "done", "force": True, "reason": "override"},
            {"reachable_review_commits": entries},
        )
        assert result.value["status"] == "done"
        assert result.events[-1]["data"]["attestations"] == {"reachable_review_commits": entries}

    def test_duplicate_entries_are_stale(self, board: LocalBoard) -> None:
        task_id = _reviewed_task(board)
        err = _stale(
            board,
            "task.status",
            {"task": task_id, "new_status": "done"},
            {"reachable_review_commits": [_entry(SHA_A), _entry(SHA_A)]},
        )
        assert "more than once" in err.message


class TestCompleteAttestation:
    """``complete`` attaches a payload marked with the caller's HEAD; the
    attestation must cover that marker as well as the board's."""

    def _complete(self, board: LocalBoard, task_id: str, attestations: dict):  # noqa: ANN202
        return _run(
            board, "task.complete", {"task": task_id, "review": "Looks good."}, attestations
        )

    def test_omitting_the_prospective_marker_is_stale(self, board: LocalBoard) -> None:
        task_id = _reviewed_task(board)
        with pytest.raises(OpError) as exc:
            self._complete(
                board,
                task_id,
                {"review_head": SHA_HEAD, "reachable_review_commits": [_entry(SHA_A)]},
            )
        assert exc.value.details["reason"] == STALE_ATTESTATION
        assert SHA_HEAD in exc.value.message

    def test_fresh_attestation_passes(self, board: LocalBoard) -> None:
        task_id = _reviewed_task(board)
        entries = [_entry(SHA_HEAD, ok=False), _entry(SHA_A)]
        result = self._complete(
            board, task_id, {"review_head": SHA_HEAD, "reachable_review_commits": entries}
        )
        assert result.value["status"] == "done"
        assert result.events[-1]["data"]["attestations"]["reachable_review_commits"] == entries

    def test_no_head_is_not_a_worktree(self, board: LocalBoard) -> None:
        task_id = _reviewed_task(board)
        with pytest.raises(OpError) as exc:
            self._complete(board, task_id, {})
        assert (exc.value.code, exc.value.message) == (
            "COMPLETION_BLOCKED",
            "Not inside a git worktree.",
        )


class TestClientRetry:
    """The CLI recomputes a stale attestation and retries once, as a new call."""

    @pytest.fixture()
    def cli(self, board: LocalBoard, invoke, monkeypatch: pytest.MonkeyPatch):  # noqa: ANN001, ANN201
        """Script the computed attestations and log compute / refresh / execute."""
        import lattice.cli.attestations as client
        import lattice.ops as ops

        monkeypatch.setattr(client, "caller_worktree", lambda: board.root)
        self.calls = 0
        self.log: list[tuple[str, str | None]] = []
        real_execute = ops.execute

        def execute(board_dir, op_name, params, caller, **kwargs):  # noqa: ANN001, ANN003, ANN202
            self.log.append(("execute", caller.origin["op_id"]))
            return real_execute(board_dir, op_name, params, caller, **kwargs)

        monkeypatch.setattr(ops, "execute", execute)
        monkeypatch.setattr(
            LocalBoard, "refresh", lambda _self: self.log.append(("refresh", None))
        )

        def install(*answers: list[dict]) -> None:
            def compute(*_args, **_kwargs):  # noqa: ANN002, ANN003, ANN202
                answer = answers[min(self.calls, len(answers) - 1)]
                self.calls += 1
                self.log.append(("compute", None))
                return answer

            monkeypatch.setattr(client, "compute_reachable_review_commits", compute)

        return install

    def test_stale_then_fresh_succeeds_on_the_retry(self, board: LocalBoard, invoke, cli) -> None:  # noqa: ANN001
        task_id = _reviewed_task(board)
        cli([_entry(SHA_A, branch="main")], [_entry(SHA_A)])
        self.log.clear()
        result = invoke("status", task_id, "done", "--actor", "agent:t", "--json")
        assert result.exit_code == 0, result.output
        assert json.loads(result.output)["data"]["status"] == "done"
        assert self.calls == 2
        # compute, attempt, re-sync, recompute, a new attempt with a new op_id
        assert [kind for kind, _ in self.log] == [
            "compute",
            "execute",
            "refresh",
            "compute",
            "execute",
        ]
        first, second = (op_id for kind, op_id in self.log if kind == "execute")
        assert first != second
        log = (board.lattice_dir / "events" / f"{task_id}.jsonl").read_text().splitlines()
        done = json.loads(log[-1])
        assert done["origin"]["op_id"] == second
        assert done["data"]["attestations"]["reachable_review_commits"] == [_entry(SHA_A)]

    def test_no_refresh_without_a_stale_refusal(self, board: LocalBoard, invoke, cli) -> None:  # noqa: ANN001
        task_id = _reviewed_task(board)
        cli([_entry(SHA_A)])
        assert invoke("status", task_id, "done", "--actor", "agent:t").exit_code == 0
        assert ("refresh", None) not in self.log

    def test_stale_twice_reports_the_second_refusal(self, board: LocalBoard, invoke, cli) -> None:  # noqa: ANN001
        task_id = _reviewed_task(board)
        cli([])
        result = invoke("status", task_id, "done", "--actor", "agent:t", "--json")
        assert result.exit_code == 1
        error = json.loads(result.output)["error"]
        assert error["code"] == "COMPLETION_BLOCKED"
        assert "Stale reachable_review_commits attestation" in error["message"]
        assert self.calls == 2

    def test_complete_retries_once(self, board: LocalBoard, invoke, cli, monkeypatch) -> None:  # noqa: ANN001
        import lattice.cli.task_cmds as task_cmds

        monkeypatch.setattr(task_cmds.subprocess, "check_output", lambda *a, **k: SHA_HEAD)
        task_id = _reviewed_task(board)
        fresh = [_entry(SHA_HEAD, ok=False), _entry(SHA_A)]
        cli([_entry(SHA_A)], fresh)
        result = invoke("complete", task_id, "--review", "ok", "--actor", "agent:t", "--json")
        assert result.exit_code == 0, result.output
        assert json.loads(result.output)["data"]["status"] == "done"
        assert self.calls == 2
