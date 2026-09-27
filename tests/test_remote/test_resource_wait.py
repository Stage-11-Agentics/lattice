"""AC-5 (hosted part, H-12): ``resource acquire --wait`` succeeds once another
client releases the resource during the wait.

Two bound checkouts of one project. The waiter is a real ``lattice`` process
in checkout B; its loop is separate ``resource.acquire`` operations with no
lock held between them (SPEC §3.3), so checkout A's release lands while B
waits. The server's request log shows B refused at least once first.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

from lattice.server.testing import wait_for
from tests.test_remote.hosted import HostedEnv, make_repo, run_cli

_LATTICE = [sys.executable, "-c", "from lattice.cli.main import cli; cli()"]


def _refusals(env: HostedEnv) -> int:
    assert env.handle is not None
    return sum(
        1
        for line in env.handle.log_lines
        if line.get("op") == "resource.acquire"
        and line.get("actor") == "agent:waiter"
        and line.get("error_code") == "RESOURCE_HELD"
    )


def test_hosted_wait_succeeds_after_another_client_releases(
    hosted_env: HostedEnv, tmp_path: Path
) -> None:
    a = make_repo(tmp_path / "machine-a" / "repo")
    b = make_repo(tmp_path / "machine-b" / "repo")
    for repo in (a, b):
        assert run_cli(repo, "remote", "attach", "team", "demo").exit_code == 0
    created = run_cli(a, "resource", "create", "gpu", "--actor", "human:alice", "--json")
    assert created.exit_code == 0, created.output
    held = run_cli(a, "resource", "acquire", "gpu", "--actor", "agent:holder")
    assert held.exit_code == 0, held.output

    waiter = subprocess.Popen(
        [*_LATTICE, "resource", "acquire", "gpu", "--wait", "--timeout", "30"]
        + ["--actor", "agent:waiter", "--json"],
        env=dict(os.environ),
        cwd=b,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        assert wait_for(lambda: _refusals(hosted_env) >= 1 or waiter.poll() is not None, 20)
        assert waiter.poll() is None, waiter.communicate()
        released = run_cli(a, "resource", "release", "gpu", "--actor", "agent:holder")
        assert released.exit_code == 0, released.output
        out, err = waiter.communicate(timeout=30)
    finally:
        if waiter.poll() is None:
            waiter.kill()
            waiter.communicate()
    assert waiter.returncode == 0, err
    data = json.loads(out)["data"]
    assert [h["actor"] for h in data["holders"]] == ["agent:waiter"]
    # Both checkouts' caches now show B holding it.
    for repo in (a, b):
        shown = run_cli(repo, "resource", "status", "gpu", "--json")
        assert shown.exit_code == 0, shown.output
        holders = json.loads(shown.stdout)["data"]["holders"]
        assert [h["actor"] for h in holders] == ["agent:waiter"]
