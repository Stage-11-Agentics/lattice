"""``lattice sync [--follow]``: one catch-up, or the foreground follower."""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest
from click.testing import CliRunner

from lattice.cli.main import cli
from lattice.remote import cache
from lattice.remote import endpoint as remote_endpoint
from lattice.remote.cache import SyncOutcome
from lattice.remote.follower import live_follower, read_follower
from tests.test_remote.stream_stub import wait_for

REPO = Path(__file__).resolve().parents[2]


def _invoke(*args: str):
    return CliRunner().invoke(cli, ["sync", *args])


def test_sync_on_a_local_checkout_is_not_hosted(tmp_path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    result = _invoke()
    assert result.exit_code == 1
    assert result.stderr.startswith("Error: This checkout is not bound to a Lattice server")
    result = _invoke("--json")
    assert result.exit_code == 1
    assert json.loads(result.stdout)["error"]["code"] == "NOT_HOSTED"


def test_follow_and_json_are_refused_before_anything_else(tmp_path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    result = _invoke("--follow", "--json")
    assert result.exit_code == 1
    assert json.loads(result.stdout)["error"]["code"] == "VALIDATION_ERROR"


@pytest.fixture
def hosted(tmp_path, monkeypatch):
    monkeypatch.setattr(remote_endpoint, "hosted_root", lambda start: tmp_path)
    outcomes: list[SyncOutcome] = []
    monkeypatch.setattr(cache, "catch_up", lambda root, *, bulk=False: outcomes.pop(0))
    return outcomes


def test_sync_once_plain_and_json(hosted) -> None:
    hosted.append(SyncOutcome("applied", 7, "2026-09-27T06:00:00Z"))
    result = _invoke()
    assert (result.exit_code, result.stdout) == (0, "Synced to seq 7.\n")
    hosted.append(SyncOutcome("unchanged", 7, "2026-09-27T06:00:01Z"))
    result = _invoke()
    assert (result.exit_code, result.stdout) == (0, "Already up to date at seq 7.\n")
    hosted.append(SyncOutcome("unchanged", 7, "2026-09-27T06:00:01Z"))
    result = _invoke("--json")
    assert result.exit_code == 0
    assert json.loads(result.stdout) == {
        "ok": True,
        "data": {"head_seq": 7, "status": "unchanged", "synced_at": "2026-09-27T06:00:01Z"},
    }


@pytest.mark.parametrize(
    ("status", "code"),
    [
        ("unreachable", "SERVER_UNREACHABLE"),
        ("busy", "BOARD_BUSY"),
        ("incomplete", "CACHE_INCOMPLETE"),
    ],
)
def test_sync_once_failures(hosted, status, code) -> None:
    hosted.append(SyncOutcome(status, 3, "2026-09-27T06:00:00Z"))
    result = _invoke("--json")
    assert result.exit_code == 1
    assert json.loads(result.stdout)["error"]["code"] == code
    hosted.append(SyncOutcome(status, 3, "2026-09-27T06:00:00Z"))
    result = _invoke()
    assert result.exit_code == 1 and result.stderr.startswith("Error: ")


_FOLLOW_SCRIPT = textwrap.dedent(
    """
    import sys
    from pathlib import Path
    from lattice.cli.main import cli
    from lattice.remote import cache, endpoint as remote_endpoint
    from tests.test_remote.stream_stub import StubSyncer, endpoint

    root, url = Path(sys.argv[1]), sys.argv[2]
    remote_endpoint.hosted_root = lambda start: root
    remote_endpoint.endpoint_for = lambda r: endpoint(url)
    cache.catch_up = StubSyncer(url)
    cli(["sync", "--follow"])
    """
)


def test_follow_exits_0_on_sigterm_and_clears_stream_live_until(tmp_path, stream_stub) -> None:
    root = tmp_path / "b"
    (root / ".lattice").mkdir(parents=True)
    env = dict(os.environ, PYTHONPATH=str(REPO))
    proc = subprocess.Popen(
        [sys.executable, "-c", _FOLLOW_SCRIPT, str(root), stream_stub.url],
        cwd=root,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    try:
        assert wait_for(lambda: live_follower(root), 4), proc.stderr.read1().decode()
        assert read_follower(root)["pid"] == proc.pid
        proc.send_signal(signal.SIGTERM)
        out, err = proc.communicate(timeout=4)
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait()
    assert proc.returncode == 0, err.decode()
    assert read_follower(root) == {"pid": proc.pid, "stream_live_until": None}
    lines = err.decode().splitlines()
    assert lines[0] == "Following stub/demo... (Ctrl-C to stop)"
    assert "lattice: following stub" in lines
    assert lines[-1] == "Stopped."


def test_sync_once_uses_the_bulk_policy(tmp_path, monkeypatch) -> None:
    """SPEC §9.5: ``lattice sync`` always uses the bulk transfer policy."""
    calls: list[dict] = []
    monkeypatch.setattr(remote_endpoint, "hosted_root", lambda start: tmp_path)

    def fake(root, **kwargs):
        calls.append(kwargs)
        return SyncOutcome("unchanged", 1, "t")

    monkeypatch.setattr(cache, "catch_up", fake)
    assert _invoke().exit_code == 0
    assert calls == [{"bulk": True}]
