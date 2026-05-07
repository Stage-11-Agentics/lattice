"""Tests for core review logic: failure tracking, temp cleanup, state helpers."""

from __future__ import annotations

import json
import tempfile
from pathlib import Path

import pytest

from lattice.core.review import (
    DEFAULT_AGENT_TIMEOUT,
    FAILURE_THRESHOLD,
    _extract_actor_str,
    cleanup_temp_files,
    clear_review_state,
    count_agent_failures,
    read_review_state,
    record_agent_failure,
    write_review_state,
)


@pytest.fixture
def lattice_dir(tmp_path: Path) -> Path:
    """Create a minimal .lattice directory."""
    ld = tmp_path / ".lattice"
    ld.mkdir()
    return ld


# ---------------------------------------------------------------------------
# Review state helpers
# ---------------------------------------------------------------------------


class TestReviewState:
    def test_write_read_clear(self, lattice_dir: Path) -> None:
        state = {"task_id": "t1", "mode": "single", "agents": []}
        write_review_state(lattice_dir, state)
        loaded = read_review_state(lattice_dir, "t1")
        assert loaded is not None
        assert loaded["task_id"] == "t1"
        assert loaded["mode"] == "single"

        clear_review_state(lattice_dir, "t1")
        assert read_review_state(lattice_dir, "t1") is None

    def test_read_nonexistent(self, lattice_dir: Path) -> None:
        assert read_review_state(lattice_dir, "nonexistent") is None

    def test_clear_nonexistent(self, lattice_dir: Path) -> None:
        # Should not raise
        clear_review_state(lattice_dir, "nonexistent")


# ---------------------------------------------------------------------------
# Persistent failure tracking
# ---------------------------------------------------------------------------


class TestFailureTracking:
    def test_record_and_count(self, lattice_dir: Path) -> None:
        count = record_agent_failure(lattice_dir, "codex", "task_abc")
        assert count == 1
        count = record_agent_failure(lattice_dir, "codex", "task_def")
        assert count == 2
        assert count_agent_failures(lattice_dir, "codex") == 2
        # Different agent should have 0
        assert count_agent_failures(lattice_dir, "claude") == 0

    def test_count_empty(self, lattice_dir: Path) -> None:
        assert count_agent_failures(lattice_dir, "gemini") == 0

    def test_threshold_constant(self) -> None:
        assert FAILURE_THRESHOLD == 2

    def test_failures_persisted_as_jsonl(self, lattice_dir: Path) -> None:
        record_agent_failure(lattice_dir, "claude", "t1")
        record_agent_failure(lattice_dir, "codex", "t2")
        path = lattice_dir / "review_state" / "failures.jsonl"
        assert path.exists()
        lines = path.read_text(encoding="utf-8").strip().splitlines()
        assert len(lines) == 2
        entry = json.loads(lines[0])
        assert entry["agent"] == "claude"
        assert entry["task_id"] == "t1"
        assert "timestamp" in entry


# ---------------------------------------------------------------------------
# Temp file cleanup
# ---------------------------------------------------------------------------


class TestTempCleanup:
    def test_cleanup_removes_matching_files(self) -> None:
        # Create temp files matching the pattern
        f1 = tempfile.NamedTemporaryFile(prefix="lattice-review-", suffix=".md", delete=False)
        f1.close()
        p1 = Path(f1.name)
        assert p1.exists()

        removed = cleanup_temp_files()
        assert removed >= 1
        assert not p1.exists()

    def test_cleanup_with_no_files(self) -> None:
        # Should not raise, returns 0
        removed = cleanup_temp_files()
        assert removed >= 0


# ---------------------------------------------------------------------------
# Actor extraction
# ---------------------------------------------------------------------------


class TestExtractActorStr:
    def test_string_actor(self) -> None:
        assert _extract_actor_str("agent:claude") == "agent:claude"

    def test_dict_actor_with_name(self) -> None:
        assert _extract_actor_str({"name": "agent:opus"}) == "agent:opus"

    def test_dict_actor_with_base_name(self) -> None:
        assert _extract_actor_str({"base_name": "system:bot"}) == "system:bot"

    def test_fallback(self) -> None:
        assert _extract_actor_str(42) == "system:lattice"


# ---------------------------------------------------------------------------
# Config default
# ---------------------------------------------------------------------------


class TestConfigTimeout:
    def test_default_timeout_in_config(self) -> None:
        from lattice.core.config import default_config

        cfg = default_config()
        assert cfg["review_timeout_seconds"] == 600

    def test_default_agent_timeout_constant(self) -> None:
        assert DEFAULT_AGENT_TIMEOUT == 600
