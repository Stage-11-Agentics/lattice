"""LAT-358: a gateway's own 502, 503 or 504 (no ``Lattice-Protocol``) means the
server is unreachable, never ``PROXY_REJECTED`` (SPEC §9.1). Writes retry it
with the same ``op_id`` within ``retry_seconds`` (§8.6); a read retries once,
briefly, then takes the offline path (§9.5). Every other non-Lattice answer is
still refused.

Every server here is on ``127.0.0.1``: the real Lattice server behind a small
forwarding gateway that answers the first N matching requests itself.
"""

from __future__ import annotations

import json
import threading
import time
import urllib.error
import urllib.request
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import pytest

from lattice.core.errors import OpError
from lattice.remote import binding, cache, client, http, session, stream
from lattice.remote.follower import Follower
from tests.test_remote.hosted import HostedEnv, make_repo, run_cli
from tests.test_remote.proxies import fixed_answer
from tests.test_remote.stream_stub import stub_remote

WINDOW = Path(".lattice/cache/unreachable_until")
BAD_GATEWAY = (502, {"Content-Type": "text/html"}, b"<html><h1>502 Bad Gateway</h1></html>")
SYNC = "/v1/projects/demo/sync?since=0"


def _notices(stderr: str) -> list[str]:
    return [line for line in stderr.splitlines() if line.startswith("lattice: ")]


# ---------------------------------------------------------------------------
# A gateway in front of the real server
# ---------------------------------------------------------------------------


@dataclass
class Gateway:
    """Forwards every request to *upstream*, except that the next ``fail``
    requests whose path contains ``match`` get ``answer`` from the gateway
    itself (``fail < 0``: every one). With ``forward_first``, those requests
    reach the server too and the gateway discards its answer (a gateway that
    fails after forwarding). ``seen`` lists ``(method, path,
    answered_by_gateway, json_body)``."""

    upstream: str
    url: str = ""
    fail: int = 0
    match: str = ""
    forward_first: bool = False
    answer: tuple[int, dict[str, str], bytes] = BAD_GATEWAY
    seen: list[tuple[str, str, bool, Any]] = field(default_factory=list)
    lock: threading.Lock = field(default_factory=threading.Lock)

    def take(self, path: str) -> bool:
        with self.lock:
            if self.match not in path or self.fail == 0:
                return False
            if self.fail > 0:
                self.fail -= 1
            return True

    def requests(self, fragment: str, *, method: str | None = None) -> list[tuple]:
        return [s for s in self.seen if fragment in s[1] and method in (None, s[0])]


@contextmanager
def gateway(upstream: str) -> Iterator[Gateway]:
    gw = Gateway(upstream)

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, format: str, *args: Any) -> None:
            pass

        def _go(self) -> None:
            raw = self.rfile.read(int(self.headers.get("Content-Length") or 0))
            failed = gw.take(self.path)
            gw.seen.append((self.command, self.path, failed, json.loads(raw) if raw else None))
            if failed and gw.forward_first:
                self._forward(raw)
            if failed:
                status, headers, body = gw.answer
                self.send_response(status)
                for name, value in headers.items():
                    self.send_header(name, value)
            else:
                resp, body = self._forward(raw)
                self.send_response(resp.status)
                for name, value in resp.headers.items():
                    if name.lower() not in ("connection", "content-length", "transfer-encoding"):
                        self.send_header(name, value)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _forward(self, raw: bytes) -> tuple[Any, bytes]:
            req = urllib.request.Request(
                gw.upstream + self.path, data=raw or None, method=self.command
            )
            for name, value in self.headers.items():
                if name.lower() not in ("host", "connection", "content-length"):
                    req.add_header(name, value)
            try:
                resp = urllib.request.urlopen(req, timeout=30)
            except urllib.error.HTTPError as exc:
                resp = exc
            return resp, resp.read()

        do_GET = do_POST = _go

    httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    httpd.daemon_threads = True
    thread = threading.Thread(target=httpd.serve_forever, args=(0.02,), daemon=True)
    thread.start()
    gw.url = f"http://127.0.0.1:{httpd.server_address[1]}"
    try:
        yield gw
    finally:
        httpd.shutdown()
        httpd.server_close()
        thread.join(timeout=5)


