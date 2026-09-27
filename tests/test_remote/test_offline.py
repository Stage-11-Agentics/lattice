"""AC-8 and G-8: a hosted checkout offline (SPEC §9.5).

Reads serve the cache with exactly one stderr notice; after a failed catch-up
the next 15 seconds of reads make no network attempt; a write while the server
is down fails with ``SERVER_UNREACHABLE`` and changes nothing on disk but the
offline window (no offline write queue). Offline writes say so in plain words,
show progress, and wait once per outage (SPEC §8.6).
"""

from __future__ import annotations

import json
import os
import socket
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

import pytest

from lattice.core.errors import OpError
from lattice.remote import cache, client, http
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


def _without_window(tree: dict[str, str]) -> dict[str, str]:
    return {k: v for k, v in tree.items() if k != WINDOW.as_posix()}


def test_write_while_stopped_is_refused_and_changes_nothing(
    hosted_env: HostedEnv, repo: Path
) -> None:
    """G-8: no offline write queue. The one file a refused write leaves is the
    offline window, so the next write does not wait again (SPEC §8.6)."""
    hosted_env.write_remote(retry_seconds=0.2)
    with hosted_env.stopped():
        before = _without_window(tree_hash(repo))
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
            assert (repo / WINDOW).exists()
        assert _without_window(tree_hash(repo)) == before


@pytest.fixture()
def progress_times(monkeypatch: pytest.MonkeyPatch) -> list[float]:
    """When each retry progress line was written (``time.monotonic``)."""
    times: list[float] = []
    write = client._progress

    def timed(line: str) -> None:
        times.append(time.monotonic())
        write(line)

    monkeypatch.setattr(client, "_progress", timed)
    return times


def test_offline_write_says_so_plainly_with_progress(
    hosted_env: HostedEnv, repo: Path, progress_times: list[float]
) -> None:
    """G-8 (SPEC §8.6): the not-available line at once, naming alias and URL;
    no raw OS error and no operation ID on stderr or in the plain error; the
    raw OS error only in ``--json`` details."""
    hosted_env.write_remote(retry_seconds=3)
    with hosted_env.stopped():
        url = hosted_env.url
        started = time.monotonic()
        plain = run_cli(repo, "create", "Offline write", "--actor", "human:alice")
        ended = time.monotonic()
    assert plain.exit_code == 1
    assert plain.stdout == ""
    assert _notices(plain.stderr) == [
        f"lattice: server team ({url}) is not available; retrying for up to 3 s"
    ]
    assert plain.stderr.splitlines()[-1] == (
        f"Error: server team ({url}) is not available. Nothing was written; "
        "run the command again when it is back."
    )
    text = plain.stderr.lower()
    assert "errno" not in text and "refused" not in text and "op_" not in text
    # At once, then one line within each 5 s until it gives up.
    assert progress_times[0] - started < 1.0
    marks = [*progress_times, ended]
    assert all(b - a <= 5.5 for a, b in zip(marks, marks[1:], strict=False))

    assert (repo / WINDOW).exists()  # the next write will not wait again

    # Past the window, a --json write waits again and says so on stderr alone
    # (a 1 s budget keeps the test short).
    (repo / WINDOW).unlink()
    hosted_env.write_remote(retry_seconds=1)
    with hosted_env.stopped():
        url = hosted_env.url
        as_json = run_cli(repo, "create", "Offline write", "--actor", "human:alice", "--json")
    assert as_json.exit_code == 1
    assert _notices(as_json.stderr) == [
        f"lattice: server team ({url}) is not available; retrying for up to 1 s"
    ]
    assert "op_" not in as_json.stderr and "errno" not in as_json.stderr.lower()
    error = json.loads(as_json.stdout)["error"]
    assert error["code"] == "SERVER_UNREACHABLE"
    assert "errno" not in error["message"].lower()
    details = error["details"]
    assert set(details) == {"remote", "url", "os_error", "waited_seconds"}
    assert (details["remote"], details["url"]) == ("team", url)
    assert "refused" in details["os_error"].lower()
    assert 0 < details["waited_seconds"] <= 1


