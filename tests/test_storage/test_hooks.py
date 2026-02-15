"""Tests for the hook execution system."""

from __future__ import annotations

import json
import stat
from pathlib import Path

import pytest

from lattice.core.events import create_event
from lattice.storage.hooks import execute_hooks


@pytest.fixture()
def sample_event() -> dict:
    """Return a sample event dict for testing hooks."""
    return create_event(
        type="status_changed",
        task_id="task_01AAAAAAAAAAAAAAAAAAAAAAAAAA",
        actor="human:test",
        data={"from": "backlog", "to": "in_planning"},
    )


@pytest.fixture()
def lattice_dir(tmp_path: Path) -> Path:
    """Return a temporary .lattice/ directory."""
    ld = tmp_path / ".lattice"
    ld.mkdir()
    return ld


# ---------------------------------------------------------------------------
# 1. No hooks configured → no-op, no errors
# ---------------------------------------------------------------------------


def test_no_hooks_configured(lattice_dir: Path, sample_event: dict) -> None:
    """execute_hooks does nothing when config has no hooks key."""
    config: dict = {"schema_version": 1}
    # Should not raise
    execute_hooks(config, lattice_dir, sample_event["task_id"], sample_event)


def test_empty_hooks_configured(lattice_dir: Path, sample_event: dict) -> None:
    """execute_hooks does nothing when hooks dict is empty."""
    config: dict = {"schema_version": 1, "hooks": {}}
    execute_hooks(config, lattice_dir, sample_event["task_id"], sample_event)


# ---------------------------------------------------------------------------
# 2. post_event fires on any event type
# ---------------------------------------------------------------------------


def test_post_event_fires(tmp_path: Path, lattice_dir: Path, sample_event: dict) -> None:
    """post_event hook fires and receives correct env and stdin."""
    output_file = tmp_path / "hook_output.json"

    # Write a hook script that dumps env vars and stdin to a file
    hook_script = tmp_path / "hook.sh"
    hook_script.write_text(
        f"""#!/bin/sh
cat > "{output_file}"
"""
    )
    hook_script.chmod(hook_script.stat().st_mode | stat.S_IEXEC)

    config = {"hooks": {"post_event": str(hook_script)}}
    execute_hooks(config, lattice_dir, sample_event["task_id"], sample_event)

    assert output_file.exists(), "Hook script did not run"
    content = output_file.read_text()
    parsed = json.loads(content)
    assert parsed["type"] == "status_changed"
    assert parsed["task_id"] == sample_event["task_id"]


# ---------------------------------------------------------------------------
# 3. on.<type> fires only for matching type
# ---------------------------------------------------------------------------


def test_on_type_fires_for_matching(tmp_path: Path, lattice_dir: Path, sample_event: dict) -> None:
    """on.status_changed fires when event type is status_changed."""
    output_file = tmp_path / "type_hook_output.txt"

    hook_script = tmp_path / "type_hook.sh"
    hook_script.write_text(
        f"""#!/bin/sh
echo "fired" > "{output_file}"
"""
    )
    hook_script.chmod(hook_script.stat().st_mode | stat.S_IEXEC)

    config = {"hooks": {"on": {"status_changed": str(hook_script)}}}
    execute_hooks(config, lattice_dir, sample_event["task_id"], sample_event)

    assert output_file.exists(), "Type-specific hook did not fire"
    assert output_file.read_text().strip() == "fired"


def test_on_type_does_not_fire_for_non_matching(
    tmp_path: Path, lattice_dir: Path, sample_event: dict
) -> None:
    """on.task_created does NOT fire when event type is status_changed."""
    output_file = tmp_path / "wrong_type_output.txt"

    hook_script = tmp_path / "wrong_hook.sh"
    hook_script.write_text(
        f"""#!/bin/sh
echo "should-not-fire" > "{output_file}"
"""
    )
    hook_script.chmod(hook_script.stat().st_mode | stat.S_IEXEC)

    config = {"hooks": {"on": {"task_created": str(hook_script)}}}
    execute_hooks(config, lattice_dir, sample_event["task_id"], sample_event)

    assert not output_file.exists(), "Hook fired for wrong event type"


# ---------------------------------------------------------------------------
# 4. Both post_event and on.<type> fire when both configured
# ---------------------------------------------------------------------------