@pytest.fixture()
def gw(hosted_env: HostedEnv) -> Iterator[Gateway]:
    with gateway(hosted_env.url) as gw:
        hosted_env.write_remote(url=gw.url)
        yield gw


@pytest.fixture()
def repo(hosted_env: HostedEnv, gw: Gateway, tmp_path: Path) -> Path:
    repo = make_repo(tmp_path / "repo")
    assert run_cli(repo, "remote", "attach", "team", "demo").exit_code == 0
    created = run_cli(repo, "create", "Before the outage", "--actor", "human:alice")
    assert created.exit_code == 0, created.output
    gw.seen.clear()
    return repo


@pytest.fixture()
def read_waits(monkeypatch: pytest.MonkeyPatch) -> list[float]:
    """The waits before a sync's gateway retry (kept short on the real clock)."""
    waits: list[float] = []

    def sleep(seconds: float) -> None:
        waits.append(seconds)
        time.sleep(min(seconds, 0.05))

    monkeypatch.setattr(cache, "_sleep", sleep, raising=False)
    return waits


def _titles_on_server(env: HostedEnv) -> list[str]:
    return [
        json.loads(path.read_text())["title"] for path in sorted((env.board / "tasks").glob("*"))
    ]


# ---------------------------------------------------------------------------
# The transport: which answers are a gateway failure (scenario 5)
# ---------------------------------------------------------------------------


def _remote(url: str) -> http.Remote:
    return http.Remote(alias="team", url=url, token="t")


@pytest.mark.parametrize("status", [502, 503, 504])
@pytest.mark.parametrize("content_type", ["text/html", "application/json", None])
def test_a_gateway_5xx_is_unreachable(status: int, content_type: str | None) -> None:
    headers = {"Content-Type": content_type} if content_type else {}
    with fixed_answer(status, headers, b"<html>gateway</html>") as proxy:
        with pytest.raises(http.GatewayUnavailable) as err:
            http.request(_remote(proxy.url), "GET", SYNC)
    assert isinstance(err.value, http.Unreachable)
    assert err.value.status == status
    # Any of the three may have forwarded the request first (SPEC §8.6).
    assert err.value.sent is True
    assert f"HTTP {status}" in err.value.reason
    assert err.value.retry_after is None


def test_a_gateway_503_carries_its_retry_after() -> None:
    headers = {"Content-Type": "text/html", "Retry-After": "2"}
    with fixed_answer(503, headers, b"<html>busy</html>") as proxy:
        with pytest.raises(http.GatewayUnavailable) as err:
            http.request(_remote(proxy.url), "GET", SYNC)
    assert err.value.retry_after == 2.0


@pytest.mark.parametrize(
    ("status", "headers", "body"),
    [
        (200, {"Content-Type": "text/html"}, b"<html>Sign in</html>"),
        (302, {"Location": "https://login.example.com/start"}, b""),
        (403, {"Content-Type": "text/html"}, b"<html>Forbidden</html>"),
        (500, {"Content-Type": "text/html"}, b"<html>oops</html>"),
        (501, {"Content-Type": "text/html"}, b"<html>not implemented</html>"),
        (520, {"Content-Type": "text/html"}, b"<html>unknown error</html>"),
        (404, {"Content-Type": "application/json"}, b'{"message": "no route"}'),
    ],
    ids=["200-login", "302-login", "403", "500", "501", "520", "404-json"],
)
@pytest.mark.parametrize("expect", ["json", "bytes"])
def test_every_other_non_lattice_answer_is_still_refused(
    status: int, headers: dict[str, str], body: bytes, expect: str
) -> None:
    with fixed_answer(status, headers, body) as proxy, pytest.raises(OpError) as err:
        http.request(_remote(proxy.url), "GET", SYNC, expect=expect)
    assert err.value.code == "PROXY_REJECTED"
    assert f"HTTP {status}" in err.value.message


def test_a_502_with_another_protocol_is_a_mismatch() -> None:
    headers = {"Content-Type": "text/html", "Lattice-Protocol": "2"}
    with fixed_answer(502, headers, b"<html>x</html>") as proxy, pytest.raises(OpError) as err:
        http.request(_remote(proxy.url), "GET", SYNC)
    assert err.value.code == "PROTOCOL_MISMATCH"


