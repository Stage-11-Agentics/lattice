"""The client's retry policy for one operation call (SPEC §8.6, "Client retries").

A scripted listener answers each attempt; a fake clock makes the waits free.
The kill-and-restart proof against a real server is H-22's (AC-46).
"""

from __future__ import annotations

import json
import socket
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import click
import pytest

from lattice.core.errors import OpError
from lattice.remote import client, http

OP_ID = "op_01J9Z0000000000000000000CD"
BODY = {"op_id": OP_ID, "params": {"title": "x"}}
OK = (200, {}, {"ok": True, "data": {"result": {"events": []}, "seq": 7, "op_id": OP_ID}})


def _error(status: int, code: str, retry_after: str | None = None) -> tuple:
    headers = {"Retry-After": retry_after} if retry_after else {}
    return (status, headers, {"ok": False, "error": {"code": code, "message": code.lower()}})


@contextmanager
def scripted(answers: list[tuple]) -> Iterator[dict[str, Any]]:
    """Answer attempt *n* with ``answers[n]`` (the last one repeats); ``"hang"``
    reads the request and never answers."""
    seen: dict[str, Any] = {"bodies": []}
    release = threading.Event()

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, format: str, *args: Any) -> None:
            pass

        def do_POST(self) -> None:  # noqa: N802
            length = int(self.headers.get("Content-Length") or 0)
            seen["bodies"].append(json.loads(self.rfile.read(length)))
            answer = answers[min(len(seen["bodies"]) - 1, len(answers) - 1)]
            if answer == "hang":
                release.wait(10)
                return
            status, headers, body = answer
            raw = json.dumps(body).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Lattice-Protocol", "1")
            for name, value in headers.items():
                self.send_header(name, value)
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

    httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    httpd.daemon_threads = True
    thread = threading.Thread(target=httpd.serve_forever, args=(0.02,), daemon=True)
    thread.start()
    seen["url"] = f"http://127.0.0.1:{httpd.server_address[1]}"
    try:
        yield seen
    finally:
        release.set()
        httpd.shutdown()
        httpd.server_close()
        thread.join(timeout=5)


@pytest.fixture()
def clock(monkeypatch: pytest.MonkeyPatch) -> list[float]:
    """A fake clock: each sleep advances it; returns the list of waits between
    attempts (a wait sliced for progress lines counts once)."""
    now = [1000.0]
    waits: list[float] = []
    pending = [0.0]
    send = http.request

    def sleep(seconds: float) -> None:
        pending[0] += seconds
        now[0] += seconds

    def request(*args: Any, **kwargs: Any) -> Any:
        if pending[0]:
            waits.append(pending[0])
            pending[0] = 0.0
        return send(*args, **kwargs)

    monkeypatch.setattr(client, "_now", lambda: now[0])
    monkeypatch.setattr(client, "_sleep", sleep)
    monkeypatch.setattr(http, "request", request)
    return waits


def _remote(url: str, retry_seconds: float = 15.0) -> http.Remote:
    return http.Remote(alias="team", url=url, token="t", retry_seconds=retry_seconds)


def _post(url: str, retry_seconds: float = 15.0, *, offline: bool = False) -> dict:
    return client.post_operation(
        _remote(url, retry_seconds), "demo", "task.create", dict(BODY), offline=offline
    )


def _closed_port() -> str:
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()  # nothing listens here
    return f"http://127.0.0.1:{port}"


def test_lookup_hint_uses_invoked_program_name() -> None:
    remote = _remote("http://127.0.0.1:1")
    with click.Context(click.Command("lattice-v2"), info_name="lattice-v2"):
        error = client.lookup_unreachable(remote, OP_ID, "connection refused")
    assert f"lattice-v2 remote op-status {OP_ID}" in error.message
    assert f"lattice remote op-status {OP_ID}" not in error.message


@pytest.mark.parametrize(
    "transient",
    [
        _error(429, "RATE_LIMITED"),
        _error(503, "BOARD_BUSY"),
        _error(502, "BAD_GATEWAY"),
        _error(504, "GATEWAY_TIMEOUT"),
    ],
)
def test_transient_answers_are_retried_with_the_same_op_id(
    clock: list[float], transient: tuple, capsys: pytest.CaptureFixture
) -> None:
    with scripted([transient, transient, OK]) as server:
        data = _post(server["url"])
    assert data["seq"] == 7
    assert capsys.readouterr().err.splitlines() == [
        f"lattice: server team ({server['url']}) is busy; retrying for up to 15 s"
    ]
    assert [b["op_id"] for b in server["bodies"]] == [OP_ID] * 3
    assert clock == [0.5, 1.0]  # backoff from 0.5 s, doubling


