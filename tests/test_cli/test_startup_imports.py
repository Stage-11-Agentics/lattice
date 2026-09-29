"""CLI start-up stays lean (LAT-359): importing ``lattice.cli.main`` loads no
operation module and no hosted-client code. Each command that needs them
imports them itself, so every local command stops paying for them."""

from __future__ import annotations

import subprocess
import sys


def test_cli_start_up_loads_no_operation_module_or_http_client() -> None:
    code = (
        "import sys, lattice.cli.main\n"
        "ops = sorted(m for m in sys.modules if m.startswith('lattice.ops.')\n"
        "             and m not in ('lattice.ops.base', 'lattice.ops.discovery'))\n"
        "remote = sorted(m for m in sys.modules if m.startswith(('lattice.remote.', 'http.')))\n"
        "print(ops, remote)\n"
        "assert not ops and not remote\n"
    )
    proc = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True)
    assert proc.returncode == 0, proc.stdout + proc.stderr


def test_installed_cli_plugin_sees_every_command_and_runs() -> None:
    """With a CLI plugin installed, every command module loads before the
    plugin registers (as before LAT-359), and the plugin's command runs."""
    code = (
        "import sys, types, importlib.metadata as md\n"
        "import click\n"
        "plugin = types.ModuleType('fake_cli_plugin')\n"
        "def register(group):\n"
        "    assert 'weather' in group.commands and 'create' in group.commands\n"
        "    @group.command('hello-plugin')\n"
        "    def hello():\n"
        "        click.echo('hello from plugin')\n"
        "plugin.register = register\n"
        "sys.modules['fake_cli_plugin'] = plugin\n"
        "ep = md.EntryPoint('fake', 'fake_cli_plugin:register', 'lattice.cli_plugins')\n"
        "real = md.entry_points\n"
        "md.entry_points = lambda **kw: [ep] if kw.get('group') == 'lattice.cli_plugins' "
        "else real(**kw)\n"
        "from lattice.cli.main import cli\n"
        "cli(['hello-plugin'])\n"
    )
    proc = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert proc.stdout == "hello from plugin\n"