def test_a_lattice_502_keeps_its_envelope() -> None:
    headers = {"Content-Type": "application/json", "Lattice-Protocol": "1"}
    body = b'{"ok": false, "error": {"code": "BAD_GATEWAY", "message": "upstream"}}'
    with fixed_answer(502, headers, body) as proxy, pytest.raises(http.ServerError) as err:
        http.request(_remote(proxy.url), "GET", SYNC)
    assert (err.value.code, err.value.status) == ("BAD_GATEWAY", 502)


def test_a_lattice_502_without_an_envelope_is_refused() -> None:
    headers = {"Content-Type": "text/html", "Lattice-Protocol": "1"}
    with fixed_answer(502, headers, b"<html>x</html>") as proxy, pytest.raises(OpError) as err:
        http.request(_remote(proxy.url), "GET", SYNC)
    assert err.value.code == "PROXY_REJECTED"


# ---------------------------------------------------------------------------
# Writes (scenarios 1, 2, 6)
# ---------------------------------------------------------------------------


def test_a_write_after_one_gateway_502_applies_once(
    hosted_env: HostedEnv, gw: Gateway, repo: Path
) -> None:
    gw.match, gw.fail = "/ops/", 1
    result = run_cli(repo, "create", "Through the gateway", "--actor", "human:alice", "--json")
    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout)["data"]["title"] == "Through the gateway"
    assert _notices(result.stderr) == [
        f"lattice: server team ({gw.url}) is not available; retrying for up to 1 s"
    ]
    posts = gw.requests("/ops/", method="POST")
    assert [failed for _, _, failed, _ in posts] == [True, False]
    assert posts[0][3]["op_id"] == posts[1][3]["op_id"]
    assert _titles_on_server(hosted_env).count("Through the gateway") == 1


@pytest.mark.parametrize("status", [502, 503, 504])
def test_a_write_forwarded_before_a_gateway_failure_applies_once(
    hosted_env: HostedEnv, gw: Gateway, repo: Path, status: int
) -> None:
    """The gateway forwards the POST, the server commits it, and the gateway
    answers its own error: the retry (same op_id) is answered from the
    receipt, so the task exists once."""
    gw.match, gw.fail, gw.forward_first = "/ops/", 1, True
    gw.answer = (status, {"Content-Type": "text/html"}, b"<html>gateway</html>")
    result = run_cli(repo, "create", "Forwarded first", "--actor", "human:alice", "--json")
    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout)["data"]["title"] == "Forwarded first"
    posts = gw.requests("/ops/", method="POST")
    assert [failed for _, _, failed, _ in posts] == [True, False]
    assert posts[0][3]["op_id"] == posts[1][3]["op_id"]
    assert _titles_on_server(hosted_env).count("Forwarded first") == 1


def test_op_status_behind_a_gateway_outage_keeps_the_outcome_unknown(
    hosted_env: HostedEnv, gw: Gateway, repo: Path
) -> None:
    """The recovery step after OUTCOME_UNKNOWN never says "Nothing was written"
    when the lookup itself cannot reach the server: the write may exist."""
    hosted_env.write_remote(retry_seconds=0.4)
    gw.match, gw.fail, gw.forward_first = "/ops/", -1, True
    gw.answer = (503, {"Content-Type": "text/html"}, b"<html>gateway</html>")
    write = run_cli(repo, "create", "Maybe written", "--actor", "human:alice", "--json")
    assert write.exit_code == 1
    error = json.loads(write.stdout)["error"]
    assert error["code"] == "OUTCOME_UNKNOWN"
    (op_id,) = {body["op_id"] for _, _, _, body in gw.requests("/ops/", method="POST")}
    assert f"lattice remote op-status {op_id}" in error["message"]
    assert _titles_on_server(hosted_env).count("Maybe written") == 1  # it was written

    gw.forward_first = False  # the lookup (GET .../ops/<op_id>) meets the gateway's 503
    as_json = run_cli(repo, "remote", "op-status", op_id, "--json")
    plain = run_cli(repo, "remote", "op-status", op_id)
    assert as_json.exit_code == 1 and plain.exit_code == 1
    lookup = json.loads(as_json.stdout)["error"]
    assert lookup["code"] == "SERVER_UNREACHABLE"
    assert lookup["details"]["op_id"] == op_id
    for message in (lookup["message"], plain.stderr):
        assert "Nothing was written" not in message
        assert f"look up operation {op_id}" in message
        assert "outcome is still unknown; do not run the write again" in message
        assert f"lattice remote op-status {op_id}" in message

    gw.fail = 0  # the gateway recovers: the lookup settles it
    settled = run_cli(repo, "remote", "op-status", op_id, "--json")
    assert settled.exit_code == 0
    assert json.loads(settled.stdout)["data"]["state"] == "committed"


