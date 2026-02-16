"""Tests for the `lattice weather` CLI command."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

from click.testing import CliRunner

from lattice.cli.main import cli
from lattice.cli.weather_cmds import _determine_weather


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _init_lattice(root: Path, project_code: str = "TST") -> Path:
    """Initialize a minimal .lattice/ directory for testing."""
    from lattice.core.config import default_config, serialize_config
    from lattice.storage.fs import LATTICE_DIR, atomic_write, ensure_lattice_dirs

    ensure_lattice_dirs(root)
    lattice_dir = root / LATTICE_DIR
    config = dict(default_config())
    config["project_code"] = project_code
    config["instance_name"] = "Test Project"
    atomic_write(lattice_dir / "config.json", serialize_config(config))
    (lattice_dir / "events" / "_lifecycle.jsonl").touch()
    return lattice_dir


def _write_task_snapshot(
    lattice_dir: Path,
    task_id: str,
    *,
    title: str = "Test task",
    status: str = "backlog",
    priority: str = "medium",
    short_id: str | None = None,
    assigned_to: str | None = None,
    updated_at: str | None = None,
    tags: list[str] | None = None,
) -> dict:
    """Write a task snapshot JSON file and return the snapshot dict."""
    now = datetime.now(timezone.utc)
    if updated_at is None:
        updated_at = now.strftime("%Y-%m-%dT%H:%M:%SZ")

    snap = {
        "id": task_id,
        "title": title,
        "status": status,
        "priority": priority,
        "type": "task",
        "assigned_to": assigned_to,
        "updated_at": updated_at,
        "created_at": updated_at,
        "tags": tags or [],
    }
    if short_id:
        snap["short_id"] = short_id

    path = lattice_dir / "tasks" / f"{task_id}.json"
    path.write_text(json.dumps(snap, sort_keys=True, indent=2) + "\n")
    return snap


def _write_event(
    lattice_dir: Path,
    task_id: str,
    event_type: str = "status_changed",
    ts: str | None = None,
) -> None:
    """Append a minimal event to the task's event log."""
    now = datetime.now(timezone.utc)
    if ts is None:
        ts = now.strftime("%Y-%m-%dT%H:%M:%SZ")

    ev = {
        "id": f"ev_{task_id[-6:]}_{event_type[:4]}",
        "ts": ts,
        "type": event_type,
        "task_id": task_id,
        "actor": "human:test",
        "data": {},
    }
    path = lattice_dir / "events" / f"{task_id}.jsonl"
    with open(path, "a") as f:
        f.write(json.dumps(ev, sort_keys=True, separators=(",", ":")) + "\n")


def _ts_ago(hours: float = 0, days: float = 0) -> str:
    """Return a UTC timestamp string *hours*/*days* in the past."""
    dt = datetime.now(timezone.utc) - timedelta(hours=hours, days=days)
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


# ---------------------------------------------------------------------------
# Weather metaphor unit tests
# ---------------------------------------------------------------------------


class TestDetermineWeather:
    """Test the heuristic weather metaphor selection."""

    def test_clear_skies_no_issues(self) -> None:
        result = _determine_weather(
            stale_count=0, active_count=10, wip_breaches=0, recently_completed=3
        )
        assert result == "Clear skies"

    def test_fair_weather_minor_staleness(self) -> None:
        result = _determine_weather(
            stale_count=2, active_count=10, wip_breaches=0, recently_completed=1
        )
        assert result == "Fair weather"

    def test_partly_cloudy_some_stale(self) -> None:
        result = _determine_weather(
            stale_count=3, active_count=10, wip_breaches=0, recently_completed=0
        )
        assert result == "Partly cloudy"

    def test_partly_cloudy_one_wip_breach(self) -> None:
        result = _determine_weather(
            stale_count=0, active_count=10, wip_breaches=1, recently_completed=0
        )
        assert result == "Partly cloudy"

    def test_overcast_many_stale(self) -> None:
        result = _determine_weather(
            stale_count=5, active_count=20, wip_breaches=0, recently_completed=0
        )
        assert result == "Overcast"

    def test_overcast_multiple_wip_breaches(self) -> None:
        result = _determine_weather(
            stale_count=0, active_count=10, wip_breaches=2, recently_completed=0
        )
        assert result == "Overcast"

    def test_stormy_majority_stale(self) -> None:
        result = _determine_weather(
            stale_count=6, active_count=10, wip_breaches=0, recently_completed=0
        )
        assert result == "Stormy"

    def test_stormy_critical_wip_breaches(self) -> None:
        result = _determine_weather(
            stale_count=0, active_count=10, wip_breaches=3, recently_completed=0
        )
        assert result == "Stormy"

    def test_clear_skies_empty_project(self) -> None:
        result = _determine_weather(
            stale_count=0, active_count=0, wip_breaches=0, recently_completed=0
        )
        assert result == "Clear skies"

    def test_partly_cloudy_four_stale(self) -> None:
        """4 stale tasks is in the 3-5 range -> partly cloudy."""
        result = _determine_weather(
            stale_count=4, active_count=20, wip_breaches=0, recently_completed=0
        )
        assert result == "Partly cloudy"


