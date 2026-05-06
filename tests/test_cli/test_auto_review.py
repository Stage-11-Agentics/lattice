"""Tests for the CLI-side auto-fire helper (LAT-211).

``subprocess.Popen`` is patched throughout — no actual subprocess is
forked.  We verify argv composition, log-file lifecycle, claim
interaction, and the result-dict surface for every skip reason.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from unittest.mock import patch

import pytest

from lattice.cli import auto_review as cli_auto_review
from lattice.cli.auto_review import auto_fire_review, log_path_for
from lattice.core.auto_review import AUTO_REVIEW_ACTOR
from lattice.core.review import (
    claim_review_state,
    read_review_state,
    write_review_state,
)


@pytest.fixture
def lattice_dir(tmp_path: Path) -> Path:
    """Return a minimal .lattice directory for tests."""
    ld = tmp_path / ".lattice"
    ld.mkdir()
    return ld


class _FakeProc:
    def __init__(self, pid: int = 99999) -> None:
        self.pid = pid


# ---------------------------------------------------------------------------
# log_path_for
# ---------------------------------------------------------------------------


def test_log_path_for_uses_daemon_subdir(lattice_dir: Path) -> None:
    path = log_path_for(lattice_dir, "code-review", "task_abc")
    assert path == lattice_dir / ".daemon" / "auto-code-review-task_abc.log"


def test_log_path_for_plan_review(lattice_dir: Path) -> None:
    path = log_path_for(lattice_dir, "plan-review", "task_abc")
    assert path == lattice_dir / ".daemon" / "auto-plan-review-task_abc.log"


# ---------------------------------------------------------------------------
# auto_fire_review — happy path
# ---------------------------------------------------------------------------


def _patch_executable(path: str = "/usr/local/bin/lattice"):
    return patch.object(cli_auto_review, "find_lattice_executable", return_value=path)


class TestAutoFireReviewHappyPath:
    def test_fires_for_review_with_default_config(self, lattice_dir: Path) -> None:
        with (
            patch.object(
                cli_auto_review.subprocess, "Popen", return_value=_FakeProc(12345)
            ) as popen,
            _patch_executable("/usr/local/bin/lattice"),
        ):
            result = auto_fire_review(
                lattice_dir,
                "task_abc",
                "review",
                status_event_id="evt_xyz",
                config={},
                no_auto_review_flag=False,
            )

        assert result["fired"] is True
        assert result["review_type"] == "code-review"
        assert result["mode"] == "single"
        assert result["pid"] == 12345
        assert result["log_path"].endswith("/.daemon/auto-code-review-task_abc.log")
        assert "spawned_at" in result

        # argv composition
        popen.assert_called_once()
        argv = popen.call_args.args[0]
        assert argv == [
            "/usr/local/bin/lattice",
            "code-review",
            "task_abc",
            "--actor",
            AUTO_REVIEW_ACTOR,
            "--triggered-by",
            "evt_xyz",
        ]
        # detached and clean fds
        assert popen.call_args.kwargs["start_new_session"] is True
        assert popen.call_args.kwargs["close_fds"] is True
        # log file written with header + claim record landed on disk
        log = log_path_for(lattice_dir, "code-review", "task_abc")
        assert log.exists()
        header = log.read_text()
        assert header.startswith("# auto-code-review for task_abc started ")

        state = read_review_state(lattice_dir, "task_abc")
        assert state is not None
        assert state["auto_fired"] is True
        assert state["started_by_pid"] == os.getpid()
        assert state["mode"] == "single"
        assert state["review_type"] == "code-review"

    def test_fires_for_planned_with_triple_mode(self, lattice_dir: Path) -> None:
        with (
            patch.object(cli_auto_review.subprocess, "Popen", return_value=_FakeProc(7777)),
            _patch_executable(),
        ):
            result = auto_fire_review(
                lattice_dir,
                "task_def",
                "planned",
                status_event_id="evt_p",
                config={"plan_review_mode": "triple"},
                no_auto_review_flag=False,
            )
        assert result["fired"] is True
        assert result["review_type"] == "plan-review"
        assert result["mode"] == "triple"

    def test_log_fh_is_closed_in_parent(self, lattice_dir: Path) -> None:
        # Patch ``open`` for the auto_review module so we can assert close().
        opened_handles: list = []
        real_open = open

        def _spy_open(path, mode="r", *args, **kwargs):  # noqa: ANN001, ANN202
            fh = real_open(path, mode, *args, **kwargs)
            opened_handles.append(fh)
            return fh

        with (
            patch.object(cli_auto_review, "open", _spy_open, create=True),
            patch.object(cli_auto_review.subprocess, "Popen", return_value=_FakeProc()),
            _patch_executable(),
        ):
            auto_fire_review(
                lattice_dir,
                "task_abc",
                "review",
                status_event_id="evt_xyz",
                config={},
                no_auto_review_flag=False,
            )

        assert opened_handles, "open was not called"
        log_fh = opened_handles[0]
        assert log_fh.closed is True


# ---------------------------------------------------------------------------
# auto_fire_review — gating skip paths
# ---------------------------------------------------------------------------


class TestAutoFireReviewSkipPaths:
    def test_inline_mode_skip(self, lattice_dir: Path) -> None:
        with patch.object(cli_auto_review.subprocess, "Popen") as popen:
            result = auto_fire_review(
                lattice_dir,
                "task_abc",
                "review",
                status_event_id="evt_xyz",
                config={"review_mode": "inline"},
                no_auto_review_flag=False,
            )
        assert result == {"fired": False, "reason": "inline_mode"}
        popen.assert_not_called()
        # No claim was made.
        assert read_review_state(lattice_dir, "task_abc") is None

    def test_disabled_in_config_skip(self, lattice_dir: Path) -> None:
        with patch.object(cli_auto_review.subprocess, "Popen") as popen:
            result = auto_fire_review(
                lattice_dir,
                "task_abc",
                "review",
                status_event_id="evt_xyz",
                config={"auto_code_review_on_transition": False},
                no_auto_review_flag=False,
            )
        assert result == {"fired": False, "reason": "disabled_in_config"}
        popen.assert_not_called()

    def test_no_auto_review_flag_skip(self, lattice_dir: Path) -> None:
        with patch.object(cli_auto_review.subprocess, "Popen") as popen:
            result = auto_fire_review(
                lattice_dir,
                "task_abc",
                "review",
                status_event_id="evt_xyz",
                config={},
                no_auto_review_flag=True,
            )
        assert result == {"fired": False, "reason": "no_auto_review_flag"}
        popen.assert_not_called()

    def test_not_a_review_gate_skip(self, lattice_dir: Path) -> None:
        with patch.object(cli_auto_review.subprocess, "Popen") as popen:
            result = auto_fire_review(
                lattice_dir,
                "task_abc",
                "in_progress",
                status_event_id="evt_xyz",
                config={},
                no_auto_review_flag=False,
            )
        assert result == {"fired": False, "reason": "not_a_review_gate"}
        popen.assert_not_called()


# ---------------------------------------------------------------------------
# auto_fire_review — coordination + failure paths
# ---------------------------------------------------------------------------


class TestAutoFireReviewCoordination:
    def test_review_in_flight_when_live_other_pid_holds(self, lattice_dir: Path) -> None:
        ppid = os.getppid()
        if ppid == os.getpid() or ppid <= 1:
            pytest.skip("Need a usable parent pid to seed a live holder.")
        # Seed a pre-existing record whose holder is alive (test parent
        # process) — auto_fire_review must refuse.
        write_review_state(
            lattice_dir,
            {
                "task_id": "task_abc",
                "mode": "single",
                "review_type": "code-review",
                "started_at": "2026-05-06T00:00:00Z",
                "started_by_pid": ppid,
                "auto_fired": False,
                "agents": [],
            },
        )
        with (
            patch.object(cli_auto_review.subprocess, "Popen") as popen,
            _patch_executable(),
        ):
            result = auto_fire_review(
                lattice_dir,
                "task_abc",
                "review",
                status_event_id="evt_xyz",
                config={},
                no_auto_review_flag=False,
            )
        assert result["fired"] is False
        assert result["reason"] == "review_in_flight"
        assert result["holder_pid"] == ppid
        assert result["holder_auto_fired"] is False
        popen.assert_not_called()
        # Existing record untouched.
        state = read_review_state(lattice_dir, "task_abc")
        assert state is not None
        assert state["started_by_pid"] == ppid

    def test_executable_not_found(self, lattice_dir: Path) -> None:
        with (
            patch.object(cli_auto_review, "find_lattice_executable", return_value=None),
            patch.object(cli_auto_review.subprocess, "Popen") as popen,
        ):
            result = auto_fire_review(
                lattice_dir,
                "task_abc",
                "review",
                status_event_id="evt_xyz",
                config={},
                no_auto_review_flag=False,
            )
        assert result == {"fired": False, "reason": "executable_not_found"}
        popen.assert_not_called()
        # Phantom claim was released.
        assert read_review_state(lattice_dir, "task_abc") is None

    def test_popen_failure_releases_claim(self, lattice_dir: Path) -> None:
        with (
            _patch_executable(),
            patch.object(
                cli_auto_review.subprocess,
                "Popen",
                side_effect=OSError("no fork for you"),
            ),
        ):
            result = auto_fire_review(
                lattice_dir,
                "task_abc",
                "review",
                status_event_id="evt_xyz",
                config={},
                no_auto_review_flag=False,
            )
        assert result["fired"] is False
        assert result["reason"].startswith("spawn_failed:")
        assert "no fork" in result["reason"]
        # Claim released so a retry isn't blocked.
        assert read_review_state(lattice_dir, "task_abc") is None

    def test_claim_records_survive_after_fire(self, lattice_dir: Path) -> None:
        # Sanity — after a successful fire the claim is left in place
        # for the spawned child to adopt.
        with (
            patch.object(cli_auto_review.subprocess, "Popen", return_value=_FakeProc(42)),
            _patch_executable(),
        ):
            auto_fire_review(
                lattice_dir,
                "task_abc",
                "review",
                status_event_id="evt_xyz",
                config={},
                no_auto_review_flag=False,
            )
        state = read_review_state(lattice_dir, "task_abc")
        assert state is not None
        assert state["auto_fired"] is True
        assert state["started_by_pid"] == os.getpid()
        # JSON is valid (no leftover serialization quirks).
        assert json.dumps(state)


# ---------------------------------------------------------------------------
# Sanity: claim_review_state is what's actually being called
# ---------------------------------------------------------------------------


def test_uses_claim_review_state_under_the_hood(lattice_dir: Path) -> None:
    # Defensive — if someone bypasses the helper, this test catches it.
    seen: list = []
    real_claim = claim_review_state

    def _spy(*args, **kwargs):  # noqa: ANN001, ANN202
        seen.append((args, kwargs))
        return real_claim(*args, **kwargs)

    with (
        patch.object(cli_auto_review, "claim_review_state", _spy),
        patch.object(cli_auto_review.subprocess, "Popen", return_value=_FakeProc()),
        _patch_executable(),
    ):
        auto_fire_review(
            lattice_dir,
            "task_abc",
            "review",
            status_event_id="evt_xyz",
            config={},
            no_auto_review_flag=False,
        )
    assert seen, "claim_review_state was not invoked"
    _, kwargs = seen[0]
    assert kwargs["auto_fired"] is True
    assert kwargs["started_by_pid"] == os.getpid()