def test_a_read_only_lookup_never_says_nothing_was_written() -> None:
    """``get_json`` (``remote list``, ``remote status``) cannot reach the
    server: its error makes no claim about writes."""
    with fixed_answer(503, {"Content-Type": "text/html"}, b"<html/>") as proxy:
        with pytest.raises(OpError) as err:
            client.get_json(_remote(proxy.url), "/v1/projects")
    assert err.value.code == "SERVER_UNREACHABLE"
    assert "HTTP 503" in err.value.message
    assert "Nothing was written" not in err.value.message


def test_sync_names_the_gateway_status(gw: Gateway, repo: Path, read_waits: list[float]) -> None:
    gw.match, gw.fail = "", -1
    result = run_cli(repo, "sync", "--json")
    assert result.exit_code == 1
    error = json.loads(result.stdout)["error"]
    assert error["code"] == "SERVER_UNREACHABLE"
    assert error["message"].startswith(
        "Cannot reach team (a gateway in front of it answered HTTP 502"
    )


def test_a_write_past_the_budget_is_outcome_unknown(
    hosted_env: HostedEnv, gw: Gateway, repo: Path
) -> None:
    hosted_env.write_remote(retry_seconds=0.4)
    gw.match, gw.fail = "/ops/", -1
    for args in (("--json",), ()):
        gw.seen.clear()
        started = time.monotonic()
        result = run_cli(repo, "create", "Never through", "--actor", "human:alice", *args)
        assert time.monotonic() - started < 10  # bounded (CI is slow; the budget is 0.4 s)
        assert result.exit_code == 1
        # Never a silent wait: the retry line comes first.
        assert _notices(result.stderr)[0] == (
            f"lattice: server team ({gw.url}) is not available; retrying for up to 0.4 s"
        )
        op_ids = {body["op_id"] for _, _, _, body in gw.requests("/ops/", method="POST")}
        assert len(op_ids) == 1
        (op_id,) = op_ids
        if args:
            error = json.loads(result.stdout)["error"]
            assert error["code"] == "OUTCOME_UNKNOWN"
            message = error["message"]
        else:
            message = result.stderr.splitlines()[-1]
        assert f"lattice remote op-status {op_id}" in message
        assert "PROXY_REJECTED" not in result.output and "not a Lattice" not in result.output
    assert "Never through" not in _titles_on_server(hosted_env)


# The budget and Retry-After, on a fake clock (as in test_client_retries).

OP_ID = "op_01J9Z0000000000000000000CD"
OK = (
    200,
    {"Content-Type": "application/json", "Lattice-Protocol": "1"},
    json.dumps(
        {"ok": True, "data": {"result": {"events": []}, "seq": 7, "op_id": OP_ID}}
    ).encode(),
)


@contextmanager
def scripted(answers: list[tuple]) -> Iterator[dict[str, Any]]:
    """Answer attempt *n* with ``answers[n]`` (the last one repeats), raw."""
    seen: dict[str, Any] = {"bodies": []}

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, format: str, *args: Any) -> None:
            pass

        def do_POST(self) -> None:  # noqa: N802
            length = int(self.headers.get("Content-Length") or 0)
            seen["bodies"].append(json.loads(self.rfile.read(length)))
            status, headers, body = answers[min(len(seen["bodies"]), len(answers)) - 1]
            self.send_response(status)
            for name, value in headers.items():
                self.send_header(name, value)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    httpd.daemon_threads = True
    thread = threading.Thread(target=httpd.serve_forever, args=(0.02,), daemon=True)
    thread.start()
    seen["url"] = f"http://127.0.0.1:{httpd.server_address[1]}"
    try:
        yield seen
    finally:
        httpd.shutdown()
        httpd.server_close()
        thread.join(timeout=5)


START = 1000.0


class Clock(list):
    """The waits between attempts (a wait sliced for progress lines counts
    once), plus each attempt's start time on the fake clock. ``oversleep`` is
    added to every sleep (a loaded host); ``request_seconds`` is how long each
    attempt takes to fail."""

    def __init__(self) -> None:
        super().__init__()
        self.starts: list[float] = []
        self.oversleep = 0.0
        self.request_seconds = 0.0