def test_both_hooks_fire(tmp_path: Path, lattice_dir: Path, sample_event: dict) -> None:
    """Both post_event and on.<type> fire when both are configured."""
    post_output = tmp_path / "post_output.txt"
    type_output = tmp_path / "type_output.txt"

    post_script = tmp_path / "post_hook.sh"
    post_script.write_text(
        f"""#!/bin/sh
echo "post" > "{post_output}"
"""
    )
    post_script.chmod(post_script.stat().st_mode | stat.S_IEXEC)

    type_script = tmp_path / "type_hook.sh"
    type_script.write_text(
        f"""#!/bin/sh
echo "type" > "{type_output}"
"""
    )
    type_script.chmod(type_script.stat().st_mode | stat.S_IEXEC)

    config = {
        "hooks": {
            "post_event": str(post_script),
            "on": {"status_changed": str(type_script)},
        }
    }
    execute_hooks(config, lattice_dir, sample_event["task_id"], sample_event)

    assert post_output.exists(), "post_event hook did not fire"
    assert type_output.exists(), "on.<type> hook did not fire"
    assert post_output.read_text().strip() == "post"
    assert type_output.read_text().strip() == "type"


# ---------------------------------------------------------------------------
# 5. Hook receives correct env vars and stdin
# ---------------------------------------------------------------------------


def test_hook_receives_env_vars(tmp_path: Path, lattice_dir: Path, sample_event: dict) -> None:
    """Hook subprocess receives LATTICE_* environment variables."""
    env_output = tmp_path / "env_output.txt"

    hook_script = tmp_path / "env_hook.sh"
    hook_script.write_text(
        f"""#!/bin/sh
echo "ROOT=$LATTICE_ROOT" > "{env_output}"
echo "TASK_ID=$LATTICE_TASK_ID" >> "{env_output}"
echo "EVENT_TYPE=$LATTICE_EVENT_TYPE" >> "{env_output}"
echo "EVENT_ID=$LATTICE_EVENT_ID" >> "{env_output}"
echo "ACTOR=$LATTICE_ACTOR" >> "{env_output}"
"""
    )
    hook_script.chmod(hook_script.stat().st_mode | stat.S_IEXEC)

    config = {"hooks": {"post_event": str(hook_script)}}
    execute_hooks(config, lattice_dir, sample_event["task_id"], sample_event)

    assert env_output.exists()
    lines = env_output.read_text().strip().splitlines()
    env_dict = {}
    for line in lines:
        key, val = line.split("=", 1)
        env_dict[key] = val

    assert env_dict["ROOT"] == str(lattice_dir)
    assert env_dict["TASK_ID"] == sample_event["task_id"]
    assert env_dict["EVENT_TYPE"] == "status_changed"
    assert env_dict["EVENT_ID"] == sample_event["id"]
    assert env_dict["ACTOR"] == "human:test"


# ---------------------------------------------------------------------------
# 6. Hook failure does not fail the command
# ---------------------------------------------------------------------------


def test_hook_failure_does_not_raise(
    tmp_path: Path, lattice_dir: Path, sample_event: dict
) -> None:
    """A hook that exits non-zero does not raise an exception."""
    hook_script = tmp_path / "failing_hook.sh"
    hook_script.write_text("#!/bin/sh\nexit 1\n")
    hook_script.chmod(hook_script.stat().st_mode | stat.S_IEXEC)

    config = {"hooks": {"post_event": str(hook_script)}}
    # Should not raise
    execute_hooks(config, lattice_dir, sample_event["task_id"], sample_event)


def test_hook_nonexistent_command_does_not_raise(lattice_dir: Path, sample_event: dict) -> None:
    """A hook pointing to a nonexistent command does not raise."""
    config = {"hooks": {"post_event": "/nonexistent/path/to/hook"}}
    # Should not raise
    execute_hooks(config, lattice_dir, sample_event["task_id"], sample_event)


# ---------------------------------------------------------------------------
# 7. Hook timeout does not hang the command
# ---------------------------------------------------------------------------


def test_hook_timeout_does_not_hang(tmp_path: Path, lattice_dir: Path, sample_event: dict) -> None:
    """A hook that sleeps longer than the timeout gets killed."""
    hook_script = tmp_path / "slow_hook.sh"
    hook_script.write_text("#!/bin/sh\nsleep 60\n")
    hook_script.chmod(hook_script.stat().st_mode | stat.S_IEXEC)

    config = {"hooks": {"post_event": str(hook_script)}}

    # Temporarily reduce timeout for test speed
    import lattice.storage.hooks as hooks_mod

    original_timeout = hooks_mod.HOOK_TIMEOUT_SECONDS
    hooks_mod.HOOK_TIMEOUT_SECONDS = 1
    try:
        # Should not hang — timeout after 1 second
        execute_hooks(config, lattice_dir, sample_event["task_id"], sample_event)
    finally:
        hooks_mod.HOOK_TIMEOUT_SECONDS = original_timeout


