"""Tests for the hook execution system."""

from __future__ import annotations

import json
import stat
from pathlib import Path

import pytest

from lattice.core.events import create_event
from lattice.storage.hooks import (
    _match_transitions,
    _parse_transition_key,
    execute_hooks,
)


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


# ---------------------------------------------------------------------------
# 9. _parse_transition_key unit tests
# ---------------------------------------------------------------------------


class TestParseTransitionKey:
    """Unit tests for the transition key parser."""

    def test_standard_arrow(self) -> None:
        assert _parse_transition_key("in_progress -> review") == ("in_progress", "review")

    def test_no_spaces(self) -> None:
        assert _parse_transition_key("in_progress->review") == ("in_progress", "review")

    def test_extra_spaces(self) -> None:
        assert _parse_transition_key("  in_progress  ->  review  ") == ("in_progress", "review")

    def test_wildcard_source(self) -> None:
        assert _parse_transition_key("* -> review") == ("*", "review")

    def test_wildcard_target(self) -> None:
        assert _parse_transition_key("in_progress -> *") == ("in_progress", "*")

    def test_both_wildcards(self) -> None:
        assert _parse_transition_key("* -> *") == ("*", "*")

    def test_no_arrow_returns_none(self) -> None:
        assert _parse_transition_key("in_progress review") is None

    def test_empty_string_returns_none(self) -> None:
        assert _parse_transition_key("") is None

    def test_arrow_only_returns_none(self) -> None:
        assert _parse_transition_key("->") is None

    def test_missing_right_returns_none(self) -> None:
        assert _parse_transition_key("in_progress ->") is None

    def test_missing_left_returns_none(self) -> None:
        assert _parse_transition_key("-> review") is None

    def test_multiple_arrows_returns_none(self) -> None:
        assert _parse_transition_key("a -> b -> c") is None


# ---------------------------------------------------------------------------
# 10. _match_transitions unit tests
# ---------------------------------------------------------------------------


class TestMatchTransitions:
    """Unit tests for the transition matching logic."""

    def test_exact_match(self) -> None:
        transitions = {"in_progress -> review": "exact.sh"}
        result = _match_transitions(transitions, "in_progress", "review")
        assert result == ["exact.sh"]

    def test_no_match(self) -> None:
        transitions = {"in_progress -> review": "exact.sh"}
        result = _match_transitions(transitions, "backlog", "in_planning")
        assert result == []

    def test_wildcard_source(self) -> None:
        transitions = {"* -> review": "entering-review.sh"}
        result = _match_transitions(transitions, "in_progress", "review")
        assert result == ["entering-review.sh"]

    def test_wildcard_target(self) -> None:
        transitions = {"in_progress -> *": "leaving-progress.sh"}
        result = _match_transitions(transitions, "in_progress", "review")
        assert result == ["leaving-progress.sh"]

    def test_exact_before_wildcards(self) -> None:
        transitions = {
            "* -> review": "wildcard-source.sh",
            "in_progress -> *": "wildcard-target.sh",
            "in_progress -> review": "exact.sh",
        }
        result = _match_transitions(transitions, "in_progress", "review")
        assert result[0] == "exact.sh"
        assert len(result) == 3

    def test_wildcard_source_does_not_match_wrong_target(self) -> None:
        transitions = {"* -> done": "entering-done.sh"}
        result = _match_transitions(transitions, "in_progress", "review")
        assert result == []

    def test_wildcard_target_does_not_match_wrong_source(self) -> None:
        transitions = {"backlog -> *": "leaving-backlog.sh"}
        result = _match_transitions(transitions, "in_progress", "review")
        assert result == []

    def test_multiple_exact_matches(self) -> None:
        transitions = {
            "in_progress -> review": "first.sh",
            "in_progress->review": "second.sh",
        }
        result = _match_transitions(transitions, "in_progress", "review")
        assert len(result) == 2
        assert "first.sh" in result
        assert "second.sh" in result

    def test_malformed_key_is_skipped(self) -> None:
        transitions = {
            "not a valid key": "bad.sh",
            "in_progress -> review": "good.sh",
        }
        result = _match_transitions(transitions, "in_progress", "review")
        assert result == ["good.sh"]

    def test_double_wildcard_matches(self) -> None:
        transitions = {"* -> *": "all.sh"}
        result = _match_transitions(transitions, "in_progress", "review")
        assert result == ["all.sh"]

    def test_double_wildcard_matches_any_transition(self) -> None:
        transitions = {"* -> *": "all.sh"}
        result = _match_transitions(transitions, "backlog", "cancelled")
        assert result == ["all.sh"]

    def test_deterministic_wildcard_order(self) -> None:
        """Wildcard source runs before wildcard target regardless of config key order."""
        transitions = {
            "in_progress -> *": "target.sh",
            "* -> review": "source.sh",
        }
        result = _match_transitions(transitions, "in_progress", "review")
        assert result == ["source.sh", "target.sh"]

    def test_full_priority_order(self) -> None:
        """exact -> wild_src -> wild_tgt -> wild_both."""
        transitions = {
            "* -> *": "both.sh",
            "in_progress -> *": "tgt.sh",
            "* -> review": "src.sh",
            "in_progress -> review": "exact.sh",
        }
        result = _match_transitions(transitions, "in_progress", "review")
        assert result == ["exact.sh", "src.sh", "tgt.sh", "both.sh"]

    def test_empty_transitions(self) -> None:
        assert _match_transitions({}, "in_progress", "review") == []

    def test_array_value_single(self) -> None:
        """Array with one command works like a string."""
        transitions = {"* -> review": ["entering-review.sh"]}
        result = _match_transitions(transitions, "in_progress", "review")
        assert result == ["entering-review.sh"]

    def test_array_value_multiple(self) -> None:
        """Array with multiple commands flattens them all."""
        transitions = {"* -> review": ["first.sh", "second.sh", "third.sh"]}
        result = _match_transitions(transitions, "in_progress", "review")
        assert result == ["first.sh", "second.sh", "third.sh"]

    def test_array_and_string_mixed(self) -> None:
        """Mix of string and array values both work."""
        transitions = {
            "* -> review": ["arr1.sh", "arr2.sh"],
            "in_progress -> *": "single.sh",
        }
        result = _match_transitions(transitions, "in_progress", "review")
        assert result == ["arr1.sh", "arr2.sh", "single.sh"]

    def test_array_value_with_exact_match(self) -> None:
        """Array on exact match runs all commands."""
        transitions = {
            "in_progress -> review": ["exact1.sh", "exact2.sh"],
        }
        result = _match_transitions(transitions, "in_progress", "review")
        assert result == ["exact1.sh", "exact2.sh"]

    def test_array_empty_treated_as_no_commands(self) -> None:
        """Empty array means no commands for that pattern."""
        transitions = {"* -> review": []}
        result = _match_transitions(transitions, "in_progress", "review")
        assert result == []