@pytest.fixture()
def clock(monkeypatch: pytest.MonkeyPatch) -> Clock:
    """A fake clock for the write's retry loop."""
    now = [START]
    waits = Clock()
    pending = [0.0]
    send = http.request

    def sleep(seconds: float) -> None:
        pending[0] += seconds + waits.oversleep
        now[0] += seconds + waits.oversleep

    def request(*args: Any, **kwargs: Any) -> Any:
        if pending[0]:
            waits.append(pending[0])
            pending[0] = 0.0
        waits.starts.append(now[0])
        now[0] += waits.request_seconds
        return send(*args, **kwargs)

    monkeypatch.setattr(client, "_now", lambda: now[0])
    monkeypatch.setattr(client, "_sleep", sleep)
    monkeypatch.setattr(http, "request", request)
    return waits


def _gateway_answer(status: int, retry_after: str | None = None) -> tuple:
    headers = {"Content-Type": "text/html"}
    if retry_after is not None:
        headers["Retry-After"] = retry_after
    return (status, headers, b"<html>gateway</html>")


def _post(url: str, retry_seconds: float = 15.0, *, offline: bool = False) -> dict:
    remote = http.Remote(alias="team", url=url, token="t", retry_seconds=retry_seconds)
    body = {"op_id": OP_ID, "params": {"title": "x"}}
    return client.post_operation(remote, "demo", "task.create", body, offline=offline)


@pytest.mark.parametrize("status", [502, 503, 504])
def test_gateway_answers_are_retried_with_the_same_op_id(
    clock: list[float], status: int, capsys: pytest.CaptureFixture
) -> None:
    with scripted([_gateway_answer(status), _gateway_answer(status), OK]) as server:
        assert _post(server["url"])["seq"] == 7
    assert [b["op_id"] for b in server["bodies"]] == [OP_ID] * 3
    assert clock == [0.5, 1.0]
    assert capsys.readouterr().err.splitlines() == [
        f"lattice: server team ({server['url']}) is not available; retrying for up to 15 s"
    ]


@pytest.mark.parametrize("status", [502, 503, 504])
def test_gateway_failures_past_the_budget_may_have_applied(
    clock: list[float], status: int
) -> None:
    """A gateway's 502, 503, or 504 may have forwarded the request (SPEC §8.6)."""
    with scripted([_gateway_answer(status)]) as server, pytest.raises(OpError) as err:
        _post(server["url"], retry_seconds=3)
    assert err.value.code == "OUTCOME_UNKNOWN"
    assert f"lattice remote op-status {OP_ID}" in err.value.message
    assert sum(clock) <= 3
    assert len(server["bodies"]) == len(clock) + 1


@pytest.mark.parametrize(("oversleep", "request_seconds"), [(0.3, 0.0), (0.0, 0.7), (0.3, 0.7)])
def test_no_attempt_starts_past_the_budget(
    clock: Clock, oversleep: float, request_seconds: float
) -> None:
    """Sleeps that overshoot and attempts that take time never let an attempt
    start after ``retry_seconds``."""
    clock.oversleep, clock.request_seconds = oversleep, request_seconds
    with scripted([_gateway_answer(502)]) as server, pytest.raises(OpError) as err:
        _post(server["url"], retry_seconds=3)
    assert err.value.code == "OUTCOME_UNKNOWN"
    assert len(clock.starts) >= 2
    assert all(start < START + 3 for start in clock.starts), clock.starts


@pytest.mark.parametrize(
    "answer",
    [_gateway_answer(503, retry_after="0"), None],
    ids=["gateway-503", "server-busy"],
)
def test_a_zero_retry_after_waits_the_backoff(clock: Clock, answer: tuple | None) -> None:
    """Retry-After is floored at the current backoff: ``0`` never spins."""
    if answer is None:
        answer = (
            503,
            {"Content-Type": "application/json", "Lattice-Protocol": "1", "Retry-After": "0"},
            b'{"ok": false, "error": {"code": "BOARD_BUSY", "message": "busy"}}',
        )
    with scripted([answer, answer, OK]) as server:
        _post(server["url"])
    assert clock == [0.5, 1.0]
    with scripted([answer]) as server, pytest.raises(OpError) as err:
        _post(server["url"], retry_seconds=3)
    assert err.value.code == "OUTCOME_UNKNOWN"
    assert len(server["bodies"]) <= 4  # 0.5 + 1 + 2 s of backoff fill the budget