def test_retry_after_is_honored(clock: list[float]) -> None:
    with scripted([_error(503, "BOARD_BUSY", retry_after="2"), OK]) as server:
        _post(server["url"])
    assert clock == [2.0]


def test_backoff_caps_at_five_seconds(clock: list[float]) -> None:
    busy = _error(503, "BOARD_BUSY")
    with scripted([busy] * 6 + [OK]) as server:
        _post(server["url"], retry_seconds=30)
    assert clock == [0.5, 1.0, 2.0, 4.0, 5.0, 5.0]


@pytest.mark.parametrize(
    "final",
    [_error(503, "BOARD_UNAVAILABLE"), _error(500, "INTERNAL_ERROR"), _error(409, "CONFLICT")],
)
def test_other_errors_are_not_retried(clock: list[float], final: tuple) -> None:
    with scripted([final]) as server, pytest.raises(OpError) as exc:
        _post(server["url"])
    assert exc.value.code == final[2]["error"]["code"]
    assert len(server["bodies"]) == 1
    assert clock == []


def test_giving_up_after_the_server_answered_is_outcome_unknown(clock: list[float]) -> None:
    with scripted([_error(503, "BOARD_BUSY")]) as server, pytest.raises(OpError) as exc:
        _post(server["url"], retry_seconds=3)
    assert exc.value.code == "OUTCOME_UNKNOWN"
    assert OP_ID in exc.value.message
    assert f"lattice remote op-status {OP_ID}" in exc.value.message
    assert sum(clock) <= 3


