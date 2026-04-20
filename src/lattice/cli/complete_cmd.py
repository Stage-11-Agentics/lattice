"""lattice completion — print shell completion script to stdout."""

from __future__ import annotations

import json
import os
from pathlib import Path

import click
from click.shell_completion import BashComplete, FishComplete, ZshComplete

from lattice.cli.main import cli

_SHELLS = ["bash", "zsh", "fish"]
_COMPLETE_CLASSES = {"bash": BashComplete, "zsh": ZshComplete, "fish": FishComplete}


def _detect_shell() -> str:
    shell_path = os.environ.get("SHELL", "")
    name = Path(shell_path).name.lower()
    return name if name in _SHELLS else "bash"


def _get_script(shell: str) -> str:
    cls = _COMPLETE_CLASSES[shell]
    return cls(cli, {}, "lattice", "_LATTICE_COMPLETE").source()


@cli.command("completion")
@click.option(
    "--shell",
    "shell_name",
    type=click.Choice(_SHELLS, case_sensitive=False),
    default=None,
    help="Target shell. Auto-detected from $SHELL if omitted.",
)
@click.option(
    "--json",
    "as_json",
    is_flag=True,
    default=False,
    help="Emit {shell, script} as JSON instead of the raw script.",
)
def completion_cmd(shell_name: str | None, as_json: bool) -> None:
    """Print a shell completion script for lattice to stdout.

    \b
    Activate it by sourcing the output in your shell's rc file. Examples:

      # bash
      echo 'source <(lattice completion --shell bash)' >> ~/.bashrc

      # zsh
      echo 'source <(lattice completion --shell zsh)' >> ~/.zshrc

      # fish
      lattice completion --shell fish > ~/.config/fish/completions/lattice.fish

    Lattice does not modify your shell config itself — you decide where it goes.
    """
    shell = shell_name or _detect_shell()
    script = _get_script(shell)
    if as_json:
        click.echo(json.dumps({"ok": True, "data": {"shell": shell, "script": script}}))
    else:
        click.echo(script, nl=False)
