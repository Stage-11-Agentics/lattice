"""``lattice dashboard`` on a bound checkout keeps its reads fresh (H-13a; SPEC §9.5, §9.6).

The embedded follower brings another writer's change in without the dashboard
asking, and clears its freshness record when the dashboard stops. Whenever the
follower is not live (its thread ended, or its freshness lapsed after a failed
sync) every read runs the CLI's freshness step first: a catch-up, or offline,
the cache with its notice (once, however often the page polls); a follower
that died is restarted.
"""

from __future__ import annotations

import json
import os
import threading
import time
from pathlib import Path

import pytest

from lattice.remote.follower import read_follower
from lattice.server.testing import wait_for
from tests.test_dashboard.bound_helpers import bound_repo, dashboard, request
from tests.test_remote.hosted import HostedEnv


def test_the_embedded_follower_brings_other_writers_changes_in(
    hosted_env: HostedEnv, tmp_path: Path
) -> None:
    repo, task = bound_repo(hosted_env, tmp_path)

    with dashboard(repo) as port:
        assert wait_for(lambda: (read_follower(repo) or {}).get("stream_live_until"), timeout=10)
        # Another writer, straight to the server: the dashboard never asks.
        hosted_env.server_op("task.comment", {"task": task, "text": "from elsewhere"})

        def seen() -> bool:
            _, events = request(port, "GET", f"/api/tasks/{task}/events")
            return any(e["type"] == "comment_added" for e in events["data"])

        assert wait_for(seen, timeout=10)

    record = read_follower(repo) or {}
    assert not record.get("stream_live_until")


def test_bound_dashboard_reads_issue_metadata_without_local_media_urls(
    hosted_env: HostedEnv, tmp_path: Path
) -> None:
    from lattice.server import admin

    admin.set_project_config(hosted_env.server_root, "demo", {"issues.enabled": True})
    repo, _task = bound_repo(hosted_env, tmp_path)
    filed = hosted_env.server_op("issue.file", {"title": "Read from the bound mirror"})
    issue = filed["result"]["value"]

    with dashboard(repo, follower_factory=_ExitingFollower) as port:
        status, listed = request(port, "GET", "/api/issues")
        assert status == 200, listed
        assert [row["id"] for row in listed["data"]] == [issue["id"]]
        status, detail = request(port, "GET", f"/api/issues/{issue['id']}")
        assert status == 200, detail
        assert detail["data"]["title"] == "Read from the bound mirror"
        assert detail["data"]["media"] == []


# ---------------------------------------------------------------------------
# Reads catch up whenever the follower is not live (SPEC §9.5, §9.6)
# ---------------------------------------------------------------------------


class _ExitingFollower:
    """A follower whose thread ends at once (as one stopped by a hard error)."""

    made: list[_ExitingFollower] = []

    def __init__(self, *args: object, **kwargs: object) -> None:
        _ExitingFollower.made.append(self)

    def run(self) -> None:
        return None

    def stop(self) -> None:
        return None


class _StaleFollower:
    """A follower that stays alive but whose freshness has lapsed (as after a
    failed sync): its record names this live process and a past deadline."""

    def __init__(self, root: Path, *args: object, **kwargs: object) -> None:
        self._root = Path(root)
        self._stop = threading.Event()

    def run(self) -> None:
        record = {"pid": os.getpid(), "stream_live_until": "2000-01-01T00:00:00.000000Z"}
        (self._root / ".lattice" / "cache" / "follower.json").write_text(json.dumps(record))
        self._stop.wait()

    def stop(self) -> None:
        self._stop.set()


def _titles(port: int) -> list[str]:
    status, body = request(port, "GET", "/api/tasks")
    assert status == 200, body
    return sorted(t["title"] for t in body["data"])


@pytest.mark.parametrize("follower", [_ExitingFollower, _StaleFollower])
def test_a_read_catches_up_when_the_follower_is_not_live(
    hosted_env: HostedEnv, tmp_path: Path, follower: type
) -> None:
    repo, _task = bound_repo(hosted_env, tmp_path)
    with dashboard(repo, follower_factory=follower, restart_after=3600) as port:
        assert _titles(port) == ["Drag me"]
        # The server moves on; nothing streams it to this dashboard.
        hosted_env.server_op("task.create", {"title": "Made elsewhere"})
        assert _titles(port) == ["Drag me", "Made elsewhere"]


def test_a_dead_follower_is_restarted(hosted_env: HostedEnv, tmp_path: Path) -> None:
    repo, _task = bound_repo(hosted_env, tmp_path)
    _ExitingFollower.made.clear()
    notices: list[str] = []
    with dashboard(
        repo, follower_factory=_ExitingFollower, restart_after=0, on_notice=notices.append
    ) as port:
        wait_for(lambda: len(_ExitingFollower.made) == 1)
        time.sleep(0.05)  # let the first follower's thread end
        _titles(port)
    assert len(_ExitingFollower.made) == 2
    assert "restarting the follower" in notices


def test_with_the_server_down_reads_serve_the_cache_and_never_hang(
    hosted_env: HostedEnv, tmp_path: Path
) -> None:
    repo, _task = bound_repo(hosted_env, tmp_path)
    notices: list[str] = []
    hosted_env.stop()
    try:
        with dashboard(
            repo, follower_factory=_ExitingFollower, restart_after=3600, on_notice=notices.append
        ) as port:
            started = time.monotonic()
            for _ in range(4):
                assert _titles(port) == ["Drag me"]
            assert time.monotonic() - started < 5
    finally:
        hosted_env.start()
    # The offline notice, once, however often the page polls.
    offline = [n for n in notices if n.startswith("cannot reach")]
    assert len(offline) == 1, notices