# ---------------------------------------------------------------------------
# 11. Transition hooks fire on status_changed events
# ---------------------------------------------------------------------------


def test_transition_exact_match_fires(
    tmp_path: Path, lattice_dir: Path, sample_event: dict
) -> None:
    """Exact transition hook fires for matching from -> to."""
    output_file = tmp_path / "transition_output.txt"

    hook_script = tmp_path / "transition_hook.sh"
    hook_script.write_text(
        f"""#!/bin/sh
echo "fired" > "{output_file}"
"""
    )
    hook_script.chmod(hook_script.stat().st_mode | stat.S_IEXEC)

    config = {
        "hooks": {
            "transitions": {
                "backlog -> in_planning": str(hook_script),
            }
        }
    }
    execute_hooks(config, lattice_dir, sample_event["task_id"], sample_event)

    assert output_file.exists(), "Transition hook did not fire"
    assert output_file.read_text().strip() == "fired"


def test_transition_does_not_fire_for_wrong_transition(
    tmp_path: Path, lattice_dir: Path, sample_event: dict
) -> None:
    """Transition hook does NOT fire when the transition doesn't match."""
    output_file = tmp_path / "wrong_transition_output.txt"

    hook_script = tmp_path / "wrong_transition_hook.sh"
    hook_script.write_text(
        f"""#!/bin/sh
echo "should-not-fire" > "{output_file}"
"""
    )
    hook_script.chmod(hook_script.stat().st_mode | stat.S_IEXEC)

    config = {
        "hooks": {
            "transitions": {
                "in_progress -> review": str(hook_script),
            }
        }
    }
    # sample_event is backlog -> in_planning, not in_progress -> review
    execute_hooks(config, lattice_dir, sample_event["task_id"], sample_event)

    assert not output_file.exists(), "Transition hook fired for wrong transition"