# ---------------------------------------------------------------------------
# Empty project
# ---------------------------------------------------------------------------


class TestWeatherEmptyProject:
    """Weather report on a project with no tasks."""

    def test_empty_project_text(self, tmp_path: Path) -> None:
        _init_lattice(tmp_path)
        runner = CliRunner()
        result = runner.invoke(cli, ["weather"], env={"LATTICE_ROOT": str(tmp_path)})
        assert result.exit_code == 0
        assert "Weather Report" in result.output
        assert "Clear skies" in result.output
        assert "Active tasks:      0" in result.output

    def test_empty_project_json(self, tmp_path: Path) -> None:
        _init_lattice(tmp_path)
        runner = CliRunner()
        result = runner.invoke(cli, ["weather", "--json"], env={"LATTICE_ROOT": str(tmp_path)})
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert data["ok"] is True
        assert data["data"]["headline"]["weather"] == "Clear skies"
        assert data["data"]["vital_signs"]["active_tasks"] == 0

    def test_empty_project_markdown(self, tmp_path: Path) -> None:
        _init_lattice(tmp_path)
        runner = CliRunner()
        result = runner.invoke(cli, ["weather", "--markdown"], env={"LATTICE_ROOT": str(tmp_path)})
        assert result.exit_code == 0
        assert "# Test Project Weather Report" in result.output
        assert "**Forecast:** Clear skies" in result.output


# ---------------------------------------------------------------------------
# Tasks in various states
# ---------------------------------------------------------------------------


class TestWeatherWithTasks:
    """Weather report with tasks in different states."""

    def test_vital_signs_counts(self, tmp_path: Path) -> None:
        lattice_dir = _init_lattice(tmp_path)

        # Create tasks in various states
        _write_task_snapshot(lattice_dir, "task_001", title="Backlog task", status="backlog")
        _write_task_snapshot(
            lattice_dir,
            "task_002",
            title="In progress",
            status="in_implementation",
            assigned_to="human:atin",
        )
        _write_task_snapshot(lattice_dir, "task_003", title="Done task", status="done")
        _write_task_snapshot(lattice_dir, "task_004", title="Planning", status="in_planning")

        runner = CliRunner()
        result = runner.invoke(cli, ["weather", "--json"], env={"LATTICE_ROOT": str(tmp_path)})
        assert result.exit_code == 0
        data = json.loads(result.output)["data"]

        assert data["vital_signs"]["active_tasks"] == 4
        assert data["vital_signs"]["in_progress"] == 2  # in_implementation + in_planning

    def test_up_next_picks_backlog_and_planned(self, tmp_path: Path) -> None:
        lattice_dir = _init_lattice(tmp_path)

        _write_task_snapshot(
            lattice_dir,
            "task_001",
            title="High pri",
            status="backlog",
            priority="high",
            short_id="TST-1",
        )
        _write_task_snapshot(
            lattice_dir,
            "task_002",
            title="Low pri",
            status="planned",
            priority="low",
            short_id="TST-2",
        )
        _write_task_snapshot(
            lattice_dir,
            "task_003",
            title="In progress",
            status="in_implementation",
        )

        runner = CliRunner()
        result = runner.invoke(cli, ["weather", "--json"], env={"LATTICE_ROOT": str(tmp_path)})
        data = json.loads(result.output)["data"]

        up_next_ids = [t["id"] for t in data["up_next"]]
        assert "TST-1" in up_next_ids
        assert "TST-2" in up_next_ids
        # In-progress should NOT be in up_next
        assert "task_003" not in up_next_ids

    def test_up_next_ordered_by_priority(self, tmp_path: Path) -> None:
        lattice_dir = _init_lattice(tmp_path)

        _write_task_snapshot(
            lattice_dir,
            "task_001",
            title="Low",
            status="backlog",
            priority="low",
            short_id="TST-1",
        )
        _write_task_snapshot(
            lattice_dir,
            "task_002",
            title="Critical",
            status="backlog",
            priority="critical",
            short_id="TST-2",
        )
        _write_task_snapshot(
            lattice_dir,
            "task_003",
            title="High",
            status="backlog",
            priority="high",
            short_id="TST-3",
        )

        runner = CliRunner()
        result = runner.invoke(cli, ["weather", "--json"], env={"LATTICE_ROOT": str(tmp_path)})
        data = json.loads(result.output)["data"]

        priorities = [t["priority"] for t in data["up_next"]]
        assert priorities == ["critical", "high", "low"]


