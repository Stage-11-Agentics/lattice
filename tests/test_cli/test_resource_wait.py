"""AC-5 (local part): ``resource acquire --wait`` succeeds once another
process releases the resource during the wait.

The wait is a client-side loop of separate ``resource.acquire`` calls that
holds no lock between attempts, so the other process's release (which takes
the same resource lock) lands while the waiter is still waiting.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

from click.testing import CliRunner

from lattice.cli.main import cli

_LATTICE = [sys.executable, "-c", "from lattice.cli.main import cli; cli()"]


def _events(root: Path, resource_id: str) -> list[dict]:
    path = root / ".lattice" / "events" / f"{resource_id}.jsonl"
    return [json.loads(line) for line in path.read_text().splitlines()]


def test_wait_succeeds_after_another_process_releases(initialized_root: Path) -> None:
    env = {**os.environ, "LATTICE_ROOT": str(initialized_root)}
    runner = CliRunner()

    def invoke(*args: str):  # noqa: ANN202
        return runner.invoke(cli, list(args), env={"LATTICE_ROOT": str(initialized_root)})

    created = invoke("resource", "create", "gpu", "--actor", "human:admin", "--json")
    assert created.exit_code == 0, created.output
    resource_id = json.loads(created.output)["data"]["id"]
    held = invoke("resource", "acquire", "gpu", "--actor", "agent:holder")
    assert held.exit_code == 0, held.output
    # A session actor: every attempt touches the session file (a fresh inode
    # per atomic write), which is how this test sees an attempt was refused.
    started = invoke(
        "session", "start", "--name", "Waiter", "--model", "m", "--framework", "f", "--quiet"
    )
    assert started.output.strip() == "Waiter-1", started.output
    session_file = initialized_root / ".lattice" / "sessions" / "Waiter-1.json"
    inode = session_file.stat().st_ino

    waiter = subprocess.Popen(
        [*_LATTICE, "resource", "acquire", "gpu", "--wait", "--timeout", "20"]
        + ["--name", "Waiter-1", "--json"],
        env=env,
        cwd=initialized_root,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        # Release only after the waiter has made a refused attempt.
        deadline = time.monotonic() + 15
        while session_file.stat().st_ino == inode:
            assert waiter.poll() is None, waiter.communicate()
            assert time.monotonic() < deadline, "the waiter never attempted"
            time.sleep(0.02)
        time.sleep(0.2)
        assert waiter.poll() is None, waiter.communicate()
        released = invoke("resource", "release", "gpu", "--actor", "agent:holder")
        assert released.exit_code == 0, released.output
        stdout, stderr = waiter.communicate(timeout=20)
    finally:
        if waiter.poll() is None:
            waiter.kill()

    assert waiter.returncode == 0, stderr
    data = json.loads(stdout)["data"]
    assert [h["actor"]["name"] for h in data["holders"]] == ["Waiter-1"]

    events = _events(initialized_root, resource_id)
    assert [e["type"] for e in events] == [
        "resource_created",
        "resource_acquired",
        "resource_released",
        "resource_acquired",
    ]
    # Each operation call carries its own op_id; the winning attempt is the
    # waiter's own resource.acquire, not a continuation of the holder's.
    ops = [e["origin"]["op"] for e in events]
    assert ops == ["resource.create", "resource.acquire", "resource.release", "resource.acquire"]
    assert len({e["origin"]["op_id"] for e in events}) == 4


def test_wait_times_out_with_timeout_code(initialized_root: Path) -> None:
    runner = CliRunner()
    env = {"LATTICE_ROOT": str(initialized_root)}
    runner.invoke(cli, ["resource", "create", "gpu", "--actor", "human:admin"], env=env)
    runner.invoke(cli, ["resource", "acquire", "gpu", "--actor", "agent:holder"], env=env)

    result = runner.invoke(
        cli,
        ["resource", "acquire", "gpu", "--wait", "--timeout", "0", "--actor", "agent:b", "--json"],
        env=env,
    )
    assert result.exit_code == 1
    error = json.loads(result.output)["error"]
    assert error == {
        "code": "TIMEOUT",
        "message": "Timed out waiting for resource 'gpu' after 0s.",
    }


def test_wait_does_not_retry_other_errors(initialized_root: Path) -> None:
    """Only RESOURCE_HELD is retried; anything else ends the wait at once."""
    runner = CliRunner()
    env = {"LATTICE_ROOT": str(initialized_root)}
    started = time.monotonic()
    result = runner.invoke(
        cli,
        ["resource", "acquire", "nowhere", "--wait", "--timeout", "30", "--actor", "agent:b"],
        env=env,
    )
    assert result.exit_code == 1
    assert "Resource 'nowhere' not found." in result.output
    assert time.monotonic() - started < 5