def test_transition_wildcard_source_fires(
    tmp_path: Path, lattice_dir: Path, sample_event: dict
) -> None:
    """Wildcard source (* -> to) fires for any source with matching target."""
    output_file = tmp_path / "wildcard_source_output.txt"

    hook_script = tmp_path / "wildcard_source_hook.sh"
    hook_script.write_text(
        f"""#!/bin/sh
echo "fired" > "{output_file}"
"""
    )
    hook_script.chmod(hook_script.stat().st_mode | stat.S_IEXEC)

    config = {
        "hooks": {
            "transitions": {
                "* -> in_planning": str(hook_script),
            }
        }
    }
    execute_hooks(config, lattice_dir, sample_event["task_id"], sample_event)

    assert output_file.exists(), "Wildcard source transition hook did not fire"


def test_transition_wildcard_target_fires(
    tmp_path: Path, lattice_dir: Path, sample_event: dict
) -> None:
    """Wildcard target (from -> *) fires for any target with matching source."""
    output_file = tmp_path / "wildcard_target_output.txt"

    hook_script = tmp_path / "wildcard_target_hook.sh"
    hook_script.write_text(
        f"""#!/bin/sh
echo "fired" > "{output_file}"
"""
    )
    hook_script.chmod(hook_script.stat().st_mode | stat.S_IEXEC)

    config = {
        "hooks": {
            "transitions": {
                "backlog -> *": str(hook_script),
            }
        }
    }
    execute_hooks(config, lattice_dir, sample_event["task_id"], sample_event)

    assert output_file.exists(), "Wildcard target transition hook did not fire"


def test_transition_does_not_fire_for_non_status_event(tmp_path: Path, lattice_dir: Path) -> None:
    """Transition hooks do NOT fire for non-status_changed events."""
    output_file = tmp_path / "non_status_output.txt"

    hook_script = tmp_path / "non_status_hook.sh"
    hook_script.write_text(
        f"""#!/bin/sh
echo "should-not-fire" > "{output_file}"
"""
    )
    hook_script.chmod(hook_script.stat().st_mode | stat.S_IEXEC)

    task_created_event = create_event(
        type="task_created",
        task_id="task_01AAAAAAAAAAAAAAAAAAAAAAAAAA",
        actor="human:test",
        data={"title": "Test task"},
    )

    config = {
        "hooks": {
            "transitions": {
                "* -> *": str(hook_script),
            }
        }
    }
    execute_hooks(config, lattice_dir, task_created_event["task_id"], task_created_event)

    assert not output_file.exists(), "Transition hook fired for non-status_changed event"


def test_transition_env_vars(tmp_path: Path, lattice_dir: Path, sample_event: dict) -> None:
    """Transition hooks receive LATTICE_FROM_STATUS and LATTICE_TO_STATUS env vars."""
    env_output = tmp_path / "transition_env_output.txt"

    hook_script = tmp_path / "transition_env_hook.sh"
    hook_script.write_text(
        f"""#!/bin/sh
echo "FROM=$LATTICE_FROM_STATUS" > "{env_output}"
echo "TO=$LATTICE_TO_STATUS" >> "{env_output}"
"""
    )
    hook_script.chmod(hook_script.stat().st_mode | stat.S_IEXEC)

    config = {
        "hooks": {
            "transitions": {
                "backlog -> in_planning": str(hook_script),
            }
        }
    }
    execute_hooks(config, lattice_dir, sample_event["task_id"], sample_event)

    assert env_output.exists()
    lines = env_output.read_text().strip().splitlines()
    env_dict = {}
    for line in lines:
        key, val = line.split("=", 1)
        env_dict[key] = val

    assert env_dict["FROM"] == "backlog"
    assert env_dict["TO"] == "in_planning"


def test_all_three_hook_types_fire_together(
    tmp_path: Path, lattice_dir: Path, sample_event: dict
) -> None:
    """post_event, on.status_changed, and transitions all fire for one event."""
    post_output = tmp_path / "post.txt"
    type_output = tmp_path / "type.txt"
    transition_output = tmp_path / "transition.txt"

    for name, output in [
        ("post", post_output),
        ("type", type_output),
        ("transition", transition_output),
    ]:
        script = tmp_path / f"{name}_hook.sh"
        script.write_text(f"""#!/bin/sh\necho "{name}" > "{output}"\n""")
        script.chmod(script.stat().st_mode | stat.S_IEXEC)

    config = {
        "hooks": {
            "post_event": str(tmp_path / "post_hook.sh"),
            "on": {"status_changed": str(tmp_path / "type_hook.sh")},
            "transitions": {
                "backlog -> in_planning": str(tmp_path / "transition_hook.sh"),
            },
        }
    }
    execute_hooks(config, lattice_dir, sample_event["task_id"], sample_event)

    assert post_output.exists(), "post_event did not fire"
    assert type_output.exists(), "on.status_changed did not fire"
    assert transition_output.exists(), "transition hook did not fire"


