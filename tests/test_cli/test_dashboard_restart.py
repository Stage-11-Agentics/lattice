"""Restart targets one dashboard listener and starts a fresh process."""

from __future__ import annotations

import json
import os
import signal
import shutil
import socket
import subprocess
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import pytest
from click.testing import CliRunner

from lattice.cli import dashboard_cmd as dashboard_module
from lattice.cli.main import cli

_LATTICE = shutil.which("lattice")
assert _LATTICE is not None


def _make_board(root: Path) -> tuple[Path, str]:
    from lattice.core.config import default_config, serialize_config
    from lattice.storage.fs import atomic_write, ensure_lattice_dirs
    from lattice.storage.short_ids import save_id_index

    ensure_lattice_dirs(root)
    lattice_dir = root / ".lattice"
    config = dict(default_config())
    config["project_code"] = "LAT"
    config["auto_code_review_on_transition"] = False
    config["auto_plan_review_on_transition"] = False
    atomic_write(lattice_dir / "config.json", serialize_config(config))
    save_id_index(lattice_dir, {"schema_version": 2, "next_seqs": {}, "map": {}})

    result = CliRunner().invoke(
        cli,
        ["create", "Restart drain probe", "--actor", "human:test", "--json"],
        env={"LATTICE_ROOT": str(root)},
    )
    assert result.exit_code == 0, result.output
    return lattice_dir, json.loads(result.output)["data"]["id"]


def _available_port(port_range: range) -> int:
    for port in port_range:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
            try:
                probe.bind(("127.0.0.1", port))
            except OSError:
                continue
            return port
    pytest.skip(f"no free loopback test port in {port_range.start}-{port_range.stop - 1}")


def _start_dashboard(
    root: Path,
    cache_home: Path,
    host: str,
    port: int,
    *,
    output_json: bool = True,
    env: dict[str, str] | None = None,
):
    env = dict(env) if env is not None else os.environ.copy()
    env["LATTICE_ROOT"] = str(root)
    env["XDG_CACHE_HOME"] = str(cache_home)
    log_path = root / "dashboard-start.log"
    argv = [_LATTICE, "dashboard", "--host", host, "--port", str(port)]
    if output_json:
        argv.append("--json")
    with log_path.open("ab") as log:
        process = subprocess.Popen(
            argv,
            cwd=root,
            env=env,
            stdin=subprocess.DEVNULL,
            stdout=log,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )

    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        if process.poll() is not None:
            pytest.fail(
                f"dashboard exited with status {process.returncode}: "
                f"{log_path.read_text(errors='replace')}"
            )
        boot = dashboard_module._probe_dashboard(host, port)
        if boot is not None:
            assert boot["pid"] == process.pid
            return process, boot
        time.sleep(0.05)
    pytest.fail(f"dashboard did not become healthy: {log_path.read_text(errors='replace')}")


def _restart_argv(port: int) -> list[str]:
    return [_LATTICE, "restart", "--port", str(port)]


def _test_env(root: Path, cache_home: Path) -> dict[str, str]:
    env = os.environ.copy()
    env["LATTICE_ROOT"] = str(root)
    env["XDG_CACHE_HOME"] = str(cache_home)
    return env