def test_a_gateway_retry_after_is_honored_within_the_budget(clock: list[float]) -> None:
    with scripted([_gateway_answer(503, retry_after="2"), OK]) as server:
        _post(server["url"])
    assert clock == [2.0]


def test_a_gateway_retry_after_past_the_budget_gives_up_at_once(clock: list[float]) -> None:
    with (
        scripted([_gateway_answer(503, retry_after="30")]) as server,
        pytest.raises(OpError) as err,
    ):
        _post(server["url"], retry_seconds=15)
    assert err.value.code == "OUTCOME_UNKNOWN"
    assert clock == [] and len(server["bodies"]) == 1


def test_a_gateway_failure_in_the_offline_window_still_retries(clock: list[float]) -> None:
    """The request may have been forwarded, so the write retries for the full
    budget (SPEC §8.6 "No repeated wait" gives up at once only when nothing
    was sent)."""
    with scripted([_gateway_answer(503), OK]) as server:
        assert _post(server["url"], offline=True)["seq"] == 7
    assert clock == [0.5] and len(server["bodies"]) == 2


# ---------------------------------------------------------------------------
# Reads (scenarios 3, 4, 6)
# ---------------------------------------------------------------------------


def test_a_read_after_one_gateway_502_is_served_fresh(
    hosted_env: HostedEnv, gw: Gateway, repo: Path, read_waits: list[float]
) -> None:
    hosted_env.server_op("task.create", {"title": "Made elsewhere"})
    gw.match, gw.fail = "/sync", 1
    for args in (("list",), ("show", "DEM-2", "--json")):
        session.reset_process_state()
        gw.fail = 1
        read_waits.clear()
        result = run_cli(repo, *args)
        assert result.exit_code == 0, result.output
        assert "Made elsewhere" in result.stdout
        assert _notices(result.stderr) == []
        assert read_waits == [cache.GATEWAY_RETRY_SECONDS]
    assert not (repo / WINDOW).exists()


def test_a_read_through_a_gateway_outage_serves_the_cache_once(
    gw: Gateway, repo: Path, read_waits: list[float]
) -> None:
    gw.match, gw.fail = "", -1
    result = run_cli(repo, "list")
    assert result.exit_code == 0, result.output
    assert "Before the outage" in result.stdout
    [notice] = _notices(result.stderr)
    assert notice.startswith("lattice: cannot reach team; showing cache as of ")
    # One retry, briefly: two sync requests, one wait of about a second.
    assert len(gw.requests("/sync")) == 2
    assert read_waits == [cache.GATEWAY_RETRY_SECONDS]
    assert (repo / WINDOW).exists()

    # Inside the window, the next read does not wait again.
    gw.seen.clear()
    read_waits.clear()
    again = run_cli(repo, "show", "DEM-1", "--json")
    assert again.exit_code == 0
    assert json.loads(again.stdout)["data"]["title"] == "Before the outage"
    assert _notices(again.stderr) == [notice]
    assert gw.requests("/sync") == [] and read_waits == []


def test_a_never_synced_read_behind_a_gateway_outage(
    hosted_env: HostedEnv, gw: Gateway, tmp_path: Path, read_waits: list[float]
) -> None:
    fresh = hosted_env.bind(make_repo(tmp_path / "fresh"))
    gw.match, gw.fail = "", -1
    result = run_cli(fresh, "list", "--json")
    assert result.exit_code == 1
    error = json.loads(result.stdout)["error"]
    assert error["code"] == "SERVER_UNREACHABLE"
    assert "has no cache of team/demo yet" in error["message"]
    assert "HTTP 502" in error["message"]
    plain = run_cli(fresh, "list")
    assert plain.exit_code == 1
    assert "no cache of team/demo yet" in plain.stderr and "PROXY_REJECTED" not in plain.output


