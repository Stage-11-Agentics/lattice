"""Dashboard restart re-execs cleanly and signals only its listening process."""

from __future__ import annotations

import os
import shutil
import signal
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest
from click.testing import CliRunner

from lattice.cli import dashboard_cmd as dashboard_module
from lattice.cli.main import cli


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
        "(boot identity unchanged)." in result.stderr
    )


def test_restart_does_not_signal_a_dashboard_without_boot_identity(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    port = 8805
    _stub_lsof(monkeypatch, port, ["1234\n"])
    signalled: list[int] = []
    monkeypatch.setattr(dashboard_module.os, "kill", lambda pid, _sig: signalled.append(pid))
    monkeypatch.setattr(
        dashboard_module, "_read_dashboard_boot_id", lambda *_args, **_kwargs: None
    )

    result = CliRunner().invoke(cli, ["restart", "--port", str(port)])

    assert result.exit_code == 1
    assert "did not provide a boot identity" in result.stderr
    assert signalled == []


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
