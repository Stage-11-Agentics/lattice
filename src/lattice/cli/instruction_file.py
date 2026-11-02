"""Write the Lattice block into a repo's agent instruction file.

The rule: honour whichever instruction file the repo already has, and never
create a second one. A new CLAUDE.md beside an AGENTS.md hides it from Claude
Code, and a lowercase agents.md is not AGENTS.md to a Linux reader.

Used by ``lattice init`` and ``lattice demo init``.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import click

AGENTS_MD = "AGENTS.md"
CLAUDE_MD = "CLAUDE.md"

#: The one file created when a repo has no instruction file.
DEFAULT_INSTRUCTION_FILE = AGENTS_MD


def write_instruction_block(
    root: Path,
    marker: str,
    block: str,
    *,
    title: str,
    setup_agents: bool | None = None,
    setup_claude: bool | None = None,
    quiet: bool = False,
) -> list[str]:
    """Put *block* in the instruction file(s) *root* already has, or create one.

    *setup_agents* and *setup_claude* carry the ``--setup-*`` flags (``None``
    when not given). Returns the names on disk that now hold the block.
    """
    echo = (lambda _message: None) if quiet else click.echo

    agents_names = _names_on_disk(root, AGENTS_MD)
    claude_names = _names_on_disk(root, CLAUDE_MD)
    for found, kept in ((agents_names, AGENTS_MD), (claude_names, CLAUDE_MD)):
        for extra in found[1:]:
            click.echo(
                f"  Warning: {extra} sits beside {found[0]} and was left alone; "
                f"only one of them should be {kept}.",
                err=True,
            )

    agents = agents_names[0] if agents_names else None
    claude = claude_names[0] if claude_names else None

    if agents is None and claude is None:
        target = CLAUDE_MD if setup_claude else DEFAULT_INSTRUCTION_FILE
        if not _flags_allow((target,), setup_agents, setup_claude):
            return []
        try:
            (root / target).write_text(f"# {title}\n{block}", encoding="utf-8")
        except OSError as exc:
            click.echo(f"  Warning: could not create {target}: {exc}", err=True)
            return []
        echo(f"  Created {target} with Lattice integration.")
        return [target]

    if setup_claude and claude is None:
        echo(
            f"  --setup-claude: did not create {CLAUDE_MD}, because this repo already has "
            f"{agents}. Lattice never adds a second instruction file."
        )
    if setup_agents and agents is None:
        echo(
            f"  --setup-agents: did not create {AGENTS_MD}, because this repo already has "
            f"{claude}. Lattice never adds a second instruction file."
        )

    groups: list[tuple[str, ...]]
    if agents and claude and _same_file(root / agents, root / claude):
        groups = [(AGENTS_MD, CLAUDE_MD)]
    else:
        groups = [(AGENTS_MD,)] if agents else []
        groups += [(CLAUDE_MD,)] if claude else []

    holders: list[str] = []
    attempted = False
    for group in groups:
        if not _flags_allow(group, setup_agents, setup_claude):
            continue
        attempted = True

        if agents and AGENTS_MD in group and agents != AGENTS_MD:
            try:
                _rename_in_two_steps(root, agents, AGENTS_MD)
            except (OSError, subprocess.CalledProcessError) as exc:
                detail = getattr(exc, "stderr", None) or exc
                click.echo(
                    f"  Warning: could not rename {agents} to {AGENTS_MD}: {detail}", err=True
                )
            else:
                echo(f"  Renamed {agents} to {AGENTS_MD}.")
                agents = AGENTS_MD

        names = [
            name
            for name in (
                agents if AGENTS_MD in group else None,
                claude if CLAUDE_MD in group else None,
            )
            if name
        ]
        label = names[0] if len(names) == 1 else f"{names[0]} and {names[1]} (one linked file)"
        try:
            already_there = _append_block(root / names[0], marker, block)
        except (OSError, UnicodeError) as exc:
            click.echo(f"  Warning: could not update {label}: {exc}", err=True)
            continue
        if already_there:
            echo(f"  {label} already has Lattice integration.")
        else:
            echo(f"  Updated {label} with Lattice integration.")
        holders.extend(names)

    if not attempted and setup_agents is not False:
        echo(
            f"  Left {claude} as it is (--no-setup-claude). Lattice never adds a second "
            "instruction file, so no block was written."
        )
    return holders


def describe_instruction_file(name: str) -> str:
    """One line on what *name* is for, for init's summary."""
    if name.casefold() == CLAUDE_MD.casefold():
        return "Claude Code integration"
    return "agent integration instructions"