def _test_env_with_server_hooks(
    root: Path,
    cache_home: Path,
    *,
    drain_timeout: float | None = None,
    admitted_marker: Path | None = None,
    draining_marker: Path | None = None,
) -> dict[str, str]:
    """Set subprocess-only server instrumentation without changing production code."""
    hook_dir = cache_home / "python-hooks"
    hook_dir.mkdir(parents=True, exist_ok=True)
    hook_lines = [
        "import os",
        "from pathlib import Path",
        "from lattice.dashboard import server",
    ]
    if drain_timeout is not None:
        hook_lines.append(
            'server.WRITE_DRAIN_TIMEOUT = float(os.environ["RESTART_TEST_DRAIN_TIMEOUT"])'
        )
    if admitted_marker is not None:
        hook_lines.extend(
            [
                "_original_admit_write = server._RestartAwareHTTPServer.admit_write",
                "def _record_admission(self, request):",
                "    admitted = _original_admit_write(self, request)",
                '    marker = os.environ.get("RESTART_TEST_ADMITTED_MARKER")',
                "    if admitted and marker:",
                "        Path(marker).touch()",
                "    return admitted",
                "server._RestartAwareHTTPServer.admit_write = _record_admission",
            ]
        )
    if draining_marker is not None:
        hook_lines.extend(
            [
                "_original_begin_write_drain = server._RestartAwareHTTPServer.begin_write_drain",
                "def _record_drain(self, *args, **kwargs):",
                "    with self._restart_condition:",
                "        self._draining = True",
                "        self._restart_condition.notify_all()",
                '    marker = os.environ.get("RESTART_TEST_DRAINING_MARKER")',
                "    if marker:",
                "        Path(marker).touch()",
                "    return _original_begin_write_drain(self, *args, **kwargs)",
                "server._RestartAwareHTTPServer.begin_write_drain = _record_drain",
            ]
        )
    (hook_dir / "sitecustomize.py").write_text("\n".join(hook_lines) + "\n", encoding="utf-8")

    env = _test_env(root, cache_home)
    env["PYTHONPATH"] = os.pathsep.join(
        part for part in (str(hook_dir), env.get("PYTHONPATH")) if part
    )
    if drain_timeout is not None:
        env["RESTART_TEST_DRAIN_TIMEOUT"] = str(drain_timeout)
    if admitted_marker is not None:
        env["RESTART_TEST_ADMITTED_MARKER"] = str(admitted_marker)
    if draining_marker is not None:
        env["RESTART_TEST_DRAINING_MARKER"] = str(draining_marker)
    return env


def _wait_for_path(path: Path, timeout: float = 5) -> None:
    deadline = time.monotonic() + timeout
    while not path.exists() and time.monotonic() < deadline:
        time.sleep(0.02)
    assert path.exists(), f"timed out waiting for {path}"


