"""AC-8 and G-8: a hosted checkout offline (SPEC §9.5).

Reads serve the cache with exactly one stderr notice; after a failed catch-up
the next 15 seconds of reads make no network attempt; a write while the server
is down fails with ``SERVER_UNREACHABLE`` and changes nothing on disk (no
offline write queue).
"""

from __future__ import annotations

import json
import socket
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

import pytest

from lattice.core.errors import OpError
from lattice.remote import cache
from tests.test_remote import sync_shim
from tests.test_remote.hosted import HostedEnv, make_repo, run_cli, tree_hash

WINDOW = Path(".lattice/cache/unreachable_until")


def _notices(stderr: str) -> list[str]:
    return [line for line in stderr.splitlines() if line.startswith("lattice: ")]


@pytest.fixture()
def repo(hosted_env: HostedEnv, tmp_path: Path) -> Path:
    repo = make_repo(tmp_path / "repo")
    assert run_cli(repo, "remote", "attach", "team", "demo").exit_code == 0
    created = run_cli(repo, "create", "Offline reading", "--actor", "human:alice")
    assert created.exit_code == 0, created.output
    return repo


@contextmanager
def silent_listener() -> Iterator[dict]:
    """A listener that accepts connections and never answers; counts accepts."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.bind(("127.0.0.1", 0))
    sock.listen(16)
    sock.settimeout(0.05)
    seen = {"accepts": 0, "url": f"http://127.0.0.1:{sock.getsockname()[1]}"}
    held: list[socket.socket] = []
    stop = threading.Event()

    def loop() -> None:
        while not stop.is_set():
            try:
                conn, _ = sock.accept()
            except (TimeoutError, OSError):
                continue
            seen["accepts"] += 1
            held.append(conn)

    thread = threading.Thread(target=loop, daemon=True)
    thread.start()
    try:
        yield seen
    finally:
        stop.set()
        thread.join(timeout=2)
        for conn in held:
            conn.close()
        sock.close()


def test_write_while_stopped_is_refused_and_changes_nothing(
    hosted_env: HostedEnv, repo: Path
) -> None:
    """G-8: no offline write queue."""
    hosted_env.write_remote(retry_seconds=0.2)
    with hosted_env.stopped():
        before = tree_hash(repo)
        for args in (
            ("create", "Queued?", "--actor", "human:alice"),
            ("comment", "DEM-1", "queued?", "--actor", "human:alice"),
            ("create", "Queued?", "--actor", "human:alice", "--json"),
        ):
            result = run_cli(repo, *args)
            assert result.exit_code == 1, result.output
            if "--json" in args:
                error = json.loads(result.stdout)["error"]
                assert error["code"] == "SERVER_UNREACHABLE"
            else:
                assert "Nothing was written" in result.stderr
        assert tree_hash(repo) == before


def test_reads_serve_the_cache_with_one_notice(hosted_env: HostedEnv, repo: Path) -> None:
    with hosted_env.stopped():
        plain = run_cli(repo, "show", "DEM-1")
        assert plain.exit_code == 0, plain.output
        assert "Offline reading" in plain.stdout
        notices = _notices(plain.stderr)
        assert len(notices) == 1
        assert notices[0].startswith("lattice: cannot reach team; showing cache as of ")
        as_json = run_cli(repo, "list", "--json")
        assert as_json.exit_code == 0
        assert json.loads(as_json.stdout)["ok"] is True
        assert len(_notices(as_json.stderr)) == 1


def test_window_skips_the_network_and_a_write_clears_it(
    hosted_env: HostedEnv, repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(cache, "PROBE_SECONDS", 0.5)
    with silent_listener() as listener:
        hosted_env.write_remote(url=listener["url"])
        first = run_cli(repo, "show", "DEM-1", "--json")
        assert first.exit_code == 0, first.output
        assert listener["accepts"] >= 1
        assert (repo / WINDOW).exists()
        accepted = listener["accepts"]
        for _ in range(3):
            again = run_cli(repo, "list")
            assert again.exit_code == 0
            assert len(_notices(again.stderr)) == 1
        assert listener["accepts"] == accepted  # no network attempt in the window
    hosted_env.settings.pop("url")
    hosted_env.write_remote()
    written = run_cli(repo, "comment", "DEM-1", "back online", "--actor", "human:alice")
    assert written.exit_code == 0, written.output
    assert not (repo / WINDOW).exists()


def test_stopped_server_opens_the_window(hosted_env: HostedEnv, repo: Path) -> None:
    with hosted_env.stopped():
        assert run_cli(repo, "list").exit_code == 0
        assert (repo / WINDOW).exists()


def test_busy_server_prints_the_busy_line(
    hosted_env: HostedEnv, repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def busy(project: object, query: dict) -> dict:
        raise OpError("BOARD_BUSY", "project demo is busy; retry shortly.")

    monkeypatch.setattr(sync_shim, "assemble", busy)
    result = run_cli(repo, "show", "DEM-1")
    assert result.exit_code == 0, result.output
    notices = _notices(result.stderr)
    assert len(notices) == 1
    assert notices[0].startswith("lattice: team is busy; showing cache as of ")
    assert not (repo / WINDOW).exists()


def test_interrupted_cache_and_unreachable_server_refuse_reads(
    hosted_env: HostedEnv, repo: Path
) -> None:
    applying = repo / ".lattice" / "cache" / "applying"
    applying.write_text(json.dumps({"remote": "team", "project": "demo", "kind": "delta"}))
    with hosted_env.stopped():
        result = run_cli(repo, "show", "DEM-1", "--json")
    assert result.exit_code == 1
    assert json.loads(result.stdout)["error"]["code"] == "CACHE_INCOMPLETE"


def test_a_fresh_clone_offline_says_there_is_no_cache(
    hosted_env: HostedEnv, tmp_path: Path
) -> None:
    clone = make_repo(tmp_path / "fresh")
    hosted_env.bind(clone)
    with hosted_env.stopped():
        result = run_cli(clone, "list", "--json")
    assert result.exit_code == 1
    error = json.loads(result.stdout)["error"]
    assert error["code"] == "SERVER_UNREACHABLE"
    assert "no cache of team/demo yet" in error["message"]