def _names_on_disk(root: Path, canonical: str) -> list[str]:
    """Names in *root* spelling *canonical* in any case, the best match first.

    Only a directory listing can tell: on a case-insensitive disk
    ``Path("agents.md").exists()`` is true for a file named ``AGENTS.md``.
    """
    found = [name for name in os.listdir(root) if name.casefold() == canonical.casefold()]
    return sorted(found, key=lambda name: (name != canonical, name != canonical.lower(), name))


def _flags_allow(
    group: tuple[str, ...], setup_agents: bool | None, setup_claude: bool | None
) -> bool:
    """Whether the flags let Lattice write to the one file known by these names.

    A file asked for by name is written, a file declined by name is not, and
    otherwise ``--no-setup-agents`` turns integration off.
    """
    flags = [setup_agents if name == AGENTS_MD else setup_claude for name in group]
    if True in flags:
        return True
    if False in flags:
        return False
    return setup_agents is not False


def _same_file(first: Path, second: Path) -> bool:
    try:
        return os.path.samefile(first, second)
    except OSError:
        return False


def _append_block(path: Path, marker: str, block: str) -> bool:
    """Append *block* through any symlink. Returns True if *marker* was already there."""
    content = path.read_text(encoding="utf-8")
    if marker in content:
        return True
    separator = "\n" if content and not content.endswith("\n") else ""
    with open(path, "a", encoding="utf-8") as handle:
        handle.write(separator + block)
    return False


def _git(root: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(root), *args], capture_output=True, text=True, check=True)


def _git_tracked(root: Path, name: str) -> bool:
    try:
        _git(root, "ls-files", "--error-unmatch", "--", name)
    except (OSError, subprocess.CalledProcessError):
        return False
    return True


def _rename_in_two_steps(root: Path, old: str, new: str) -> None:
    """Rename *old* to *new* by way of a temporary name.

    A direct rename that changes only case can be a no-op on a case-insensitive
    disk. A tracked file moves with ``git mv``, so the index agrees. Symlinks
    beside it that point at *old* follow it, or they would dangle on a
    case-sensitive disk.
    """
    tracked = _git_tracked(root, old)
    links = _links_to(root, old)
    temporary = f".{new}.lattice-rename-{os.getpid()}"

    def move(source: str, destination: str) -> None:
        if tracked:
            _git(root, "mv", "--", source, destination)
        else:
            os.rename(root / source, root / destination)

    move(old, temporary)
    try:
        move(temporary, new)
    except (OSError, subprocess.CalledProcessError):
        move(temporary, old)
        raise

    for link in links:
        try:
            _repoint_link(root, link, new)
        except (OSError, subprocess.CalledProcessError) as exc:
            detail = getattr(exc, "stderr", None) or exc
            click.echo(f"  Warning: could not repoint {link} at {new}: {detail}", err=True)


def _links_to(root: Path, name: str) -> list[str]:
    """Symlinks directly in *root* whose own target is the file *name*."""
    links = []
    for entry in os.listdir(root):
        path = root / entry
        if (
            entry != name
            and path.is_symlink()
            and Path(os.readlink(path)).name.casefold() == name.casefold()
            and _same_file(path, root / name)
        ):
            links.append(entry)
    return links


def _repoint_link(root: Path, link: str, target: str) -> None:
    temporary = root / f".{link}.lattice-rename-{os.getpid()}"
    os.symlink(target, temporary)
    os.replace(temporary, root / link)
    if _git_tracked(root, link):
        _git(root, "add", "--", link)