# ---------------------------------------------------------------------------
# 8. Integration: CLI commands fire hooks
# ---------------------------------------------------------------------------


def test_cli_create_fires_hooks(tmp_path: Path, cli_runner, cli_env: dict) -> None:
    """lattice create fires post_event hook."""
    from lattice.cli.main import cli

    output_file = tmp_path / "create_hook_output.json"

    hook_script = tmp_path / "hook.sh"
    hook_script.write_text(
        f"""#!/bin/sh
cat > "{output_file}"
"""
    )
    hook_script.chmod(hook_script.stat().st_mode | stat.S_IEXEC)

    # Update config to include hooks
    lattice_dir = Path(cli_env["LATTICE_ROOT"]) / ".lattice"
    config = json.loads((lattice_dir / "config.json").read_text())
    config["hooks"] = {"post_event": str(hook_script)}
    (lattice_dir / "config.json").write_text(json.dumps(config, sort_keys=True, indent=2) + "\n")

    result = cli_runner.invoke(
        cli,
        ["create", "Hook test task", "--actor", "human:test"],
        env=cli_env,
    )
    assert result.exit_code == 0, f"create failed: {result.output}"

    assert output_file.exists(), "Hook did not fire during create"
    event = json.loads(output_file.read_text())
    assert event["type"] == "task_created"


def test_cli_status_fires_hooks(tmp_path: Path, cli_runner, cli_env: dict) -> None:
    """lattice status fires on.status_changed hook."""
    from lattice.cli.main import cli

    output_file = tmp_path / "status_hook_output.json"

    hook_script = tmp_path / "hook.sh"
    hook_script.write_text(
        f"""#!/bin/sh
cat > "{output_file}"
"""
    )
    hook_script.chmod(hook_script.stat().st_mode | stat.S_IEXEC)

    # Create a task first
    result = cli_runner.invoke(
        cli,
        ["create", "Hook status task", "--actor", "human:test", "--json"],
        env=cli_env,
    )
    assert result.exit_code == 0
    task_id = json.loads(result.output)["data"]["id"]

    # Update config to include hooks
    lattice_dir = Path(cli_env["LATTICE_ROOT"]) / ".lattice"
    config = json.loads((lattice_dir / "config.json").read_text())
    config["hooks"] = {"on": {"status_changed": str(hook_script)}}
    (lattice_dir / "config.json").write_text(json.dumps(config, sort_keys=True, indent=2) + "\n")

    result = cli_runner.invoke(
        cli,
        ["status", task_id, "in_planning", "--actor", "human:test"],
        env=cli_env,
    )
    assert result.exit_code == 0, f"status failed: {result.output}"

    assert output_file.exists(), "Hook did not fire during status change"
    event = json.loads(output_file.read_text())
    assert event["type"] == "status_changed"
    assert event["data"]["to"] == "in_planning"


def test_cli_archive_fires_hooks(tmp_path: Path, cli_runner, cli_env: dict) -> None:
    """lattice archive fires hooks."""
    from lattice.cli.main import cli

    output_file = tmp_path / "archive_hook_output.json"

    hook_script = tmp_path / "hook.sh"
    hook_script.write_text(
        f"""#!/bin/sh
cat > "{output_file}"
"""
    )
    hook_script.chmod(hook_script.stat().st_mode | stat.S_IEXEC)

    # Create a task first
    result = cli_runner.invoke(
        cli,
        ["create", "Archive hook task", "--actor", "human:test", "--json"],
        env=cli_env,
    )
    assert result.exit_code == 0
    task_id = json.loads(result.output)["data"]["id"]

    # Update config with hooks
    lattice_dir = Path(cli_env["LATTICE_ROOT"]) / ".lattice"
    config = json.loads((lattice_dir / "config.json").read_text())
    config["hooks"] = {"post_event": str(hook_script)}
    (lattice_dir / "config.json").write_text(json.dumps(config, sort_keys=True, indent=2) + "\n")

    result = cli_runner.invoke(
        cli,
        ["archive", task_id, "--actor", "human:test"],
        env=cli_env,
    )
    assert result.exit_code == 0, f"archive failed: {result.output}"

    assert output_file.exists(), "Hook did not fire during archive"
    event = json.loads(output_file.read_text())
    assert event["type"] == "task_archived"
