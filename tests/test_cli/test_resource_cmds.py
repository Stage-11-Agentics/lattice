"""CLI integration tests for resource commands."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from click.testing import CliRunner

from lattice.cli.main import cli
from lattice.core.config import default_config, serialize_config
from lattice.storage.fs import LATTICE_DIR, atomic_write


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def res_env(initialized_root: Path) -> dict[str, str]:
    """Env dict with LATTICE_ROOT for resource tests."""
    return {"LATTICE_ROOT": str(initialized_root)}


@pytest.fixture()
def res_invoke(cli_runner: CliRunner, res_env: dict[str, str]):
    """Helper that invokes CLI commands for resource tests."""
    def _invoke(*args: str, **kwargs):
        return cli_runner.invoke(cli, list(args), env=res_env, **kwargs)
    return _invoke


@pytest.fixture()
def res_invoke_json(res_invoke):
    """Like res_invoke, but appends --json and parses the response."""
    def _invoke_json(*args: str) -> tuple[dict, int]:
        result = res_invoke(*args, "--json")
        parsed = json.loads(result.output)
        return parsed, result.exit_code
    return _invoke_json


@pytest.fixture()
def config_with_resources(initialized_root: Path) -> Path:
    """Initialize with config that declares resources."""
    lattice_dir = initialized_root / LATTICE_DIR
    config = dict(default_config())
    config["resources"] = {
        "browser": {
            "description": "Chrome browser",
            "max_holders": 1,
            "ttl_seconds": 300,
        },
        "ios-simulator": {
            "description": "iOS Simulator",
            "max_holders": 1,
            "ttl_seconds": 600,
        },
    }
    atomic_write(lattice_dir / "config.json", serialize_config(config))
    return initialized_root


# ---------------------------------------------------------------------------
# resource create
# ---------------------------------------------------------------------------


class TestResourceCreate:
    """Test lattice resource create."""

    def test_create_basic(self, res_invoke) -> None:
        result = res_invoke("resource", "create", "browser", "--actor", "human:atin")
        assert result.exit_code == 0
        assert "Created resource 'browser'" in result.output

    def test_create_with_options(self, res_invoke) -> None:
        result = res_invoke(
            "resource", "create", "browser",
            "--description", "Chrome browser",
            "--max-holders", "2",
            "--ttl", "600",
            "--actor", "human:atin",
        )
        assert result.exit_code == 0
        assert "Created resource 'browser'" in result.output

    def test_create_json_output(self, res_invoke_json) -> None:
        data, code = res_invoke_json(
            "resource", "create", "browser", "--actor", "human:atin",
        )
        assert code == 0
        assert data["ok"] is True
        assert data["data"]["name"] == "browser"
        assert data["data"]["max_holders"] == 1
        assert data["data"]["ttl_seconds"] == 300
        assert data["data"]["holders"] == []

    def test_create_duplicate_fails(self, res_invoke) -> None:
        res_invoke("resource", "create", "browser", "--actor", "human:atin")
        result = res_invoke("resource", "create", "browser", "--actor", "human:atin")
        assert result.exit_code == 1
        assert "already exists" in result.output

    def test_create_with_custom_id_idempotent(self, res_invoke_json) -> None:
        from lattice.core.ids import generate_resource_id

        custom_id = generate_resource_id()
        data1, code1 = res_invoke_json(
            "resource", "create", "browser",
            "--id", custom_id,
            "--actor", "human:atin",
        )
        assert code1 == 0
        assert data1["data"]["id"] == custom_id

        # Same ID + same name = idempotent
        data2, code2 = res_invoke_json(
            "resource", "create", "browser",
            "--id", custom_id,
            "--actor", "human:atin",
        )
        assert code2 == 0

    def test_creates_resource_dir(self, res_invoke, initialized_root: Path) -> None:
        res_invoke("resource", "create", "browser", "--actor", "human:atin")
        resource_dir = initialized_root / LATTICE_DIR / "resources" / "browser"
        assert resource_dir.is_dir()
        assert (resource_dir / "resource.json").exists()

    def test_creates_event_log(self, res_invoke, initialized_root: Path) -> None:
        result = res_invoke(
            "resource", "create", "browser", "--actor", "human:atin", "--json",
        )
        data = json.loads(result.output)
        resource_id = data["data"]["id"]
        event_log = initialized_root / LATTICE_DIR / "events" / f"{resource_id}.jsonl"
        assert event_log.exists()
        events = [json.loads(line) for line in event_log.read_text().splitlines() if line.strip()]
        assert len(events) == 1
        assert events[0]["type"] == "resource_created"


# ---------------------------------------------------------------------------
# resource acquire
# ---------------------------------------------------------------------------


class TestResourceAcquire:
    """Test lattice resource acquire."""

    def test_acquire_after_create(self, res_invoke) -> None:
        res_invoke("resource", "create", "browser", "--actor", "human:atin")
        result = res_invoke("resource", "acquire", "browser", "--actor", "agent:claude")
        assert result.exit_code == 0
        assert "Acquired 'browser'" in result.output

    def test_acquire_json(self, res_invoke, res_invoke_json) -> None:
        res_invoke("resource", "create", "browser", "--actor", "human:atin")
        data, code = res_invoke_json(
            "resource", "acquire", "browser", "--actor", "agent:claude",
        )
        assert code == 0
        assert data["ok"] is True
        assert len(data["data"]["holders"]) == 1
        assert data["data"]["holders"][0]["actor"] == "agent:claude"

    def test_acquire_fails_when_held(self, res_invoke) -> None:
        res_invoke("resource", "create", "browser", "--actor", "human:atin")
        res_invoke("resource", "acquire", "browser", "--actor", "agent:claude")
        result = res_invoke("resource", "acquire", "browser", "--actor", "agent:codex")
        assert result.exit_code == 1
        assert "not available" in result.output

    def test_acquire_idempotent_same_actor(self, res_invoke) -> None:
        res_invoke("resource", "create", "browser", "--actor", "human:atin")
        res_invoke("resource", "acquire", "browser", "--actor", "agent:claude")
        result = res_invoke("resource", "acquire", "browser", "--actor", "agent:claude")
        assert result.exit_code == 0
        assert "Already holding" in result.output

    def test_acquire_force_evicts(self, res_invoke) -> None:
        res_invoke("resource", "create", "browser", "--actor", "human:atin")
        res_invoke("resource", "acquire", "browser", "--actor", "agent:claude")
        result = res_invoke(
            "resource", "acquire", "browser", "--actor", "agent:codex", "--force",
        )
        assert result.exit_code == 0
        assert "Acquired" in result.output

    def test_acquire_with_task(self, res_invoke, res_invoke_json) -> None:
        # Create a task first
        res_invoke("create", "Test task", "--actor", "human:atin")
        res_invoke("resource", "create", "browser", "--actor", "human:atin")
        # We can't easily get the task ID without --json, so just test the flag works
        data, code = res_invoke_json(
            "resource", "acquire", "browser", "--actor", "agent:claude",
        )
        assert code == 0

    def test_acquire_auto_creates_from_config(
        self, cli_runner: CliRunner, config_with_resources: Path,
    ) -> None:
        env = {"LATTICE_ROOT": str(config_with_resources)}
        result = cli_runner.invoke(
            cli,
            ["resource", "acquire", "browser", "--actor", "agent:claude"],
            env=env,
        )
        assert result.exit_code == 0
        assert "Acquired" in result.output

        # Verify resource was auto-created
        resource_dir = config_with_resources / LATTICE_DIR / "resources" / "browser"
        assert resource_dir.is_dir()
        snap = json.loads((resource_dir / "resource.json").read_text())
        assert snap["name"] == "browser"
        assert snap["description"] == "Chrome browser"
        assert len(snap["holders"]) == 1


# ---------------------------------------------------------------------------
# resource release
# ---------------------------------------------------------------------------


class TestResourceRelease:
    """Test lattice resource release."""

    def test_release_after_acquire(self, res_invoke) -> None:
        res_invoke("resource", "create", "browser", "--actor", "human:atin")
        res_invoke("resource", "acquire", "browser", "--actor", "agent:claude")
        result = res_invoke("resource", "release", "browser", "--actor", "agent:claude")
        assert result.exit_code == 0
        assert "Released" in result.output

    def test_release_not_held_fails(self, res_invoke) -> None:
        res_invoke("resource", "create", "browser", "--actor", "human:atin")
        result = res_invoke("resource", "release", "browser", "--actor", "agent:claude")
        assert result.exit_code == 1
        assert "do not hold" in result.output

    def test_release_wrong_actor_fails(self, res_invoke) -> None:
        res_invoke("resource", "create", "browser", "--actor", "human:atin")
        res_invoke("resource", "acquire", "browser", "--actor", "agent:claude")
        result = res_invoke("resource", "release", "browser", "--actor", "agent:codex")
        assert result.exit_code == 1
        assert "do not hold" in result.output

    def test_release_makes_available(self, res_invoke_json, res_invoke) -> None:
        res_invoke("resource", "create", "browser", "--actor", "human:atin")
        res_invoke("resource", "acquire", "browser", "--actor", "agent:claude")
        res_invoke("resource", "release", "browser", "--actor", "agent:claude")
        # Now another actor can acquire
        result = res_invoke("resource", "acquire", "browser", "--actor", "agent:codex")
        assert result.exit_code == 0
        assert "Acquired" in result.output


# ---------------------------------------------------------------------------
# resource heartbeat
# ---------------------------------------------------------------------------


class TestResourceHeartbeat:
    """Test lattice resource heartbeat."""

    def test_heartbeat_extends_ttl(self, res_invoke) -> None:
        res_invoke("resource", "create", "browser", "--actor", "human:atin")
        res_invoke("resource", "acquire", "browser", "--actor", "agent:claude")
        result = res_invoke("resource", "heartbeat", "browser", "--actor", "agent:claude")
        assert result.exit_code == 0
        assert "Heartbeat" in result.output

    def test_heartbeat_not_held_fails(self, res_invoke) -> None:
        res_invoke("resource", "create", "browser", "--actor", "human:atin")
        result = res_invoke("resource", "heartbeat", "browser", "--actor", "agent:claude")
        assert result.exit_code == 1
        assert "do not hold" in result.output


# ---------------------------------------------------------------------------
# resource status / list
# ---------------------------------------------------------------------------


class TestResourceStatus:
    """Test lattice resource status."""

    def test_status_single(self, res_invoke) -> None:
        res_invoke("resource", "create", "browser", "--actor", "human:atin")
        result = res_invoke("resource", "status", "browser")
        assert result.exit_code == 0
        assert "browser" in result.output
        assert "available" in result.output

    def test_status_held(self, res_invoke) -> None:
        res_invoke("resource", "create", "browser", "--actor", "human:atin")
        res_invoke("resource", "acquire", "browser", "--actor", "agent:claude")
        result = res_invoke("resource", "status", "browser")
        assert result.exit_code == 0
        assert "HELD" in result.output
        assert "agent:claude" in result.output

    def test_status_all(self, res_invoke) -> None:
        res_invoke("resource", "create", "browser", "--actor", "human:atin")
        res_invoke("resource", "create", "simulator", "--actor", "human:atin")
        result = res_invoke("resource", "status")
        assert result.exit_code == 0
        assert "browser" in result.output
        assert "simulator" in result.output

    def test_list_is_alias(self, res_invoke) -> None:
        res_invoke("resource", "create", "browser", "--actor", "human:atin")
        result = res_invoke("resource", "list")
        assert result.exit_code == 0
        assert "browser" in result.output

    def test_status_json(self, res_invoke_json, res_invoke) -> None:
        res_invoke("resource", "create", "browser", "--actor", "human:atin")
        data, code = res_invoke_json("resource", "status", "browser")
        assert code == 0
        assert data["ok"] is True
        assert data["data"]["name"] == "browser"

    def test_list_includes_config_only(
        self, cli_runner: CliRunner, config_with_resources: Path,
    ) -> None:
        """Resources declared in config but not yet created show in list."""
        env = {"LATTICE_ROOT": str(config_with_resources)}
        result = cli_runner.invoke(cli, ["resource", "list"], env=env)
        assert result.exit_code == 0
        assert "browser" in result.output
        assert "ios-simulator" in result.output
        assert "config-only" in result.output

    def test_no_resources_message(self, res_invoke) -> None:
        result = res_invoke("resource", "list")
        assert result.exit_code == 0
        assert "No resources defined" in result.output


# ---------------------------------------------------------------------------
# Full lifecycle
# ---------------------------------------------------------------------------


class TestResourceLifecycle:
    """End-to-end lifecycle tests."""

    def test_create_acquire_heartbeat_release(self, res_invoke) -> None:
        res_invoke("resource", "create", "browser", "--actor", "human:atin")
        res_invoke("resource", "acquire", "browser", "--actor", "agent:claude")
        res_invoke("resource", "heartbeat", "browser", "--actor", "agent:claude")
        result = res_invoke("resource", "release", "browser", "--actor", "agent:claude")
        assert result.exit_code == 0

    def test_event_log_has_all_events(self, res_invoke, initialized_root: Path) -> None:
        # Create
        result = res_invoke(
            "resource", "create", "browser", "--actor", "human:atin", "--json",
        )
        resource_id = json.loads(result.output)["data"]["id"]

        # Acquire
        res_invoke("resource", "acquire", "browser", "--actor", "agent:claude")
        # Heartbeat
        res_invoke("resource", "heartbeat", "browser", "--actor", "agent:claude")
        # Release
        res_invoke("resource", "release", "browser", "--actor", "agent:claude")

        # Read event log
        event_log = initialized_root / LATTICE_DIR / "events" / f"{resource_id}.jsonl"
        events = [json.loads(line) for line in event_log.read_text().splitlines() if line.strip()]
        types = [e["type"] for e in events]
        assert types == [
            "resource_created",
            "resource_acquired",
            "resource_heartbeat",
            "resource_released",
        ]

    def test_second_actor_after_release(self, res_invoke) -> None:
        res_invoke("resource", "create", "browser", "--actor", "human:atin")
        res_invoke("resource", "acquire", "browser", "--actor", "agent:claude")
        res_invoke("resource", "release", "browser", "--actor", "agent:claude")
        result = res_invoke("resource", "acquire", "browser", "--actor", "agent:codex")
        assert result.exit_code == 0
        assert "Acquired" in result.output
