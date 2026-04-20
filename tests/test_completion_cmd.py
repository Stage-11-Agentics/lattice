import json

import pytest
from click.testing import CliRunner

from lattice.cli.main import cli


@pytest.fixture()
def runner():
    return CliRunner()


@pytest.mark.parametrize("shell", ["bash", "zsh", "fish"])
def test_completion_prints_script(runner, shell):
    result = runner.invoke(cli, ["completion", "--shell", shell])
    assert result.exit_code == 0
    assert len(result.output.strip()) > 0


def test_completion_auto_detects_shell(runner, monkeypatch):
    monkeypatch.setenv("SHELL", "/usr/bin/zsh")
    result = runner.invoke(cli, ["completion"])
    assert result.exit_code == 0
    assert "#compdef lattice" in result.output


def test_completion_json(runner):
    result = runner.invoke(cli, ["completion", "--shell", "bash", "--json"])
    assert result.exit_code == 0
    data = json.loads(result.output)
    assert data["ok"] is True
    assert data["data"]["shell"] == "bash"
    assert "_LATTICE_COMPLETE" in data["data"]["script"]


def test_completion_does_not_touch_filesystem(runner, tmp_path):
    result = runner.invoke(
        cli, ["completion", "--shell", "fish"], env={"HOME": str(tmp_path)}
    )
    assert result.exit_code == 0
    assert list(tmp_path.iterdir()) == []