def test_a_second_write_in_the_window_does_not_wait_again(
    hosted_env: HostedEnv, repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """G-8 (SPEC §8.6 "No repeated wait"): a write started inside the offline
    window (opened by an earlier write or read) fails at once, after one
    attempt. The test above shows a refused write opening the window."""
    hosted_env.write_remote(retry_seconds=3)
    attempts: list[str] = []
    send = http.request

    def counting(remote: http.Remote, method: str, path: str, **kwargs: object) -> object:
        attempts.append(f"{method} {path}")
        return send(remote, method, path, **kwargs)

    with hosted_env.stopped():
        (repo / WINDOW).write_text(f"{time.time() + 15:.3f}\n")
        monkeypatch.setattr(http, "request", counting)
        for args in (
            ("comment", "DEM-1", "second", "--actor", "human:alice"),
            ("comment", "DEM-1", "second", "--actor", "human:alice", "--json"),
        ):
            attempts.clear()
            started = time.monotonic()
            second = run_cli(repo, *args)
            assert time.monotonic() - started < 3
            assert second.exit_code == 1
            assert len(attempts) == 1 and "/ops/" in attempts[0]
            assert "retrying" not in second.stderr
            if "--json" in args:
                error = json.loads(second.stdout)["error"]
                assert error["code"] == "SERVER_UNREACHABLE"
                assert error["details"]["waited_seconds"] == 0
            else:
                assert "is not available. Nothing was written" in second.stderr


def test_a_lost_response_still_retries_fully_in_the_window(
    hosted_env: HostedEnv, repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """G-8 (SPEC §8.6): a write whose request was sent may have committed, so
    it retries for the full window even inside the offline window, and ends in
    ``OUTCOME_UNKNOWN`` naming its operation."""
    monkeypatch.setattr(client, "OP_POLICY", http.Policy(1.0, 0.3))
    with silent_listener() as listener:
        hosted_env.write_remote(url=listener["url"], retry_seconds=1)
        (repo / WINDOW).write_text(f"{time.time() + 60:.3f}\n")
        started = time.monotonic()
        result = run_cli(repo, "create", "Lost", "--actor", "human:alice", "--json")
        elapsed = time.monotonic() - started
        accepts = listener["accepts"]
    assert result.exit_code == 1
    error = json.loads(result.stdout)["error"]
    assert error["code"] == "OUTCOME_UNKNOWN"
    op_id = error["message"].split("(operation ", 1)[1].split(")", 1)[0]
    assert op_id.startswith("op_")
    assert f"lattice remote op-status {op_id}" in error["message"]
    assert accepts >= 2  # retried, not given up at once
    assert elapsed >= 0.9


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
    from lattice.server import app as server_app

    def busy(*args: object, **kwargs: object) -> dict:
        raise OpError("BOARD_BUSY", "project demo is busy; retry shortly.")

    # The checkout is at the head, so the server answers from its fast path.
    monkeypatch.setattr(server_app, "fast_path_body", busy)
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


def test_long_running_commands_read_without_holding_the_lock(
    hosted_env: HostedEnv, repo: Path
) -> None:
    """``dashboard``, ``watch``, and ``wait`` run until stopped: they catch up
    before their first read like any command, but holding the shared read lock
    for their lifetime would starve every sync, so they read without it."""
    import click

    from lattice.cli.helpers import require_root
    from lattice.cli.main import cli
    from lattice.remote import session

    previous = Path.cwd()
    os.chdir(repo)
    try:
        for name, held in (
            ("watch", False),
            ("wait", False),
            ("dashboard", False),
            ("list", True),
        ):
            with click.Context(cli, info_name="lattice") as root_ctx:
                with click.Context(click.Command(name), parent=root_ctx, info_name=name):
                    require_root(False)
                    assert bool(session._locks) is held, name
                    assert session._fresh, name  # caught up either way
            session.reset_process_state()
    finally:
        os.chdir(previous)
