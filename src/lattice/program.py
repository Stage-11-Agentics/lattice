"""The name this program was invoked as, for the commands hints print.

Client-side hints name the program as it was run, so an alias (``lattice-v2``)
names itself. Text built on a server never uses this: the server cannot know
the client's name, so it says ``lattice``.
"""

from __future__ import annotations

import click


def program_name() -> str:
    """The console script's basename (``lattice``, or an alias such as
    ``lattice-v2``) with a Windows ``.exe`` dropped.

    Outside a command, under Click's test runner (whose default name is the
    group's, ``cli``), and under ``python -c`` (whose name is ``-c``), it is
    ``lattice``.
    """
    ctx = click.get_current_context(silent=True)
    name = ctx.find_root().info_name if ctx is not None else None
    if not name or name == "cli" or name.startswith("-"):
        return "lattice"
    return name[:-4] if name.lower().endswith(".exe") else name