# ---------------------------------------------------------------------------
# Recently completed detection
# ---------------------------------------------------------------------------


class TestRecentlyCompleted:
    """Test detection of recently completed tasks."""

    def test_done_in_last_24h(self, tmp_path: Path) -> None:
        lattice_dir = _init_lattice(tmp_path)

        _write_task_snapshot(
            lattice_dir,
            "task_001",
            title="Just done",
            status="done",
            short_id="TST-1",
            updated_at=_ts_ago(hours=2),
        )

        runner = CliRunner()
        result = runner.invoke(cli, ["weather", "--json"], env={"LATTICE_ROOT": str(tmp_path)})
        data = json.loads(result.output)["data"]

        assert len(data["recently_completed"]) == 1
        assert data["recently_completed"][0]["id"] == "TST-1"

    def test_falls_back_to_72h_if_none_in_24h(self, tmp_path: Path) -> None:
        lattice_dir = _init_lattice(tmp_path)

        # Done 2 days ago (outside 24h, inside 72h)
        _write_task_snapshot(
            lattice_dir,
            "task_001",
            title="Done 2d ago",
            status="done",
            short_id="TST-1",
            updated_at=_ts_ago(days=2),
        )

        runner = CliRunner()
        result = runner.invoke(cli, ["weather", "--json"], env={"LATTICE_ROOT": str(tmp_path)})
        data = json.loads(result.output)["data"]

        assert len(data["recently_completed"]) == 1
        assert data["recently_completed"][0]["id"] == "TST-1"

    def test_no_recent_completions(self, tmp_path: Path) -> None:
        lattice_dir = _init_lattice(tmp_path)

        # Done 10 days ago (outside both windows)
        _write_task_snapshot(
            lattice_dir,
            "task_001",
            title="Old done",
            status="done",
            updated_at=_ts_ago(days=10),
        )

        runner = CliRunner()
        result = runner.invoke(cli, ["weather", "--json"], env={"LATTICE_ROOT": str(tmp_path)})
        data = json.loads(result.output)["data"]

        assert len(data["recently_completed"]) == 0


# ---------------------------------------------------------------------------
# Stale task detection
# ---------------------------------------------------------------------------


