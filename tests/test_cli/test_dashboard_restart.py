"""Dashboard restart re-execs cleanly and signals only its listening process."""

from __future__ import annotations

import http.client
import json
import os
import queue
import shutil
import signal
import socket
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler
from pathlib import Path
from socketserver import ThreadingMixIn
from types import SimpleNamespace

import pytest
from click.testing import CliRunner

from lattice.cli import dashboard_cmd as dashboard_module
from lattice.cli.main import cli
from lattice.dashboard.api import get_task_comments
from lattice.dashboard import server as dashboard_server_module


def test_sighup_restart_execs_after_dashboard_target_closes(tmp_path: Path, monkeypatch) -> None:
    closed: list[str] = []
    exec_calls: list[tuple[str, list[str]]] = []
    signal_calls: list[tuple[int, object]] = []

    class RestartExec(Exception):
        pass

    def target(_lattice_dir, stack, _output_json):
        stack.callback(closed.append, "target")
        return object()

    def set_handler(signum: int, handler) -> object:
        signal_calls.append((signum, handler))
        return signal.SIG_DFL

    def execv(executable: str, args: list[str]) -> None:
        exec_calls.append((executable, args))
        closed.append("exec")
        assert signal_calls[-1] == (signal.SIGHUP, signal.SIG_IGN)
        assert dashboard_module.os.environ[dashboard_module._RESTART_ENV] == "1"
        raise RestartExec

    monkeypatch.setattr(dashboard_module, "require_root", lambda _json: tmp_path / ".lattice")
    monkeypatch.setattr(dashboard_module, "_dashboard_target", target)
    monkeypatch.setattr(dashboard_module, "_serve", lambda *_args: True)
    monkeypatch.setattr(dashboard_module.signal, "signal", set_handler)
    monkeypatch.setattr(dashboard_module.shutil, "which", lambda _script: "/test/bin/lattice")
    monkeypatch.setattr(dashboard_module.os, "execv", execv)
    monkeypatch.setattr(dashboard_module.sys, "argv", ["lattice", "dashboard", "--port", "8800"])
    monkeypatch.delenv(dashboard_module._RESTART_ENV, raising=False)

    result = CliRunner().invoke(cli, ["dashboard", "--port", "8800"])

    assert isinstance(result.exception, RestartExec)
    assert closed == ["target", "exec"]
    assert signal_calls == [
        (signal.SIGHUP, dashboard_module._handle_sighup),
        (signal.SIGHUP, signal.SIG_IGN),
    ]
    assert exec_calls == [
        (sys.executable, [sys.executable, "/test/bin/lattice", "dashboard", "--port", "8800"])
    ]