@pytest.mark.parametrize(
    ("retry_after", "waits", "fresh"),
    [("0.5", [0.5], True), ("5", [], False)],
    ids=["within-budget", "past-budget"],
)
def test_a_read_honors_a_gateway_retry_after_within_its_budget(
    hosted_env: HostedEnv,
    gw: Gateway,
    repo: Path,
    read_waits: list[float],
    retry_after: str,
    waits: list[float],
    fresh: bool,
) -> None:
    hosted_env.server_op("task.create", {"title": "Made elsewhere"})
    gw.match, gw.fail = "/sync", 1
    gw.answer = (503, {"Content-Type": "text/html", "Retry-After": retry_after}, b"<html/>")
    result = run_cli(repo, "list")
    assert result.exit_code == 0, result.output
    assert read_waits == waits
    assert ("Made elsewhere" in result.stdout) is fresh
    assert (_notices(result.stderr) == []) is fresh


def test_a_read_retries_a_gateway_failure_only_once(
    gw: Gateway, repo: Path, read_waits: list[float]
) -> None:
    gw.match, gw.fail = "/sync", 2
    result = run_cli(repo, "list")
    assert result.exit_code == 0
    assert len(gw.requests("/sync")) == 2 and read_waits == [cache.GATEWAY_RETRY_SECONDS]
    assert _notices(result.stderr)[0].startswith("lattice: cannot reach team")


def test_the_probe_budget_bounds_the_gateway_retry(
    gw: Gateway, repo: Path, read_waits: list[float], monkeypatch: pytest.MonkeyPatch
) -> None:
    """No retry when the wait would leave the probe budget under a second."""
    monkeypatch.setattr(cache, "PROBE_SECONDS", 1.5)
    gw.match, gw.fail = "/sync", 1
    result = run_cli(repo, "list")
    assert result.exit_code == 0
    assert read_waits == [] and len(gw.requests("/sync")) == 1


# ---------------------------------------------------------------------------
# The follower, the stream, and the dashboard's catch-up (scenario 7)
# ---------------------------------------------------------------------------


def test_a_bulk_sync_behind_a_gateway_outage_is_unreachable(
    gw: Gateway, repo: Path, read_waits: list[float]
) -> None:
    """``lattice sync`` and the follower's syncs: an outcome, not PROXY_REJECTED."""
    gw.match, gw.fail = "", -1
    outcome = cache.catch_up(repo, bulk=True)
    assert outcome.kind == "unreachable"
    assert "HTTP 502" in (outcome.detail or "")
    assert read_waits == [cache.GATEWAY_RETRY_SECONDS]


def test_the_follower_keeps_following_through_a_gateway_outage(
    gw: Gateway, repo: Path, read_waits: list[float]
) -> None:
    """Before LAT-358 the sync's PROXY_REJECTED was fatal and stopped the follower."""
    gw.match, gw.fail = "", -1
    follower = Follower(
        repo,
        stub_remote(gw.url),
        "demo",
        catch_up=cache.catch_up,
        heartbeat_seconds=0.2,
        max_backoff=0.2,
    )
    thread = threading.Thread(target=follower.run, daemon=True)
    thread.start()
    try:
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline and not (
            follower.syncs >= 1 and follower.last_stream_error is not None
        ):
            time.sleep(0.02)
        assert thread.is_alive()
        assert follower.last_stream_error is not None
        assert follower.last_stream_error.code == "SERVER_UNREACHABLE"
        assert "HTTP 502" in (follower.last_sync_error or "")
    finally:
        follower.stop()
        thread.join(timeout=5)


@pytest.mark.parametrize("status", [502, 503, 504])
def test_a_gateway_5xx_on_the_stream_is_unreachable(status: int) -> None:
    with fixed_answer(status, {"Content-Type": "text/html"}, b"<html/>") as proxy:
        with pytest.raises(OpError) as err:
            stream.open_stream(_remote(proxy.url), "demo", last_event_id=None, timeout=2)
    assert err.value.code == "SERVER_UNREACHABLE"
    assert f"HTTP {status}" in err.value.message


def test_the_dashboard_catch_up_serves_the_cache_through_a_gateway_outage(
    gw: Gateway, repo: Path, read_waits: list[float]
) -> None:
    hosted = binding.classify(repo)
    assert hosted is not None
    gw.match, gw.fail = "", -1
    lines: list[str] = []
    session.catch_up_unless_live(hosted, defer_to_running_sync=True, notify=lines.append)
    assert len(lines) == 1 and lines[0].startswith("cannot reach team; showing cache as of ")
    assert (repo / WINDOW).exists()
