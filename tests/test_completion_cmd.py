import json
import pytest
from click.testing import CliRunner
from lattice.cli.main import cli


@pytest.fixture()
def runner():
    return CliRunner()


def test_completion_print_bash(runner):
    result = runner.invoke(cli, ["completion", "--shell", "bash", "--print"])
    assert result.exit_code == 0
    assert len(result.output.strip()) > 0


def test_completion_print_zsh(runner):
    result = runner.invoke(cli, ["completion", "--shell", "zsh", "--print"])
    assert result.exit_code == 0
    assert len(result.output.strip()) > 0


def test_completion_print_fish(runner):
    result = runner.invoke(cli, ["completion", "--shell", "fish", "--print"])
    assert result.exit_code == 0
    assert len(result.output.strip()) > 0


def test_completion_install_bash(runner, tmp_path):
    bashrc = tmp_path / ".bashrc"
    bashrc.write_text("# existing content\n")
    result = runner.invoke(
        cli,
        ["completion", "--shell", "bash", "--install"],
        env={"HOME": str(tmp_path)},
        catch_exceptions=False,
    )
    assert result.exit_code == 0
    content = bashrc.read_text()
    assert "_LATTICE_COMPLETE=bash_source lattice" in content


def test_completion_install_idempotent(runner, tmp_path):
    bashrc = tmp_path / ".bashrc"
    bashrc.write_text("# existing\n")
    env = {"HOME": str(tmp_path)}
    runner.invoke(cli, ["completion", "--shell", "bash", "--install"], env=env)
    content_after_first = (tmp_path / ".bashrc").read_text()
    runner.invoke(cli, ["completion", "--shell", "bash", "--install"], env=env)
    content_after_second = (tmp_path / ".bashrc").read_text()
    assert content_after_first == content_after_second


def test_completion_uninstall_bash(runner, tmp_path):
    bashrc = tmp_path / ".bashrc"
    bashrc.write_text('# existing\neval "$(_LATTICE_COMPLETE=bash_source lattice)"\n')
    env = {"HOME": str(tmp_path)}
    result = runner.invoke(cli, ["completion", "--shell", "bash", "--uninstall"], env=env)
    assert result.exit_code == 0
    content = bashrc.read_text()
    assert "_LATTICE_COMPLETE=bash_source lattice" not in content
    assert (tmp_path / ".bashrc.lattice.bak").exists()


def test_completion_install_json(runner, tmp_path):
    bashrc = tmp_path / ".bashrc"
    bashrc.write_text("")
    result = runner.invoke(
        cli,
        ["completion", "--shell", "bash", "--install", "--json"],
        env={"HOME": str(tmp_path)},
    )
    assert result.exit_code == 0
    data = json.loads(result.output)
    assert data["ok"] is True
    assert data["data"]["action"] in ("installed", "already_installed")