# ---------------------------------------------------------------------------
# 12. CLI integration: transition hooks fire via status command
# ---------------------------------------------------------------------------


def test_cli_status_fires_transition_hook(tmp_path: Path, cli_runner, cli_env: dict) -> None:
    """lattice status fires transition-specific hooks."""
    from lattice.cli.main import cli

    output_file = tmp_path / "cli_transition_output.json"

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
        ["create", "Transition hook CLI task", "--actor", "human:test", "--json"],
        env=cli_env,
    )
    assert result.exit_code == 0
    task_id = json.loads(result.output)["data"]["id"]

    # Configure transition hook
    lattice_dir = Path(cli_env["LATTICE_ROOT"]) / ".lattice"
    config = json.loads((lattice_dir / "config.json").read_text())
    config["hooks"] = {
        "transitions": {
            "backlog -> in_planning": str(hook_script),
        }
    }
    (lattice_dir / "config.json").write_text(json.dumps(config, sort_keys=True, indent=2) + "\n")

    result = cli_runner.invoke(
        cli,
        ["status", task_id, "in_planning", "--actor", "human:test"],
        env=cli_env,
    )
    assert result.exit_code == 0, f"status failed: {result.output}"

    assert output_file.exists(), "Transition hook did not fire via CLI"
    event = json.loads(output_file.read_text())
    assert event["type"] == "status_changed"
    assert event["data"]["from"] == "backlog"
    assert event["data"]["to"] == "in_planning"


def test_cli_status_transition_hook_does_not_fire_for_wrong_transition(
    tmp_path: Path, cli_runner, cli_env: dict
) -> None:
    """Transition hook does NOT fire for a non-matching status change via CLI."""
    from lattice.cli.main import cli

    output_file = tmp_path / "wrong_cli_transition_output.txt"

    hook_script = tmp_path / "hook.sh"
    hook_script.write_text(
        f"""#!/bin/sh
echo "should-not-fire" > "{output_file}"
"""
    )
    hook_script.chmod(hook_script.stat().st_mode | stat.S_IEXEC)

    # Create a task
    result = cli_runner.invoke(
        cli,
        ["create", "Wrong transition CLI task", "--actor", "human:test", "--json"],
        env=cli_env,
    )
    assert result.exit_code == 0
    task_id = json.loads(result.output)["data"]["id"]

    # Configure hook for in_progress -> review (not backlog -> in_planning)
    lattice_dir = Path(cli_env["LATTICE_ROOT"]) / ".lattice"
    config = json.loads((lattice_dir / "config.json").read_text())
    config["hooks"] = {
        "transitions": {
            "in_progress -> review": str(hook_script),
        }
    }
    (lattice_dir / "config.json").write_text(json.dumps(config, sort_keys=True, indent=2) + "\n")

    # Transition backlog -> in_planning (doesn't match hook)
    result = cli_runner.invoke(
        cli,
        ["status", task_id, "in_planning", "--actor", "human:test"],
        env=cli_env,
    )
    assert result.exit_code == 0

    assert not output_file.exists(), "Transition hook fired for non-matching transition"


# ---------------------------------------------------------------------------
# 13. Robustness: malformed transitions config
# ---------------------------------------------------------------------------


def test_malformed_transitions_string_does_not_raise(
    lattice_dir: Path, sample_event: dict
) -> None:
    """Malformed transitions config (string instead of dict) does not crash."""
    config = {"hooks": {"transitions": "not-a-dict"}}
    # Should not raise
    execute_hooks(config, lattice_dir, sample_event["task_id"], sample_event)


def test_malformed_transitions_list_does_not_raise(lattice_dir: Path, sample_event: dict) -> None:
    """Malformed transitions config (list instead of dict) does not crash."""
    config = {"hooks": {"transitions": ["bad", "config"]}}
    execute_hooks(config, lattice_dir, sample_event["task_id"], sample_event)


def test_transition_with_missing_event_data(lattice_dir: Path) -> None:
    """Transition hooks handle events with empty data gracefully."""
    event = create_event(
        type="status_changed",
        task_id="task_01AAAAAAAAAAAAAAAAAAAAAAAAAA",
        actor="human:test",
        data={},
    )
    config = {"hooks": {"transitions": {"* -> *": "echo noop"}}}
    # from_status and to_status will be empty strings, guard should skip
    execute_hooks(config, lattice_dir, event["task_id"], event)
