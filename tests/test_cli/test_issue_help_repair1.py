"""``lattice issue --help``: local output keeps the config.json instruction and never names hosting."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from click.testing import CliRunner

from lattice.cli.main import cli


def _help(root: Path, monkeypatch: pytest.MonkeyPatch) -> str:
    monkeypatch.chdir(root)
    monkeypatch.delenv("LATTICE_ROOT", raising=False)
    result = CliRunner().invoke(cli, ["issue", "--help"])
    assert result.exit_code == 0, result.output
    return " ".join(result.output.split())


def test_local_issue_help_has_the_config_instruction_and_no_server(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / ".lattice").mkdir()
    out = _help(tmp_path, monkeypatch)
    assert '"issues": {"enabled": true}' in out and ".lattice/config.json" in out
    assert "server" not in out.lower() and "hosted" not in out.lower()


def test_bound_checkout_help_adds_the_server_command(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / ".lattice-remote.json").write_text(json.dumps({"remote": "a", "project": "b"}))
    out = _help(tmp_path, monkeypatch)
    assert '"issues": {"enabled": true}' in out
    assert "lattice server project config <slug> --set issues.enabled=true" in out