def test_dashboard_start_installs_the_normal_sighup_handler(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    signal_calls: list[tuple[int, object]] = []
    monkeypatch.setattr(dashboard_module, "require_root", lambda _json: tmp_path / ".lattice")
    monkeypatch.setattr(dashboard_module, "_dashboard_target", lambda *_args: None)
    monkeypatch.setattr(dashboard_module, "_serve", lambda *_args: False)
    monkeypatch.setattr(
        dashboard_module.signal,
        "signal",
        lambda signum, handler: signal_calls.append((signum, handler)),
    )

    result = CliRunner().invoke(cli, ["dashboard", "--port", "8800"])

    assert result.exit_code == 0, result.output
    assert signal_calls == [(signal.SIGHUP, dashboard_module._handle_sighup)]


def test_restart_aborts_when_inflight_requests_miss_the_drain_deadline(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    class TimedOutServer:
        server_address = ("127.0.0.1", 8800)

        def serve_forever(self) -> None:
            dashboard_module._restart_requested = True

        def wait_for_inflight_requests(self, timeout: float) -> int:
            assert timeout == 0.01
            return 2

        def server_close(self) -> None:
            pass

    monkeypatch.setattr(
        dashboard_server_module, "create_server", lambda *_a, **_kw: TimedOutServer()
    )
    monkeypatch.setattr(dashboard_module, "_RESTART_DRAIN_TIMEOUT_SECONDS", 0.01)
    monkeypatch.setenv(dashboard_module._RESTART_ENV, "1")
    dashboard_module._restart_requested = False

    with pytest.raises(SystemExit) as exc:
        dashboard_module._serve(tmp_path / ".lattice", "127.0.0.1", 8800, False, False, None)

    assert exc.value.code == 1
    assert "2 request(s) are still in flight" in capsys.readouterr().err


def test_parsed_request_is_registered_before_stdlib_parse_returns(
    initialized_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A drain cannot close a socket in the post-parse registration gap."""
    parse_returned = threading.Event()
    release_parse = threading.Event()
    response_result: dict[str, object] = {}
    original_parse_request = BaseHTTPRequestHandler.parse_request

    def pause_after_stdlib_parse(handler) -> bool:  # noqa: ANN001
        parsed = original_parse_request(handler)
        parse_returned.set()
        if not release_parse.wait(timeout=5):
            raise TimeoutError("test did not release the parsed request")
        return parsed

    monkeypatch.setattr(BaseHTTPRequestHandler, "parse_request", pause_after_stdlib_parse)
    server = dashboard_server_module.create_server(initialized_root / ".lattice", "127.0.0.1", 0)
    port = server.server_address[1]
    serving = threading.Thread(target=server.serve_forever, daemon=True)
    serving.start()

    def get_boot() -> None:
        connection = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
        try:
            connection.request("GET", "/api/boot")
            response = connection.getresponse()
            response_result["status"] = response.status
            response_result["body"] = json.loads(response.read())
        except BaseException as exc:
            response_result["error"] = exc
        finally:
            connection.close()

    request = threading.Thread(target=get_boot, daemon=True)
    request.start()
    try:
        assert parse_returned.wait(timeout=3), "stdlib parser did not reach the barrier"
        remaining = server.wait_for_inflight_requests(timeout=0.05)
        release_parse.set()
        request.join(timeout=3)
        assert not request.is_alive(), "parsed request did not finish"
        assert remaining == 1
        assert response_result.get("status") == 200, response_result
    finally:
        release_parse.set()
        server.shutdown()
        server.server_close()
        serving.join(timeout=3)


def test_accepted_request_is_tracked_before_handler_thread_starts(
    initialized_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The drain sees a connection accepted before its handler thread starts."""
    accepted_before_thread = threading.Event()
    release_dispatch = threading.Event()
    drain_waiting = threading.Event()
    response_result: dict[str, object] = {}
    drain_result: dict[str, int] = {}
    server = dashboard_server_module.create_server(initialized_root / ".lattice", "127.0.0.1", 0)
    port = server.server_address[1]
    serving = threading.Thread(target=server.serve_forever, daemon=True)
    serving.start()
    original_dispatch = ThreadingMixIn.process_request

    def pause_before_handler_thread(instance, request, client_address) -> None:  # noqa: ANN001
        if instance is server:
            accepted_before_thread.set()
            if not release_dispatch.wait(timeout=5):
                raise TimeoutError("test did not release the accepted request")
        original_dispatch(instance, request, client_address)

    monkeypatch.setattr(ThreadingMixIn, "process_request", pause_before_handler_thread)

    def get_boot() -> None:
        connection = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
        try:
            connection.request("GET", "/api/boot")
            response = connection.getresponse()
            response_result["status"] = response.status
            response_result["body"] = json.loads(response.read())
        except BaseException as exc:
            response_result["error"] = exc
        finally:
            connection.close()

    request = threading.Thread(target=get_boot, daemon=True)
    request.start()
    drain = None
    try:
        assert accepted_before_thread.wait(timeout=3), "request was not accepted"
        with server._request_condition:
            assert len(server._pending_connections) == 1

        original_condition_wait = server._request_condition.wait

        def report_wait(timeout: float | None = None) -> bool:
            drain_waiting.set()
            return original_condition_wait(timeout)

        monkeypatch.setattr(server._request_condition, "wait", report_wait)
        drain = threading.Thread(
            target=lambda: drain_result.setdefault(
                "remaining", server.wait_for_inflight_requests(timeout=5)
            ),
            daemon=True,
        )
        drain.start()
        assert server._restart_drain_started.wait(timeout=3)
        assert drain_waiting.wait(timeout=3), "drain did not wait for the accepted request"

        release_dispatch.set()
        request.join(timeout=3)
        drain.join(timeout=3)
        assert not request.is_alive(), "accepted request did not finish"
        assert drain is not None and not drain.is_alive(), "drain did not finish"
        assert response_result.get("status") == 200, response_result
        assert drain_result == {"remaining": 0}
    finally:
        release_dispatch.set()
        if request.is_alive():
            request.join(timeout=1)
        if drain is not None and drain.is_alive():
            drain.join(timeout=1)
        server.shutdown()
        server.server_close()
        serving.join(timeout=3)


def test_second_sighup_is_ignored_during_the_exec_window(tmp_path: Path) -> None:
    """A real second SIGHUP is ignored after the old handler resets and before startup."""
    code = """
from pathlib import Path
import os
import signal
import sys
from click.testing import CliRunner
from lattice.cli import dashboard_cmd as module
from lattice.cli.main import cli
module.require_root = lambda _json: Path(sys.argv[1]) / '.lattice'
module._dashboard_target = lambda *_args: None
module._serve = lambda *_args: True
module.shutil.which = lambda _script: '/test/bin/lattice'
def fake_execv(_executable, _args):
    if signal.getsignal(signal.SIGHUP) is not signal.SIG_IGN:
        raise AssertionError('SIGHUP was not ignored before exec')
    os.kill(os.getpid(), signal.SIGHUP)
    print('second SIGHUP ignored', flush=True)
    raise OSError('simulated exec failure')
module.os.execv = fake_execv
result = CliRunner().invoke(cli, ['dashboard', '--port', '8801'])
print(result.output, end='')
raise SystemExit(result.exit_code)
"""
    result = subprocess.run(
        [sys.executable, "-c", code, str(tmp_path)],
        capture_output=True,
        text=True,
        timeout=10,
    )

    assert result.returncode == 1, result.stdout + result.stderr
    assert "second SIGHUP ignored" in result.stdout
    assert "Error: could not restart dashboard process: simulated exec failure" in result.stdout
    assert "Traceback" not in result.stdout + result.stderr


def test_exec_failure_is_reported_and_exits_nonzero(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    signal_calls: list[tuple[int, object]] = []

    def fail_exec(_executable: str, _args: list[str]) -> None:
        raise PermissionError("permission denied")

    monkeypatch.setattr(dashboard_module, "require_root", lambda _json: tmp_path / ".lattice")
    monkeypatch.setattr(dashboard_module, "_dashboard_target", lambda *_args: None)
    monkeypatch.setattr(dashboard_module, "_serve", lambda *_args: True)
    monkeypatch.setattr(dashboard_module.signal, "signal", lambda *args: signal_calls.append(args))
    monkeypatch.setattr(dashboard_module.shutil, "which", lambda _script: "/test/bin/lattice")
    monkeypatch.setattr(dashboard_module.os, "execv", fail_exec)
    monkeypatch.setattr(dashboard_module.sys, "argv", ["lattice", "dashboard", "--port", "8802"])

    result = CliRunner().invoke(cli, ["dashboard", "--port", "8802"])

    assert result.exit_code == 1
    assert "Error: could not restart dashboard process: permission denied" in result.stderr
    assert "Traceback" not in result.output
    assert signal_calls[-1] == (signal.SIGHUP, signal.SIG_IGN)


def _stub_lsof(
    monkeypatch: pytest.MonkeyPatch, port: int, responses: list[str]
) -> list[list[str]]:
    calls: list[list[str]] = []

    def run(args: list[str], **_kwargs) -> SimpleNamespace:
        calls.append(args)
        assert args == ["lsof", "-ti", f"tcp:{port}", "-sTCP:LISTEN"]
        response = responses.pop(0) if responses else ""
        return SimpleNamespace(stdout=response)

    monkeypatch.setattr(dashboard_module.subprocess, "run", run)
    monkeypatch.setattr(dashboard_module, "_listening_bind_addresses", lambda _port: ["127.0.0.1"])
    return calls


def _fake_clock(monkeypatch: pytest.MonkeyPatch) -> None:
    now = [0.0]
    monkeypatch.setattr(dashboard_module.time, "monotonic", lambda: now[0])
    monkeypatch.setattr(
        dashboard_module.time,
        "sleep",
        lambda delay: now.__setitem__(0, now[0] + delay),
    )


def test_restart_reports_success_only_after_boot_id_changes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    port = 8803
    calls = _stub_lsof(monkeypatch, port, ["1234\n"])
    _fake_clock(monkeypatch)
    signalled: list[tuple[int, int]] = []
    boot_ids = iter(["boot-before", "boot-before", "boot-after"])
    monkeypatch.setattr(dashboard_module.os, "kill", lambda pid, sig: signalled.append((pid, sig)))
    monkeypatch.setattr(
        dashboard_module,
        "_read_dashboard_boot_id",
        lambda _port, **_kwargs: next(boot_ids),
    )

    result = CliRunner().invoke(cli, ["restart", "--port", str(port)])

    assert result.exit_code == 0, result.output
    assert result.stdout == f"Dashboard restarted and is listening on port {port}.\n"
    assert result.stderr == ""
    assert signalled == [(1234, signal.SIGHUP)]
    assert len(calls) == 1


def test_restart_reports_failure_when_boot_id_does_not_change(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    port = 8804
    _stub_lsof(monkeypatch, port, ["1234\n"])
    _fake_clock(monkeypatch)
    monkeypatch.setattr(dashboard_module, "_RESTART_TIMEOUT_SECONDS", 0.1)
    monkeypatch.setattr(dashboard_module.os, "kill", lambda *_args: None)
    monkeypatch.setattr(
        dashboard_module, "_read_dashboard_boot_id", lambda _port, **_kwargs: "boot-same"
    )

    result = CliRunner().invoke(cli, ["restart", "--port", str(port)])

    assert result.exit_code == 1
    assert (
        f"Error: dashboard on port {port} did not restart within 0.1 seconds "
        "(boot identity unchanged; a request may have exceeded the 10-second drain deadline)."
        in result.stderr
    )


def test_restart_falls_back_to_listener_cycle_for_legacy_dashboard(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    port = 8805
    _stub_lsof(monkeypatch, port, ["1234\n"])
    _fake_clock(monkeypatch)
    signalled: list[int] = []
    monkeypatch.setattr(dashboard_module.os, "kill", lambda pid, _sig: signalled.append(pid))
    monkeypatch.setattr(
        dashboard_module, "_read_dashboard_boot_id", lambda *_args, **_kwargs: None
    )
    monkeypatch.setattr(dashboard_module, "_dashboard_responds", lambda *_args, **_kwargs: True)
    listener_states = iter([True, False, True])
    monkeypatch.setattr(
        dashboard_module,
        "_tcp_listener_accepting",
        lambda *_args, **_kwargs: next(listener_states),
    )

    result = CliRunner().invoke(cli, ["restart", "--port", str(port)])

    assert result.exit_code == 0, result.output
    assert result.stdout == f"Dashboard restarted and is listening on port {port}.\n"
    assert signalled == [1234]


def test_restart_ignores_legacy_http_timeout_while_listener_stays_up(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    port = 8806
    _stub_lsof(monkeypatch, port, ["1234\n"])
    _fake_clock(monkeypatch)
    monkeypatch.setattr(dashboard_module, "_RESTART_TIMEOUT_SECONDS", 0.1)
    signalled: list[int] = []
    http_results = iter([True, False, True])
    http_calls: list[bool] = []
    tcp_calls: list[bool | None] = []

    def http_responds(*_args, **_kwargs):  # noqa: ANN002, ANN003
        result = next(http_results)
        http_calls.append(result)
        return result

    def listener_stays_up(*_args, **_kwargs):  # noqa: ANN002, ANN003
        tcp_calls.append(True)
        return True

    monkeypatch.setattr(dashboard_module.os, "kill", lambda pid, _sig: signalled.append(pid))
    monkeypatch.setattr(
        dashboard_module, "_read_dashboard_boot_id", lambda *_args, **_kwargs: None
    )
    monkeypatch.setattr(dashboard_module, "_dashboard_responds", http_responds)
    monkeypatch.setattr(dashboard_module, "_tcp_listener_accepting", listener_stays_up)

    result = CliRunner().invoke(cli, ["restart", "--port", str(port)])

    assert result.exit_code == 1
    assert "listener never completed a verified restart cycle" in result.stderr
    assert signalled == [1234]
    assert http_calls == [True]
    assert tcp_calls and all(tcp_calls)


def test_restart_uses_the_specific_non_loopback_listen_address(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    port = 8807
    _stub_lsof(monkeypatch, port, ["1234\n"])
    monkeypatch.setattr(dashboard_module, "_listening_bind_addresses", lambda _port: ["192.0.2.7"])
    _fake_clock(monkeypatch)
    hosts: list[str] = []
    boot_ids = iter(["before", "after"])

    def read_boot_id(_port: int, *, host: str, **_kwargs: object) -> str:
        hosts.append(host)
        return next(boot_ids)

    monkeypatch.setattr(dashboard_module, "_read_dashboard_boot_id", read_boot_id)
    monkeypatch.setattr(dashboard_module.os, "kill", lambda *_args: None)

    result = CliRunner().invoke(cli, ["restart", "--port", str(port)])

    assert result.exit_code == 0, result.output
    assert hosts == ["192.0.2.7", "192.0.2.7"]


def test_listening_bind_addresses_parse_numeric_lsof_names(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    port = 8808

    def run(args: list[str], **_kwargs: object) -> SimpleNamespace:
        assert args == ["lsof", "-nP", "-Fpn", f"-iTCP:{port}", "-sTCP:LISTEN"]
        return SimpleNamespace(
            stdout=f"p1234\nn192.0.2.7:{port}\np2345\nn[2001:db8::4]:{port}\np3456\nn*:{port}\n"
        )

    monkeypatch.setattr(dashboard_module.subprocess, "run", run)

    assert dashboard_module._listening_bind_addresses(port) == [
        "192.0.2.7",
        "2001:db8::4",
        "*",
    ]


def test_wildcard_listen_addresses_probe_loopback() -> None:
    assert dashboard_module._probe_hosts(["*"]) == ["127.0.0.1", "::1"]
    assert dashboard_module._probe_hosts(["0.0.0.0", "::", "*"]) == ["127.0.0.1", "::1"]


def test_connected_client_survives_restart_and_only_listener_is_signalled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen()
    listener.settimeout(5)
    port = listener.getsockname()[1]

    def echo_client() -> None:
        try:
            connection, _address = listener.accept()
            with connection:
                while data := connection.recv(1024):
                    connection.sendall(data)
        except OSError:
            pass

    threading.Thread(target=echo_client, daemon=True).start()
    client_code = """
import socket, sys
s = socket.create_connection(('127.0.0.1', int(sys.argv[1])))
print('connected', flush=True)
for line in sys.stdin:
    s.sendall(line.encode())
    print(s.recv(1024).decode(), end='', flush=True)
"""
    client = subprocess.Popen(
        [sys.executable, "-c", client_code, str(port)],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        assert client.stdout is not None and client.stdin is not None
        assert client.stdout.readline().strip() == "connected"
        signalled: list[tuple[int, int]] = []
        boot_ids = iter(["boot-before", "boot-after"])
        with monkeypatch.context() as restart_patch:
            _stub_lsof(restart_patch, port, [f"{os.getpid()}\n"])
            restart_patch.setattr(
                dashboard_module.os,
                "kill",
                lambda pid, sig: signalled.append((pid, sig)),
            )
            restart_patch.setattr(
                dashboard_module,
                "_read_dashboard_boot_id",
                lambda *_args, **_kwargs: next(boot_ids),
            )
            result = CliRunner().invoke(cli, ["restart", "--port", str(port)])

        assert result.exit_code == 0, result.output
        assert signalled == [(os.getpid(), signal.SIGHUP)]
        assert client.poll() is None
        client.stdin.write("still-connected\n")
        client.stdin.flush()
        assert client.stdout.readline().strip() == "still-connected"
        assert client.poll() is None
    finally:
        client.terminate()
        client.wait(timeout=2)
        listener.close()


@pytest.mark.skipif(shutil.which("lsof") is None, reason="lsof is required for the live restart")
def test_live_restart_changes_boot_id_on_the_same_port(
    initialized_root: Path,
) -> None:
    """A restart reports success only after a new dashboard boot ID answers."""
    script = Path(sys.executable).with_name("lattice")
    if not script.exists():
        pytest.skip("the active virtualenv has no lattice console script")
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]

    env = {**os.environ, "LATTICE_ROOT": str(initialized_root)}
    dashboard = subprocess.Popen(
        [str(script), "dashboard", "--port", str(port), "--json"],
        cwd=initialized_root,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    pid_before = dashboard.pid
    try:
        _wait_for_dashboard_http(port, dashboard)
        boot_before = dashboard_module._read_dashboard_boot_id(port)
        assert boot_before
        restarted = subprocess.run(
            [str(script), "restart", "--port", str(port)],
            cwd=initialized_root,
            env=env,
            capture_output=True,
            text=True,
            timeout=10,
        )
        assert restarted.returncode == 0, restarted.stdout + restarted.stderr
        assert "Dashboard restarted and is listening" in restarted.stdout

        _wait_for_dashboard_http(port, dashboard)
        boot_after = dashboard_module._read_dashboard_boot_id(port)
        assert boot_after
        assert boot_after != boot_before
        assert dashboard.pid == pid_before  # execv replaces the image without forking.
        assert _read_restart_banner(dashboard.stderr)
    finally:
        dashboard.terminate()
        dashboard.wait(timeout=3)


@pytest.mark.skipif(
    shutil.which("lsof") is None or shutil.which("nc") is None,
    reason="lsof and nc are required for the idle-client restart regression",
)
def test_live_restart_is_not_blocked_by_an_idle_nc_client(initialized_root: Path) -> None:
    """An accepted client that sends no request cannot hold SIGHUP shutdown."""
    script = Path(sys.executable).with_name("lattice")
    if not script.exists():
        pytest.skip("the active virtualenv has no lattice console script")
    nc = shutil.which("nc")
    assert nc is not None
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]

    env = {**os.environ, "LATTICE_ROOT": str(initialized_root)}
    dashboard = subprocess.Popen(
        [str(script), "dashboard", "--port", str(port), "--json"],
        cwd=initialized_root,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    client = None
    try:
        _wait_for_dashboard_http(port, dashboard)
        boot_before = dashboard_module._read_dashboard_boot_id(port)
        assert boot_before
        client = subprocess.Popen(
            [nc, "127.0.0.1", str(port)],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        _wait_for_process_connection(port, client)

        restarted = subprocess.run(
            [str(script), "restart", "--port", str(port)],
            cwd=initialized_root,
            env=env,
            capture_output=True,
            text=True,
            timeout=10,
        )

        assert restarted.returncode == 0, restarted.stdout + restarted.stderr
        boot_after = dashboard_module._read_dashboard_boot_id(port)
        assert boot_after and boot_after != boot_before
    finally:
        if client is not None:
            client.terminate()
            client.wait(timeout=2)
        dashboard.terminate()
        dashboard.wait(timeout=3)


def test_live_restart_drains_lock_blocked_comment_across_real_exec(initialized_root: Path) -> None:
    """The held comment returns and persists across the dashboard's real exec."""
    script = Path(sys.executable).with_name("lattice")
    if not script.exists():
        pytest.skip("the active virtualenv has no lattice console script")
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]

    env = {**os.environ, "LATTICE_ROOT": str(initialized_root)}
    created = subprocess.run(
        [str(script), "create", "Real restart drain task", "--actor", "human:test", "--quiet"],
        cwd=initialized_root,
        env=env,
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert created.returncode == 0, created.stdout + created.stderr
    task_id = created.stdout.strip()
    lock_path = initialized_root / ".lattice" / "locks" / f"events_{task_id}.lock"
    lock_code = """
import sys
from filelock import FileLock
lock = FileLock(sys.argv[1])
lock.acquire()
print('locked', flush=True)
sys.stdin.readline()
lock.release()
"""
    lock_holder = subprocess.Popen(
        [sys.executable, "-c", lock_code, str(lock_path)],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    wrapper = r"""
import contextlib
import sys
from lattice.cli.main import cli
from lattice.dashboard.server import _RestartAwareHTTPServer
from lattice.storage import locks

script, task_id, port = sys.argv[1:]
original_multi_lock = locks.multi_lock
@contextlib.contextmanager
def report_task_lock(locks_dir, keys, timeout=10):
    if f'events_{task_id}' in keys:
        print('LOCK_ATTEMPT', flush=True)
    with original_multi_lock(locks_dir, keys, timeout):
        yield
locks.multi_lock = report_task_lock

original_wait = _RestartAwareHTTPServer.wait_for_inflight_requests
def report_drain_started(self, timeout):
    self._restart_draining.set()
    self._restart_drain_started.set()
    print('DRAIN_STARTED', flush=True)
    return original_wait(self, timeout)
_RestartAwareHTTPServer.wait_for_inflight_requests = report_drain_started
sys.argv = [script, 'dashboard', '--port', port, '--json']
cli()
"""
    dashboard = subprocess.Popen(
        [sys.executable, "-c", wrapper, str(script), task_id, str(port)],
        cwd=initialized_root,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        bufsize=1,
    )
    output_lines: queue.Queue[tuple[str, str]] = queue.Queue()

    def capture_output(name: str, stream) -> None:  # noqa: ANN001
        for line in iter(stream.readline, ""):
            output_lines.put((name, line.rstrip()))

    assert dashboard.stdout is not None and dashboard.stderr is not None
    readers = [
        threading.Thread(target=capture_output, args=("stdout", dashboard.stdout), daemon=True),
        threading.Thread(target=capture_output, args=("stderr", dashboard.stderr), daemon=True),
    ]
    for reader in readers:
        reader.start()

    def wait_for_line(predicate, timeout: float = 5.0) -> str:  # noqa: ANN001
        deadline = time.monotonic() + timeout
        observed: list[tuple[str, str]] = []
        while time.monotonic() < deadline:
            try:
                item = output_lines.get(timeout=max(deadline - time.monotonic(), 0.001))
            except queue.Empty:
                break
            observed.append(item)
            if predicate(*item):
                return item[1]
            if dashboard.poll() is not None:
                break
        raise AssertionError(f"dashboard output did not reach expected line: {observed}")

    post_result: dict[str, object] = {}
    post_thread = None
    try:
        assert lock_holder.stdout is not None
        assert lock_holder.stdout.readline().strip() == "locked"
        _wait_for_dashboard_http(port, dashboard)
        boot_before = dashboard_module._read_dashboard_boot_id(port)
        assert boot_before

        def post_comment() -> None:
            connection = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
            try:
                payload = json.dumps(
                    {"body": "persists across real restart", "actor": "human:test"}
                )
                connection.request(
                    "POST",
                    f"/api/tasks/{task_id}/comment",
                    body=payload,
                    headers={
                        "Content-Type": "application/json",
                        "Origin": f"http://127.0.0.1:{port}",
                    },
                )
                response = connection.getresponse()
                post_result["status"] = response.status
                post_result["body"] = json.loads(response.read())
            except BaseException as exc:
                post_result["error"] = exc
            finally:
                connection.close()

        post_thread = threading.Thread(target=post_comment, daemon=True)
        post_thread.start()
        wait_for_line(lambda _name, line: "LOCK_ATTEMPT" in line)
        os.kill(dashboard.pid, signal.SIGHUP)
        wait_for_line(lambda name, line: name == "stderr" and line == "Restarting dashboard...")
        wait_for_line(lambda name, line: name == "stdout" and "DRAIN_STARTED" in line)

        assert lock_holder.stdin is not None
        lock_holder.stdin.write("release\n")
        lock_holder.stdin.flush()
        lock_holder.wait(timeout=3)
        assert post_thread is not None
        post_thread.join(timeout=5)
        assert not post_thread.is_alive(), "comment response did not finish during restart drain"
        assert post_result.get("status") == 200, post_result
        assert dashboard_module._wait_for_dashboard_restart(port, boot_before, host="127.0.0.1")
        assert dashboard.poll() is None
        assert any(
            comment["body"] == "persists across real restart"
            for comment in get_task_comments(initialized_root / ".lattice", task_id)
        )
        wait_for_line(
            lambda name, line: name == "stderr" and "Lattice dashboard restarted:" in line
        )
    finally:
        if lock_holder.poll() is None:
            if lock_holder.stdin is not None:
                lock_holder.stdin.write("release\n")
                lock_holder.stdin.flush()
            try:
                lock_holder.wait(timeout=3)
            except subprocess.TimeoutExpired:
                lock_holder.terminate()
                lock_holder.wait(timeout=3)
        if post_thread is not None and post_thread.is_alive():
            post_thread.join(timeout=1)
        dashboard.terminate()
        dashboard.wait(timeout=3)
        for reader in readers:
            reader.join(timeout=1)


def _wait_for_process_connection(port: int, client: subprocess.Popen) -> None:
    deadline = time.monotonic() + 3
    while time.monotonic() < deadline:
        result = subprocess.run(
            ["lsof", "-ti", f"tcp:{port}"], capture_output=True, text=True, check=False
        )
        if str(client.pid) in result.stdout.splitlines():
            return
        if client.poll() is not None:
            raise AssertionError(f"nc exited before connecting: {client.returncode}")
        time.sleep(0.02)
    raise AssertionError("nc did not establish its idle dashboard connection")


def _wait_for_dashboard_http(port: int, dashboard: subprocess.Popen[str]) -> None:
    import http.client
    import time

    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        if dashboard.poll() is not None:
            stderr = dashboard.stderr.read() if dashboard.stderr else ""
            raise AssertionError(f"dashboard exited with {dashboard.returncode}: {stderr}")
        try:
            connection = http.client.HTTPConnection("127.0.0.1", port, timeout=0.2)
            connection.request("GET", "/")
            response = connection.getresponse()
            response.read()
            connection.close()
            if response.status == 200:
                return
        except OSError:
            pass
        time.sleep(0.02)
    raise AssertionError(f"dashboard did not serve HTTP on port {port}")


def _read_restart_banner(stream) -> bool:
    import select
    import time

    if stream is None:
        return False
    # The old image writes "Restarting dashboard..." immediately before execv;
    # consume both that line and the new image's startup banner from the pipe.
    deadline = time.monotonic() + 2
    output = ""
    while time.monotonic() < deadline:
        ready, _, _ = select.select([stream.fileno()], [], [], deadline - time.monotonic())
        if not ready:
            break
        output += os.read(stream.fileno(), 4096).decode()
        if "Lattice dashboard restarted:" in output:
            return True
    return False
