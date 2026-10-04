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


def _start_dashboard(root: Path, cache_home: Path, host: str, port: int):
    env = os.environ.copy()
    env["LATTICE_ROOT"] = str(root)
    env["XDG_CACHE_HOME"] = str(cache_home)
    log_path = root / "dashboard-start.log"
    with log_path.open("ab") as log:
        process = subprocess.Popen(
            [
                _LATTICE,
                "dashboard",
                "--host",
                host,
                "--port",
                str(port),
                "--json",
            ],
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


def _wait_for_established_socket(pid: int, timeout: float = 4) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        result = subprocess.run(
            ["lsof", "-nP", "-a", "-p", str(pid), "-iTCP"],
            capture_output=True,
            text=True,
        )
        if "ESTABLISHED" in result.stdout:
            return
        time.sleep(0.05)
    pytest.fail(f"dashboard PID {pid} did not accept the test write")


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
    monkeypatch.setattr(
        dashboard_module.sys,
        "orig_argv",
        [sys.executable, "/venv/bin/lattice", "dashboard", "--json"],
        raising=False,
    )
    server = SimpleNamespace(boot_id="boot-old", pid=100)

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
        "started_ns": record["started_ns"],
    }
    assert not any("SECRET" in key or "environment" in key for key in record)
    dashboard_module._stop_requested = False
    dashboard_module._handle_sighup(signal.SIGHUP, None)
    assert dashboard_module._stop_requested is False


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

    def fail_spawn(*_args, **_kwargs):
        raise FileNotFoundError("intentional spawn failure fixture")

    monkeypatch.setattr(dashboard_module.subprocess, "Popen", fail_spawn)
    result = CliRunner().invoke(cli, ["restart", "--port", "8863"])

    assert result.exit_code == 1
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
    process, old_boot = _start_dashboard(root, cache_home, "127.0.0.1", port)
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
        _wait_for_established_socket(process.pid)

        restart = subprocess.Popen(
            _restart_argv(port),
            cwd=root,
            env=_test_env(root, cache_home),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
        )
        request_path = cache_home / "lattice" / "dashboard" / f"restart-{process.pid}.request.json"
        deadline = time.monotonic() + 4
        while not request_path.exists() and time.monotonic() < deadline:
            time.sleep(0.02)
        assert request_path.exists(), "restart never issued the stop request"
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
    finally:
        for command in commands:
            if command.poll() is None:
                command.kill()
                command.wait(timeout=3)
        if process.poll() is None:
            process.terminate()
            process.wait(timeout=5)
        _stop_owned_listener(port, owned_pids)