class TestStaleDetection:
    """Test stale task detection in weather reports."""

    def test_stale_tasks_appear_in_attention(self, tmp_path: Path) -> None:
        lattice_dir = _init_lattice(tmp_path)

        _write_task_snapshot(
            lattice_dir,
            "task_001",
            title="Stale task",
            status="in_implementation",
            short_id="TST-1",
            updated_at=_ts_ago(days=10),
        )
        _write_task_snapshot(
            lattice_dir,
            "task_002",
            title="Fresh task",
            status="in_implementation",
            updated_at=_ts_ago(hours=1),
        )

        runner = CliRunner()
        result = runner.invoke(cli, ["weather", "--json"], env={"LATTICE_ROOT": str(tmp_path)})
        data = json.loads(result.output)["data"]

        stale_items = [a for a in data["attention"] if a["type"] == "stale"]
        assert len(stale_items) == 1
        assert stale_items[0]["id"] == "TST-1"

    def test_no_stale_tasks_no_attention(self, tmp_path: Path) -> None:
        lattice_dir = _init_lattice(tmp_path)

        _write_task_snapshot(
            lattice_dir,
            "task_001",
            title="Fresh",
            status="backlog",
            updated_at=_ts_ago(hours=1),
        )

        runner = CliRunner()
        result = runner.invoke(cli, ["weather", "--json"], env={"LATTICE_ROOT": str(tmp_path)})
        data = json.loads(result.output)["data"]

        stale_items = [a for a in data["attention"] if a["type"] == "stale"]
        assert len(stale_items) == 0


# ---------------------------------------------------------------------------
# Unassigned active tasks
# ---------------------------------------------------------------------------


class TestUnassignedActive:
    """Test unassigned in-progress detection."""

    def test_unassigned_in_progress_flagged(self, tmp_path: Path) -> None:
        lattice_dir = _init_lattice(tmp_path)

        _write_task_snapshot(
            lattice_dir,
            "task_001",
            title="No owner",
            status="in_implementation",
            short_id="TST-1",
        )

        runner = CliRunner()
        result = runner.invoke(cli, ["weather", "--json"], env={"LATTICE_ROOT": str(tmp_path)})
        data = json.loads(result.output)["data"]

        unassigned = [a for a in data["attention"] if a["type"] == "unassigned_active"]
        assert len(unassigned) == 1
        assert unassigned[0]["id"] == "TST-1"

    def test_assigned_in_progress_not_flagged(self, tmp_path: Path) -> None:
        lattice_dir = _init_lattice(tmp_path)

        _write_task_snapshot(
            lattice_dir,
            "task_001",
            title="Has owner",
            status="in_implementation",
            assigned_to="human:atin",
        )

        runner = CliRunner()
        result = runner.invoke(cli, ["weather", "--json"], env={"LATTICE_ROOT": str(tmp_path)})
        data = json.loads(result.output)["data"]

        unassigned = [a for a in data["attention"] if a["type"] == "unassigned_active"]
        assert len(unassigned) == 0


# ---------------------------------------------------------------------------
# Event counting (24h window)
# ---------------------------------------------------------------------------


class TestEventCounting:
    """Test that events_24h counts recent events."""

    def test_counts_recent_events(self, tmp_path: Path) -> None:
        lattice_dir = _init_lattice(tmp_path)

        _write_task_snapshot(lattice_dir, "task_001", title="Task A", status="backlog")
        _write_event(lattice_dir, "task_001", ts=_ts_ago(hours=1))
        _write_event(lattice_dir, "task_001", event_type="comment_added", ts=_ts_ago(hours=5))
        # Old event — should not count
        _write_event(lattice_dir, "task_001", event_type="field_updated", ts=_ts_ago(days=5))

        runner = CliRunner()
        result = runner.invoke(cli, ["weather", "--json"], env={"LATTICE_ROOT": str(tmp_path)})
        data = json.loads(result.output)["data"]

        assert data["vital_signs"]["events_24h"] == 2


# ---------------------------------------------------------------------------
# Markdown output format
# ---------------------------------------------------------------------------


class TestMarkdownOutput:
    """Test markdown formatting specifics."""

    def test_markdown_has_expected_sections(self, tmp_path: Path) -> None:
        lattice_dir = _init_lattice(tmp_path)

        _write_task_snapshot(lattice_dir, "task_001", title="A task", status="backlog")

        runner = CliRunner()
        result = runner.invoke(cli, ["weather", "--markdown"], env={"LATTICE_ROOT": str(tmp_path)})
        assert result.exit_code == 0
        output = result.output

        assert "# Test Project Weather Report" in output
        assert "## Vital Signs" in output
        assert "## Attention Needed" in output
        assert "## Recently Completed" in output
        assert "## Up Next" in output

    def test_markdown_table_format(self, tmp_path: Path) -> None:
        _init_lattice(tmp_path)
        runner = CliRunner()
        result = runner.invoke(cli, ["weather", "--markdown"], env={"LATTICE_ROOT": str(tmp_path)})
        assert result.exit_code == 0
        assert "| Metric | Value |" in result.output
        assert "|--------|-------|" in result.output