def test_a_lost_response_is_outcome_unknown(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(client, "OP_POLICY", http.Policy(1.0, 0.3))
    with scripted(["hang"]) as server, pytest.raises(OpError) as exc:
        _post(server["url"], retry_seconds=0)
    assert exc.value.code == "OUTCOME_UNKNOWN"
    assert len(server["bodies"]) == 1


def test_never_connecting_is_server_unreachable(
    clock: list[float], capsys: pytest.CaptureFixture
) -> None:
    url = _closed_port()
    with pytest.raises(OpError) as exc:
        _post(url, retry_seconds=2)
    assert exc.value.code == "SERVER_UNREACHABLE"
    assert exc.value.message == (
        f"server team ({url}) is not available. Nothing was written; "
        "run the command again when it is back."
    )
    details = exc.value.details
    assert set(details) == {"remote", "url", "os_error", "waited_seconds"}
    assert (details["remote"], details["url"]) == ("team", url)
    assert "refused" in details["os_error"].lower()
    assert details["waited_seconds"] == 1.9
    # The last wait ends just before the deadline (no attempt starts past it).
    assert clock == pytest.approx([0.5, 1.0, 2 - 1.5 - client.LAST_ATTEMPT_MARGIN_SECONDS])
    err = capsys.readouterr().err
    assert err.splitlines() == [
        f"lattice: server team ({url}) is not available; retrying for up to 2 s"
    ]
    assert OP_ID not in err and "Errno" not in err


def test_progress_every_five_seconds(clock: list[float], capsys: pytest.CaptureFixture) -> None:
    """SPEC §8.6 "No silent wait": the not-available line at once, then one line
    per 5 s, with no operation ID and no raw OS error."""
    url = _closed_port()
    with pytest.raises(OpError) as exc:
        _post(url, retry_seconds=16)
    assert exc.value.code == "SERVER_UNREACHABLE"
    # Backoff 0.5, 1, 2, 4, 5 s, then the last wait ends just before the 16 s deadline.
    assert clock == pytest.approx([0.5, 1.0, 2.0, 4.0, 5.0, 3.4])
    lines = capsys.readouterr().err.splitlines()
    assert lines == [
        f"lattice: server team ({url}) is not available; retrying for up to 16 s",
        "lattice: team still not available (5 s of 16 s)",
        "lattice: team still not available (10 s of 16 s)",
        "lattice: team still not available (15 s of 16 s)",
    ]


def test_busy_progress_says_busy(clock: list[float], capsys: pytest.CaptureFixture) -> None:
    busy = _error(503, "BOARD_BUSY")
    with scripted([busy] * 4 + [OK]) as server:
        _post(server["url"])
    assert capsys.readouterr().err.splitlines() == [
        f"lattice: server team ({server['url']}) is busy; retrying for up to 15 s",
        "lattice: team still busy (5 s of 15 s)",
    ]


def test_offline_window_gives_up_at_once(
    clock: list[float], capsys: pytest.CaptureFixture
) -> None:
    """SPEC §8.6 "No repeated wait": a first attempt that cannot connect ends
    the write at once, with no progress lines."""
    with pytest.raises(OpError) as exc:
        _post(_closed_port(), offline=True)
    assert exc.value.code == "SERVER_UNREACHABLE"
    assert exc.value.details["waited_seconds"] == 0
    assert clock == []
    assert capsys.readouterr().err == ""


def test_offline_window_still_retries_a_server_that_answers(clock: list[float]) -> None:
    """Only a failed connection gives up early: a busy server is retried as usual."""
    with scripted([_error(503, "BOARD_BUSY"), OK]) as server:
        assert _post(server["url"], offline=True)["seq"] == 7
    assert clock == [0.5]


@pytest.mark.parametrize("header", ["-1", "NaN", "Infinity", "-Infinity", "1e309", "soon"])
def test_an_unusable_retry_after_falls_back_to_backoff(clock: list[float], header: str) -> None:
    with scripted([_error(503, "BOARD_BUSY", retry_after=header), OK]) as server:
        _post(server["url"])
    assert clock == [0.5]


def test_a_huge_retry_after_is_capped(clock: list[float]) -> None:
    with scripted([_error(429, "RATE_LIMITED", retry_after="100000"), OK]) as server:
        _post(server["url"], retry_seconds=120)
    assert clock == [http.MAX_RETRY_AFTER_SECONDS]


@pytest.mark.parametrize("value", ["NaN", "Infinity", "1e309", "-1", "3601", "true", '"5"'])
def test_retry_seconds_must_be_finite_and_bounded(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, value: str
) -> None:
    from lattice.remote.config import resolve_remote

    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    path = tmp_path / "lattice" / "remotes.json"
    path.parent.mkdir()
    path.write_text(
        '{"remotes": {"team": {"url": "https://h.example.com", "retry_seconds": ' + value + "}}}"
    )
    path.chmod(0o600)
    with pytest.raises(OpError) as exc:
        resolve_remote("team")
    assert exc.value.code == "VALIDATION_ERROR"
    assert "retry_seconds" in exc.value.message


def test_retry_seconds_within_bounds_is_accepted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from lattice.remote.config import resolve_remote

    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    path = tmp_path / "lattice" / "remotes.json"
    path.parent.mkdir()
    path.write_text('{"remotes": {"team": {"url": "https://h.example.com", "retry_seconds": 0}}}')
    path.chmod(0o600)
    assert resolve_remote("team").retry_seconds == 0.0


def _progress_threads() -> list[threading.Thread]:
    return [t for t in threading.enumerate() if t.name == "lattice-write-progress"]


def test_progress_fake_clock_covers_in_flight_retry_and_post_deadline_lines(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    now = [10.0]
    monkeypatch.setattr(client, "PROGRESS_SECONDS", 1.0)
    monkeypatch.setattr(client, "_now", lambda: now[0])
    lines: list[str] = []
    monkeypatch.setattr(client, "_progress", lines.append)

    progress = client._Progress(_remote("http://fake", retry_seconds=3.0), 10.0, 13.0)
    now[0] = 11.0
    progress.tick(in_flight=True)  # The request can still be in flight before its first failure.
    progress.state = "busy"
    progress.begin(now[0])
    now[0] = 12.0
    progress.tick(in_flight=True)  # Retry progress is distinct from uncertain waiting.
    now[0] = 13.0
    progress.tick(in_flight=True)
    now[0] = 14.0
    progress.tick(in_flight=True)

    warning = "if the request reached it, the write may have applied"
    assert lines[0].startswith("still waiting for team to answer (")
    assert lines[0].endswith(warning)
    assert lines[1].endswith("is busy; retrying for up to 3 s")
    assert lines[2].startswith("team still busy (")
    assert len(lines[3:]) == 2
    assert all(line.startswith("still waiting for team to answer (") for line in lines[3:])
    assert all(line.endswith(warning) for line in lines[3:])
    assert all("op_" not in line and "errno" not in line.lower() for line in lines)


def test_post_operation_progress_uses_real_ticker_with_fake_http_and_clock(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    started = 100.0
    retry_seconds = 0.12
    interval = 0.005  # The real Event wait has a 0.01 s floor; Events control ordering.
    deadline = started + retry_seconds
    clock_lock = threading.Lock()
    now = [started]
    request1_entered = threading.Event()
    pre_notice = threading.Event()
    request2_entered = threading.Event()
    release_request2 = threading.Event()
    operation_done = threading.Event()
    lines_changed = threading.Condition()
    lines: list[str] = []
    post_deadline_waits = 0
    outcomes: list[Any] = []
    output_at_return: list[int] = []

    def fake_now() -> float:
        with clock_lock:
            return now[0]

    def set_now(value: float) -> None:
        with clock_lock:
            now[0] = value

    def fake_sleep(seconds: float) -> None:
        with clock_lock:
            now[0] += seconds

    def progress(line: str) -> None:
        nonlocal post_deadline_waits
        with lines_changed:
            lines.append(line)
            if line.startswith("still waiting for team to answer ("):
                if fake_now() < deadline:
                    pre_notice.set()
                else:
                    post_deadline_waits += 1
                    lines_changed.notify_all()

    monkeypatch.setattr(client, "PROGRESS_SECONDS", interval)
    monkeypatch.setattr(client, "_now", fake_now)
    monkeypatch.setattr(client, "_sleep", fake_sleep)
    monkeypatch.setattr(client, "_progress", progress)
    monkeypatch.setattr(client, "OP_POLICY", http.Policy(1.0, 1.5))

    calls = 0

    def request(*args: Any, **kwargs: Any) -> Any:
        nonlocal calls
        calls += 1
        if calls == 1:
            request1_entered.set()
            if not pre_notice.wait(10):
                raise AssertionError("ticker did not emit the forced pre-notice line")
            raise http.ServerError("BOARD_BUSY", "busy", {}, status=503)
        if calls == 2:
            request2_entered.set()
            if not release_request2.wait(10):
                raise AssertionError("test did not release the post-deadline request")
            raise http.Unreachable("read timed out", sent=True)
        raise AssertionError(f"unexpected HTTP attempt {calls}")

    monkeypatch.setattr(http, "request", request)

    def run_operation() -> None:
        try:
            outcomes.append(
                client.post_operation(
                    _remote("http://fake", retry_seconds=retry_seconds),
                    "demo",
                    "task.create",
                    dict(BODY),
                )
            )
        except Exception as exc:  # surfaced in the test thread below
            outcomes.append(exc)
        finally:
            with lines_changed:
                output_at_return.append(len(lines))
            operation_done.set()

    operation = threading.Thread(target=run_operation, name="lat394-post-operation")
    operation.start()
    try:
        assert request1_entered.wait(10)
        set_now(started + interval)
        assert pre_notice.wait(10)
        assert request2_entered.wait(10)

        set_now(deadline + interval)
        for expected in range(1, 4):
            with lines_changed:
                assert lines_changed.wait_for(lambda: post_deadline_waits >= expected, timeout=10)
            if expected < 3:
                with clock_lock:
                    now[0] += interval
    finally:
        pre_notice.set()
        release_request2.set()
        assert operation_done.wait(10)
        operation.join(timeout=10)

    assert not operation.is_alive()
    assert len(lines) == output_at_return[0]
    assert len(outcomes) == 1
    assert isinstance(outcomes[0], OpError) and outcomes[0].code == "OUTCOME_UNKNOWN"
    assert calls == 2
    assert lines[0].startswith("still waiting for team to answer (")
    assert lines[0].endswith("if the request reached it, the write may have applied")
    retry_notice = next(i for i, line in enumerate(lines) if "is busy; retrying" in line)
    assert retry_notice > 0
    assert lines[retry_notice].endswith("is busy; retrying for up to 0.12 s")
    busy = [line for line in lines if "still busy" in line]
    waiting = [line for line in lines if line.startswith("still waiting for team to answer (")]
    assert busy and len(waiting) >= 4
    assert all(
        line.endswith("if the request reached it, the write may have applied") for line in waiting
    )
    assert all("op_" not in line and "errno" not in line.lower() for line in lines)
    assert not _progress_threads()


@pytest.mark.perf  # LAT-363 timing lane; LAT-394 keeps the real-clock cadence check here.
def test_progress_keeps_coming_while_a_request_blocks_past_the_window(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
) -> None:
    """SPEC §8.6 "No silent wait": a retry the server took and never answers
    gets a line per interval for as long as it is in flight, past the retry
    window up to its read timeout, worded so the numbers stay true; the ticker
    stops with the call and nothing prints after it returns (real clock, a
    0.2 s interval, a 0.6 s window, a 1.5 s read timeout)."""
    monkeypatch.setattr(client, "PROGRESS_SECONDS", 0.2)
    monkeypatch.setattr(client, "OP_POLICY", http.Policy(1.0, 1.5))
    times: list[float] = []
    write = client._progress
    monkeypatch.setattr(
        client, "_progress", lambda line: (times.append(time.monotonic()), write(line))
    )
    with scripted([_error(503, "BOARD_BUSY"), "hang"]) as server, pytest.raises(OpError) as exc:
        started = time.monotonic()
        _post(server["url"], retry_seconds=0.6)
    ended = time.monotonic()
    assert exc.value.code == "OUTCOME_UNKNOWN"
    assert len(server["bodies"]) == 2  # the retry blocked for its whole read timeout
    assert ended - started >= 1.9  # 0.5 s backoff + 1.5 s read timeout, past the window
    assert not _progress_threads()
    lines = capsys.readouterr().err.splitlines()
    leading_waiting: str | None = None
    if lines and lines[0].startswith("lattice: still waiting for team to answer ("):
        leading_waiting = lines.pop(0)
    if leading_waiting is not None:
        assert leading_waiting.endswith("if the request reached it, the write may have applied")
    assert lines[0].endswith("is busy; retrying for up to 0.6 s")
    within = [line for line in lines if "still busy" in line]
    waiting = [line for line in lines if "still waiting for team to answer" in line]
    assert within and len(waiting) >= 5
    assert lines == [lines[0], *within, *waiting]  # the window's lines, then the waiting ones
    assert all(
        line.endswith("if the request reached it, the write may have applied") for line in waiting
    )
    transcript = ([leading_waiting] if leading_waiting is not None else []) + lines
    assert all("op_" not in line and "errno" not in line.lower() for line in transcript)
    marks = [started, *times, ended]
    assert max(b - a for a, b in zip(marks, marks[1:], strict=False)) < 0.5
    time.sleep(0.5)  # a ticker left behind would print here
    assert capsys.readouterr().err == ""
    assert not _progress_threads()


def test_a_first_attempt_in_flight_is_not_silent(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
) -> None:
    """Before any failure the write is not retrying yet, but a request the
    server took and has not answered still gets the waiting line."""
    monkeypatch.setattr(client, "PROGRESS_SECONDS", 0.2)
    monkeypatch.setattr(client, "OP_POLICY", http.Policy(1.0, 0.7))
    with scripted(["hang"]) as server, pytest.raises(OpError) as exc:
        _post(server["url"], retry_seconds=0.3)
    assert exc.value.code == "OUTCOME_UNKNOWN"
    lines = capsys.readouterr().err.splitlines()
    assert len(lines) >= 2
    assert all(line.startswith("lattice: still waiting for team to answer (") for line in lines)
    assert not _progress_threads()


def test_a_slow_first_attempt_does_not_print_two_lines_at_once(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
) -> None:
    """The next line is scheduled from the previous one, not from the start: a
    first attempt that took 5 s (a connect timeout) prints one line, not two."""
    now = [1000.0]

    def slow_refusal(*args: Any, **kwargs: Any) -> Any:
        now[0] += 5.0
        raise http.Unreachable("timed out")

    def sleep(seconds: float) -> None:
        now[0] += seconds

    monkeypatch.setattr(client, "_now", lambda: now[0])
    monkeypatch.setattr(client, "_sleep", sleep)
    monkeypatch.setattr(http, "request", slow_refusal)
    with pytest.raises(OpError):
        _post("http://127.0.0.1:9", retry_seconds=12)
    lines = capsys.readouterr().err.splitlines()
    # The first line at 5 s (after the slow attempt), the next one 5 s later at
    # 10.5 s; never a "5 s" line right after the first.
    assert lines == [
        "lattice: server team (http://127.0.0.1:9) is not available; retrying for up to 12 s",
        "lattice: team still not available (10 s of 12 s)",
    ]
