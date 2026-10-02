"""Dashboard restarts exec a fresh CLI after closing process-owned resources."""

from __future__ import annotations

import sys
from pathlib import Path

from click.testing import CliRunner

from lattice.cli import dashboard_cmd as dashboard_module
from lattice.cli.main import cli


def test_sighup_restart_execs_after_dashboard_target_closes(tmp_path: Path, monkeypatch) -> None:
    closed: list[str] = []
    exec_calls: list[tuple[str, list[str]]] = []

    class RestartExec(Exception):
        pass

    def target(_lattice_dir, stack, _output_json):
        stack.callback(closed.append, "target")
        return object()

    def execv(executable: str, args: list[str]) -> None:
        exec_calls.append((executable, args))
        closed.append("exec")
        assert dashboard_module.os.environ[dashboard_module._RESTART_ENV] == "1"
        raise RestartExec

    monkeypatch.setattr(dashboard_module, "require_root", lambda _json: tmp_path / ".lattice")
    monkeypatch.setattr(dashboard_module, "_dashboard_target", target)
    monkeypatch.setattr(dashboard_module, "_serve", lambda *_args: True)
    monkeypatch.setattr(dashboard_module.signal, "signal", lambda *_args: None)
    monkeypatch.setattr(dashboard_module.shutil, "which", lambda _script: "/test/bin/lattice")
    monkeypatch.setattr(dashboard_module.os, "execv", execv)
    monkeypatch.setattr(dashboard_module.sys, "argv", ["lattice", "dashboard", "--port", "8800"])
    monkeypatch.delenv(dashboard_module._RESTART_ENV, raising=False)

    result = CliRunner().invoke(cli, ["dashboard", "--port", "8800"])

    assert isinstance(result.exception, RestartExec)
    assert closed == ["target", "exec"]
    assert exec_calls == [
        (sys.executable, [sys.executable, "/test/bin/lattice", "dashboard", "--port", "8800"])
    ]