# ---------------------------------------------------------------------------
# JSON output structure
# ---------------------------------------------------------------------------


class TestJsonOutput:
    """Test JSON output envelope and structure."""

    def test_json_envelope_structure(self, tmp_path: Path) -> None:
        _init_lattice(tmp_path)
        runner = CliRunner()
        result = runner.invoke(cli, ["weather", "--json"], env={"LATTICE_ROOT": str(tmp_path)})
        assert result.exit_code == 0
        parsed = json.loads(result.output)

        assert parsed["ok"] is True
        assert "data" in parsed

        data = parsed["data"]
        assert "headline" in data
        assert "vital_signs" in data
        assert "attention" in data
        assert "recently_completed" in data
        assert "up_next" in data

    def test_json_headline_fields(self, tmp_path: Path) -> None:
        _init_lattice(tmp_path)
        runner = CliRunner()
        result = runner.invoke(cli, ["weather", "--json"], env={"LATTICE_ROOT": str(tmp_path)})
        data = json.loads(result.output)["data"]

        headline = data["headline"]
        assert "project" in headline
        assert "date" in headline
        assert "weather" in headline
        assert headline["project"] == "Test Project"

    def test_json_vital_signs_fields(self, tmp_path: Path) -> None:
        _init_lattice(tmp_path)
        runner = CliRunner()
        result = runner.invoke(cli, ["weather", "--json"], env={"LATTICE_ROOT": str(tmp_path)})
        data = json.loads(result.output)["data"]

        vs = data["vital_signs"]
        assert "active_tasks" in vs
        assert "in_progress" in vs
        assert "done_recently" in vs
        assert "events_24h" in vs


# ---------------------------------------------------------------------------
# Text output format
# ---------------------------------------------------------------------------


class TestTextOutput:
    """Test plain text output formatting."""

    def test_text_has_weather_indicator(self, tmp_path: Path) -> None:
        _init_lattice(tmp_path)
        runner = CliRunner()
        result = runner.invoke(cli, ["weather"], env={"LATTICE_ROOT": str(tmp_path)})
        assert result.exit_code == 0
        assert "[OK]" in result.output  # Clear skies indicator
        assert "Clear skies" in result.output

    def test_text_shows_attention_items(self, tmp_path: Path) -> None:
        lattice_dir = _init_lattice(tmp_path)

        _write_task_snapshot(
            lattice_dir,
            "task_001",
            title="Old task",
            status="in_implementation",
            short_id="TST-1",
            updated_at=_ts_ago(days=10),
        )

        runner = CliRunner()
        result = runner.invoke(cli, ["weather"], env={"LATTICE_ROOT": str(tmp_path)})
        assert result.exit_code == 0
        assert "[STALE]" in result.output
        assert "TST-1" in result.output


# ---------------------------------------------------------------------------
# WIP breach detection
# ---------------------------------------------------------------------------


class TestWipBreaches:
    """Test WIP limit breach detection in weather."""

    def test_wip_breach_in_attention(self, tmp_path: Path) -> None:
        lattice_dir = _init_lattice(tmp_path)

        # Default WIP limit for in_review is 5 — create 6 tasks in review
        for i in range(6):
            _write_task_snapshot(
                lattice_dir,
                f"task_{i:03d}",
                title=f"Review {i}",
                status="in_review",
                assigned_to="human:atin",
            )

        runner = CliRunner()
        result = runner.invoke(cli, ["weather", "--json"], env={"LATTICE_ROOT": str(tmp_path)})
        data = json.loads(result.output)["data"]

        wip_items = [a for a in data["attention"] if a["type"] == "wip_breach"]
        assert len(wip_items) >= 1
        assert any(w["status"] == "in_review" for w in wip_items)