def _stop_owned_listener(port: int, owned_pids: set[int]) -> None:
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        listeners = dashboard_module._listening_processes(port)
        for pid in listeners.keys() & owned_pids:
            try:
                os.kill(pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
        if not (listeners.keys() & owned_pids):
            return
        time.sleep(0.05)

    listeners = dashboard_module._listening_processes(port)
    for pid in listeners.keys() & owned_pids:
        try:
            os.kill(pid, signal.SIGKILL)
        except ProcessLookupError:
            pass


def test_launch_record_preserves_resolved_context_without_environment_snapshot(
    tmp_path, monkeypatch
):
    root = tmp_path / "board"
    root.mkdir()
    (root / ".lattice").mkdir()
    monkeypatch.setenv("LATTICE_ROOT", str(root))
    monkeypatch.setenv("LATTICE_SECRET_NAME", "not-recorded-secret-value")
    monkeypatch.setattr(
        dashboard_module.sys,
        "orig_argv",
        [sys.executable, "/venv/bin/lattice", "dashboard", "--json"],
        raising=False,
    )
    server = SimpleNamespace(boot_id="boot-old", pid=100)
    expected_env_names = sorted(
        name
        for name in os.environ
        if name.startswith("LATTICE_") and name != dashboard_module._RESTARTED_ENV
    )

    record = dashboard_module._make_launch_record(
        root / ".lattice", "0.0.0.0", 8860, True, True, server
    )

    assert record == {
        "schema": 1,
        "pid": 100,
        "boot_id": "boot-old",
        "board_root": str(root.resolve()),
        "argv": [
            sys.executable,
            "/venv/bin/lattice",
            "dashboard",
            "--host",
            "0.0.0.0",
            "--port",
            "8860",
            "--json",
        ],
        "cwd": str(Path.cwd().resolve()),
        "host": "0.0.0.0",
        "port": 8860,
        "json": True,
        "readonly": True,
        "root_override": str(root.resolve()),
        "lattice_env_names": expected_env_names,
        "started_ns": record["started_ns"],
    }
    assert not any("SECRET" in key or "environment" in key for key in record)
    assert "not-recorded-secret-value" not in json.dumps(record)
    dashboard_module._stop_requested = False
    dashboard_module._handle_sighup(signal.SIGHUP, None)
    assert dashboard_module._stop_requested is False


def test_restart_identity_probe_retries_transient_failure(monkeypatch):
    probes = iter((None, {"pid": 100, "boot_id": "boot-old"}))
    monkeypatch.setattr(dashboard_module, "_probe_dashboard", lambda *_args: next(probes))

    assert dashboard_module._wait_for_dashboard_identity("127.0.0.1", 8860, 100, "boot-old")


def test_completed_restart_is_not_reused_if_it_spawned_before_this_request(tmp_path, monkeypatch):
    runtime = tmp_path / "runtime"
    monkeypatch.setattr(dashboard_module, "_runtime_dir", lambda: runtime)
    dashboard_module._atomic_json(
        dashboard_module._restart_state_path(8860),
        {
            "status": "complete",
            "spawned_ns": 99,
            "completed_ns": 101,
            "new_pid": 200,
            "new_boot_id": "new-boot",
            "host": "127.0.0.1",
        },
    )
    monkeypatch.setattr(
        dashboard_module, "_listening_processes", lambda _port: {200: ["127.0.0.1:8860"]}
    )
    monkeypatch.setattr(
        dashboard_module, "_probe_dashboard", lambda *_args: {"pid": 200, "boot_id": "new-boot"}
    )

    assert not dashboard_module._reuse_completed_restart(8860, requested_ns=100)


def test_restart_log_pruning_keeps_the_newest_logs_per_port(tmp_path):
    old_logs = []
    for index in range(8):
        path = tmp_path / f"restart-8860-{index}.log"
        path.write_text(str(index), encoding="utf-8")
        timestamp = 1_000_000_000 + index
        os.utime(path, ns=(timestamp, timestamp))
        old_logs.append(path)
    other_port = tmp_path / "restart-8861-other.log"
    other_port.write_text("other", encoding="utf-8")

    dashboard_module._prune_restart_logs(tmp_path, 8860, keep=3)

    assert {path.name for path in tmp_path.glob("restart-8860-*.log")} == {
        path.name for path in old_logs[-3:]
    }
    assert other_port.exists()


def test_restart_preflights_bound_token_env_before_signalling(tmp_path, monkeypatch):
    root = tmp_path / "bound"
    (root / ".lattice").mkdir(parents=True)
    (root / ".lattice-remote.json").write_text(
        json.dumps({"remote": "team", "project": "demo"}), encoding="utf-8"
    )
    config_dir = tmp_path / "config" / "lattice"
    config_dir.mkdir(parents=True)
    remotes_path = config_dir / "remotes.json"
    remotes_path.write_text(
        json.dumps(
            {
                "remotes": {
                    "team": {
                        "url": "http://127.0.0.1:8740",
                        "token": {"env": "LATTICE_RESTART_TEST_TOKEN"},
                    }
                }
            }
        ),
        encoding="utf-8",
    )
    remotes_path.chmod(0o600)
    runtime = tmp_path / "runtime"
    cwd = tmp_path / "work"
    cwd.mkdir()
    monkeypatch.setattr(dashboard_module, "_runtime_dir", lambda: runtime)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.delenv("LATTICE_RESTART_TEST_TOKEN", raising=False)
    record = {
        "schema": 1,
        "pid": 5454,
        "boot_id": "bound-old-boot",
        "board_root": str(root),
        "argv": ["lattice", "dashboard", "--port", "8864"],
        "cwd": str(cwd),
        "host": "127.0.0.1",
        "port": 8864,
        "json": False,
        "readonly": False,
        "root_override": str(root),
        "lattice_env_names": ["LATTICE_RESTART_TEST_TOKEN"],
    }
    dashboard_module._atomic_json(dashboard_module._launch_record_path(5454), record)
    signalled = []
    old_boot = {"pid": 5454, "boot_id": "bound-old-boot"}
    monkeypatch.setattr(
        dashboard_module,
        "_listening_processes",
        lambda _port: {5454: ["127.0.0.1:8864 (LISTEN)"]},
    )
    monkeypatch.setattr(dashboard_module, "_probe_dashboard", lambda *_args: old_boot)
    monkeypatch.setattr(dashboard_module.os, "kill", lambda *args: signalled.append(args))

    result = CliRunner().invoke(cli, ["restart", "--port", "8864"])

    assert result.exit_code == 1
    assert "TOKEN_ENV_UNSET" in result.output
    assert "LATTICE_RESTART_TEST_TOKEN" in result.output
    assert "remains available on port 8864" in result.output
    assert "LATTICE_RESTART_TEST_TOKEN" in result.output  # warning names; no value is recorded
    assert signalled == []
    assert dashboard_module._probe_dashboard("127.0.0.1", 8864) == old_boot
    assert dashboard_module._launch_record_path(5454).exists()


def test_plain_sigterm_stops_when_write_drain_aborts(monkeypatch, tmp_path):
    from lattice.dashboard import server as dashboard_server

    class FakeDashboardServer:
        pid = 5353
        boot_id = "plain-stop"
        timeout = None
        closed = False
        requests = 0

        def handle_request(self):
            self.requests += 1
            if self.requests > 1:
                raise AssertionError("plain SIGTERM resumed serving after the drain timed out")
            dashboard_module._handle_sigterm(signal.SIGTERM, None)

        def begin_write_drain(self, timeout):  # noqa: ANN001
            assert timeout == dashboard_server.WRITE_DRAIN_TIMEOUT
            return False, 1

        def server_close(self):
            self.closed = True

    fake_server = FakeDashboardServer()
    monkeypatch.setattr(dashboard_module, "_runtime_dir", lambda: tmp_path / "runtime")
    monkeypatch.setattr(dashboard_server, "create_server", lambda *_args, **_kwargs: fake_server)
    monkeypatch.setattr(
        dashboard_module.sys,
        "orig_argv",
        [sys.executable, "/venv/bin/lattice", "dashboard"],
        raising=False,
    )
    monkeypatch.setattr(dashboard_module, "_stop_requested", False)
    monkeypatch.setattr(dashboard_module, "_stop_signal_count", 0)

    dashboard_module._serve(tmp_path / ".lattice", "127.0.0.1", 8860, False, True, None)

    assert fake_server.closed
    assert fake_server.requests == 1
    assert not dashboard_module._restart_result_path(fake_server.pid).exists()


def test_legacy_restart_refuses_without_signalling_and_gives_manual_command(tmp_path, monkeypatch):
    root = tmp_path / "board"
    (root / ".lattice").mkdir(parents=True)
    runtime = tmp_path / "runtime"
    signalled = []
    monkeypatch.setattr(dashboard_module, "_runtime_dir", lambda: runtime)
    monkeypatch.setattr(
        dashboard_module,
        "_listening_processes",
        lambda port: {4242: ["0.0.0.0:8861 (LISTEN)"]},
    )
    monkeypatch.setattr(dashboard_module, "_legacy_board_root", lambda _pid: (root, None))
    monkeypatch.setattr(dashboard_module.os, "kill", lambda *args: signalled.append(args))

    result = CliRunner().invoke(cli, ["restart", "--port", "8861"])

    assert result.exit_code == 1
    assert "PID 4242" in result.output
    assert "port 8861" in result.output
    assert "0.0.0.0" in result.output
    assert str(root) in result.output
    assert f"cd {root} && lattice dashboard --host 0.0.0.0 --port 8861" in result.output
    assert "predates restart metadata" in result.output
    assert signalled == []


def test_restart_refuses_ambiguous_listener_without_signalling(monkeypatch, tmp_path):
    signalled = []
    monkeypatch.setattr(dashboard_module, "_runtime_dir", lambda: tmp_path / "runtime")
    monkeypatch.setattr(
        dashboard_module,
        "_listening_processes",
        lambda port: {100: ["127.0.0.1:8862 (LISTEN)"], 200: ["127.0.0.1:8862 (LISTEN)"]},
    )
    monkeypatch.setattr(dashboard_module.os, "kill", lambda *args: signalled.append(args))

    result = CliRunner().invoke(cli, ["restart", "--port", "8862"])

    assert result.exit_code == 1
    assert "multiple listener processes (100, 200)" in result.output
    assert signalled == []


def test_restart_spawn_failure_reports_reason_and_log_without_success(tmp_path, monkeypatch):
    root = tmp_path / "board"
    (root / ".lattice").mkdir(parents=True)
    cwd = tmp_path / "work"
    cwd.mkdir()
    runtime = tmp_path / "runtime"
    monkeypatch.setattr(dashboard_module, "_runtime_dir", lambda: runtime)
    dashboard_module._ensure_runtime_dir()
    old_boot = {"pid": 5252, "boot_id": "old-boot"}
    record = {
        "schema": 1,
        "pid": 5252,
        "boot_id": "old-boot",
        "board_root": str(root),
        "argv": ["/definitely/missing/lattice", "dashboard", "--port", "8863"],
        "cwd": str(cwd),
        "host": "127.0.0.1",
        "port": 8863,
        "json": False,
        "readonly": False,
        "root_override": str(root),
        "lattice_env_names": ["LATTICE_PREVIOUS_OPTION"],
    }
    dashboard_module._atomic_json(dashboard_module._launch_record_path(5252), record)
    signalled = []
    monkeypatch.setattr(
        dashboard_module,
        "_listening_processes",
        lambda port: {5252: ["127.0.0.1:8863 (LISTEN)"]},
    )
    monkeypatch.setattr(dashboard_module, "_probe_dashboard", lambda *_args, **_kwargs: old_boot)
    monkeypatch.setattr(dashboard_module, "_pid_is_alive", lambda _pid: False)
    monkeypatch.setattr(dashboard_module, "_wait_for_free_port", lambda *_args, **_kwargs: True)
    monkeypatch.setattr(dashboard_module.os, "kill", lambda pid, sig: signalled.append((pid, sig)))
    monkeypatch.delenv("LATTICE_PREVIOUS_OPTION", raising=False)

    def fail_spawn(*_args, **_kwargs):
        raise FileNotFoundError("intentional spawn failure fixture")

    monkeypatch.setattr(dashboard_module.subprocess, "Popen", fail_spawn)
    result = CliRunner().invoke(cli, ["restart", "--port", "8863"])

    assert result.exit_code == 1
    assert "LATTICE_PREVIOUS_OPTION" in result.output
    assert "restarting shell's environment" in result.output
    assert "intentional spawn failure fixture" in result.output
    assert "Log:" in result.output
    assert "restarted" not in result.output.lower()
    assert signalled == [(5252, signal.SIGTERM)]
    assert not dashboard_module._launch_record_path(5252).exists()


def test_restart_drains_real_comment_and_confirms_new_boot(tmp_path):
    root = tmp_path / "board"
    lattice_dir, task_id = _make_board(root)
    cache_home = tmp_path / "cache"
    cache_home.mkdir()
    port = _available_port(range(8860, 8865))
    admitted_marker = cache_home / "write-admitted"
    draining_marker = cache_home / "write-drain-started"
    env = _test_env_with_server_hooks(
        root,
        cache_home,
        admitted_marker=admitted_marker,
        draining_marker=draining_marker,
    )
    process, old_boot = _start_dashboard(root, cache_home, "127.0.0.1", port, env=env)
    owned_pids = {process.pid}
    client = socket.create_connection(("127.0.0.1", port), timeout=5)
    restart = None
    try:
        body = json.dumps(
            {"body": "restart-drain-persisted", "actor": "human:test"},
            separators=(",", ":"),
        ).encode()
        request = (
            f"POST /api/tasks/{task_id}/comment HTTP/1.1\r\n"
            f"Host: 127.0.0.1:{port}\r\n"
            f"Origin: http://127.0.0.1:{port}\r\n"
            "Content-Type: application/json\r\n"
            f"Content-Length: {len(body)}\r\n"
            "Connection: close\r\n\r\n"
        ).encode()
        split = min(3, len(body) - 1)
        client.sendall(request + body[:split])
        _wait_for_path(admitted_marker)

        restart = subprocess.Popen(
            _restart_argv(port),
            cwd=root,
            env=env,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
        )
        request_path = cache_home / "lattice" / "dashboard" / f"restart-{process.pid}.request.json"
        _wait_for_path(request_path)
        _wait_for_path(draining_marker)
        client.sendall(body[split:])
        client.shutdown(socket.SHUT_WR)
        response = bytearray()
        client.settimeout(8)
        while chunk := client.recv(4096):
            response.extend(chunk)
        assert response.startswith(b"HTTP/1.0 200") or response.startswith(b"HTTP/1.1 200")

        output, _ = restart.communicate(timeout=20)
        assert restart.returncode == 0, output
        assert "current Python code" in output
        process.wait(timeout=8)
        new_boot = dashboard_module._probe_dashboard("127.0.0.1", port)
        assert new_boot is not None
        owned_pids.add(new_boot["pid"])
        assert new_boot["pid"] != old_boot["pid"]
        assert new_boot["boot_id"] != old_boot["boot_id"]
        assert set(dashboard_module._listening_processes(port)) == {new_boot["pid"]}
        event_log = lattice_dir / "events" / f"{task_id}.jsonl"
        assert "restart-drain-persisted" in event_log.read_text(encoding="utf-8")
    finally:
        client.close()
        if restart is not None and restart.poll() is None:
            restart.kill()
            restart.wait(timeout=3)
        if process.poll() is None:
            process.terminate()
            process.wait(timeout=5)
        _stop_owned_listener(port, owned_pids)


def test_restart_drain_timeout_returns_failure_and_keeps_old_pid(tmp_path):
    root = tmp_path / "board"
    _lattice_dir, task_id = _make_board(root)
    cache_home = tmp_path / "cache"
    cache_home.mkdir()
    port = _available_port(range(8870, 8875))
    admitted_marker = cache_home / "write-admitted"
    draining_marker = cache_home / "write-drain-started"
    env = _test_env_with_server_hooks(
        root,
        cache_home,
        drain_timeout=0.3,
        admitted_marker=admitted_marker,
        draining_marker=draining_marker,
    )
    process, old_boot = _start_dashboard(root, cache_home, "127.0.0.1", port, env=env)
    owned_pids = {process.pid}
    client = socket.create_connection(("127.0.0.1", port), timeout=5)
    restart = None
    try:
        body = json.dumps(
            {"body": "held-through-drain-timeout", "actor": "human:test"},
            separators=(",", ":"),
        ).encode()
        request = (
            f"POST /api/tasks/{task_id}/comment HTTP/1.1\r\n"
            f"Host: 127.0.0.1:{port}\r\n"
            f"Origin: http://127.0.0.1:{port}\r\n"
            "Content-Type: application/json\r\n"
            f"Content-Length: {len(body)}\r\n"
            "Connection: close\r\n\r\n"
        ).encode()
        split = min(3, len(body) - 1)
        client.sendall(request + body[:split])
        _wait_for_path(admitted_marker)

        restart = subprocess.Popen(
            _restart_argv(port),
            cwd=root,
            env=env,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
        )
        _wait_for_path(draining_marker)
        output, _ = restart.communicate(timeout=15)

        assert restart.returncode == 1, output
        assert "restart canceled" in output.lower()
        assert f"PID {process.pid} remains available" in output
        assert dashboard_module._probe_dashboard("127.0.0.1", port) == old_boot
        assert set(dashboard_module._listening_processes(port)) == {process.pid}

        client.sendall(body[split:])
        client.shutdown(socket.SHUT_WR)
        client.settimeout(8)
        response = bytearray()
        while chunk := client.recv(4096):
            response.extend(chunk)
        assert response.startswith(b"HTTP/1.0 200") or response.startswith(b"HTTP/1.1 200")
    finally:
        client.close()
        if restart is not None and restart.poll() is None:
            restart.kill()
            restart.wait(timeout=3)
        if process.poll() is None:
            process.terminate()
            process.wait(timeout=5)
        _stop_owned_listener(port, owned_pids)


def test_interactive_restart_does_not_reopen_browser(tmp_path):
    root = tmp_path / "board"
    _make_board(root)
    cache_home = tmp_path / "cache"
    cache_home.mkdir()
    port = _available_port(range(8875, 8880))
    browser_record = tmp_path / "browser-opens.txt"
    browser_script = tmp_path / "record_browser.py"
    browser_script.write_text(
        "#!/usr/bin/env python3\n"
        "from pathlib import Path\n"
        "import sys\n"
        f"with Path({str(browser_record)!r}).open('a', encoding='utf-8') as stream:\n"
        "    stream.write(' '.join(sys.argv[1:]) + '\\n')\n",
        encoding="utf-8",
    )
    browser_script.chmod(0o755)
    env = _test_env(root, cache_home)
    env["BROWSER"] = str(browser_script)
    process, old_boot = _start_dashboard(
        root, cache_home, "127.0.0.1", port, output_json=False, env=env
    )
    owned_pids = {process.pid}
    restart = None
    try:
        _wait_for_path(browser_record)
        assert len(browser_record.read_text(encoding="utf-8").splitlines()) == 1
        restart = subprocess.Popen(
            _restart_argv(port),
            cwd=root,
            env=env,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
        )
        output, _ = restart.communicate(timeout=20)
        assert restart.returncode == 0, output
        assert "Stop it with `kill " in output
        process.wait(timeout=8)

        new_boot = dashboard_module._probe_dashboard("127.0.0.1", port)
        assert new_boot is not None
        owned_pids.add(new_boot["pid"])
        assert new_boot["pid"] != old_boot["pid"]
        restart_logs = list((cache_home / "lattice" / "dashboard").glob(f"restart-{port}-*.log"))
        assert len(restart_logs) == 1
        deadline = time.monotonic() + 3
        log_text = ""
        while time.monotonic() < deadline:
            log_text = restart_logs[0].read_text(encoding="utf-8")
            if "Lattice dashboard restarted:" in log_text:
                break
            time.sleep(0.02)
        assert "Lattice dashboard restarted:" in log_text
        time.sleep(0.1)  # allow a mistakenly launched recording browser to write
        assert len(browser_record.read_text(encoding="utf-8").splitlines()) == 1
    finally:
        if restart is not None and restart.poll() is None:
            restart.kill()
            restart.wait(timeout=3)
        if process.poll() is None:
            process.terminate()
            process.wait(timeout=5)
        _stop_owned_listener(port, owned_pids)


def test_concurrent_restarts_on_network_bind_leave_one_healthy_listener(tmp_path):
    root = tmp_path / "board"
    _make_board(root)
    cache_home = tmp_path / "cache"
    cache_home.mkdir()
    port = _available_port(range(8865, 8870))
    process, old_boot = _start_dashboard(root, cache_home, "0.0.0.0", port)
    owned_pids = {process.pid}
    commands = []
    try:
        commands = [
            subprocess.Popen(
                _restart_argv(port),
                cwd=root,
                env=_test_env(root, cache_home),
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
            )
            for _ in range(2)
        ]
        outputs = []
        for command in commands:
            output, _ = command.communicate(timeout=25)
            outputs.append(output)
            assert command.returncode == 0, output
        process.wait(timeout=8)

        new_boot = dashboard_module._probe_dashboard("0.0.0.0", port)
        assert new_boot is not None
        owned_pids.add(new_boot["pid"])
        assert new_boot["pid"] != old_boot["pid"]
        assert new_boot["boot_id"] != old_boot["boot_id"]
        assert set(dashboard_module._listening_processes(port)) == {new_boot["pid"]}
        assert all("restart" in output.lower() for output in outputs)
        assert all("Stop it with `kill " in output and "Log:" in output for output in outputs)
    finally:
        for command in commands:
            if command.poll() is None:
                command.kill()
                command.wait(timeout=3)
        if process.poll() is None:
            process.terminate()
            process.wait(timeout=5)
        _stop_owned_listener(port, owned_pids)
